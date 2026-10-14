// The peermesh mod for Claude Code. It draws what peermesh already does: the hooks, the MCP
// server and the `peers` command stay in charge of each message. This module reads the state
// through `peers --json watch`, and it keeps no record of its own.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, ProcessRunResult, Register } from 'claude-code'

import type { Message, Peer, View } from '../types'
import { parseFrame } from './frame'
import { oneLine, warnsOfResend } from './text'
import { homeFocus, messageRows, paneTree } from './ui'
import type { Row } from './ui'

const PANE = 'peermesh'
const COMMAND = 'mesh'
const SEND = 'mcp__peermesh__peers_send'
const REPLY = 'mcp__peermesh__peers_reply'
// The timer runs at this period. It reads the state at each tick while the pane is open. While
// the pane is closed, it reads at each fifth tick, and only while a message that this session
// sent waits for its peer: no event in this session tells that the peer took it.
const TICK_MS = 3000
const WAITING_TICKS = 5

const me = atom({ plugin: 'peermesh', key: 'me' } as const, null)
const peers = atom({ plugin: 'peermesh', key: 'peers' } as const, [])
const messages = atom({ plugin: 'peermesh', key: 'messages' } as const, [])
const view = atom({ plugin: 'peermesh', key: 'view' } as const, { kind: 'home' })
const notice = atom({ plugin: 'peermesh', key: 'notice' } as const, null)
const sending = atom({ plugin: 'peermesh', key: 'isSending' } as const, false)
const current = atom({ plugin: 'peermesh', key: 'isCurrent' } as const, true)

// The executable that ran last, and the state as it was written last. A reload starts them
// again, which costs one more read.
let command: string | undefined
let written: { me: string | null; peers: string; messages: string } = { me: null, peers: '', messages: '' }
let reading: Promise<void> | undefined
let isStale = false
let isSending = false
let hasWaiting = false
let wasRead = true
let drawsDeliveries = false

// A send can wait for the transport of the peer, which gives Codex 30 seconds.
const SEND_TIMEOUT_MS = 60000

async function run(
  $: EngineInterface,
  exe: string,
  args: readonly string[],
  init: { stdin?: string; timeoutMs: number },
): Promise<ProcessRunResult> {
  // The session id makes `peers` act as this session, not as a session that started it.
  return $.process.run([exe, ...args], { env: { CLAUDE_CODE_SESSION_ID: await $.session.id() }, ...init })
}

/**
 * Run a `peers` command that only reads, and remember the executable that started.
 * Undefined means that no `peers` executable gave an answer.
 */
async function readPeers(
  $: EngineInterface,
  args: readonly string[],
): Promise<ProcessRunResult | undefined> {
  const home = (await $.env.get('HOME')) ?? ''
  // `peers install` puts the command in ~/.local/bin, which the PATH of a desktop app can lack.
  for (const exe of command !== undefined ? [command] : ['peers', `${home}/.local/bin/peers`]) {
    try {
      const ran = await run($, exe, args, { timeoutMs: 15000 })
      command = exe

      return ran
    } catch {
      command = undefined
    }
  }

  return undefined
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/**
 * Read the sessions and the messages, and write each list to the state if it changed.
 * A call during a read does not start a second read. The read runs one more time after it
 * ends, because its answer can be older than the event that made the call. The promise
 * resolves after a read that started after the call.
 */
function refresh($: EngineInterface): Promise<void> {
  if (reading !== undefined) {
    isStale = true

    return reading
  }
  reading = (async () => {
    try {
      do {
        isStale = false
        await readState($)
      } while (isStale)
    } finally {
      reading = undefined
    }
  })()

  return reading
}

/** Read the state one time. The pane says so when the read fails and its lists can be old. */
async function readState($: EngineInterface): Promise<void> {
  const isRead = await readOnce($).catch(() => false)
  if (isRead !== wasRead) {
    wasRead = isRead
    await update($, current, () => isRead)
  }
}

async function readOnce($: EngineInterface): Promise<boolean> {
  const ran = await readPeers($, ['--json', 'watch'])
  if (ran === undefined || ran.exitCode !== 0) {
    return false
  }
  const data = JSON.parse(ran.stdout) as {
    me: string | null
    peers: Record<string, unknown>[]
    messages: Record<string, unknown>[]
  }
  const nextPeers: Peer[] = data.peers.map(p => ({
    id: text(p.id),
    name: text(p.name),
    runtime: text(p.runtime),
    status: text(p.status),
    branch: typeof p.branch === 'string' ? p.branch : null,
    dir: text(p.worktree) || text(p.cwd),
    waiting: Number(p.waiting) || 0,
  }))
  const nextMessages: Message[] = data.messages.map(m => ({
    id: text(m.id),
    from: text(m.from),
    fromName: text(m.from_name),
    to: text(m.to),
    toName: text(m.to_name),
    kind: text(m.kind),
    body: text(m.body),
    sentAt: Number(m.sent_at) || 0,
    thread: text(m.thread) || text(m.id),
    inReplyTo: typeof m.in_reply_to === 'string' ? m.in_reply_to : null,
    state: text(m.state),
    note: text(m.note),
  }))
  // A write draws each reader again, so a list that did not change is not written.
  const next = { me: data.me, peers: JSON.stringify(nextPeers), messages: JSON.stringify(nextMessages) }
  if (next.me !== written.me) {
    await update($, me, () => data.me)
  }
  if (next.peers !== written.peers) {
    await update($, peers, () => nextPeers)
  }
  if (next.messages !== written.messages) {
    await update($, messages, () => nextMessages)
  }
  written = next
  hasWaiting = nextMessages.some(
    m => m.from === data.me && (m.state === 'pending' || m.state === 'sending'),
  )

  return true
}

async function tick($: EngineInterface, count: number): Promise<void> {
  // A tick during a read asks for nothing more. A read that is slower than the timer would
  // otherwise run again without an end.
  if (reading !== undefined) {
    return
  }
  const isDue = hasWaiting && count % WAITING_TICKS === 0
  if (isDue || (await $.ui.panes()).some(pane => pane.id === PANE)) {
    await refresh($)
  }
}

/**
 * Tell the peermesh hooks of this session that the mod draws the messages they give the agent.
 * The value is the session id, so a process that inherits the variable does not match it.
 * Only the terminal was seen to draw the line of the mod. On each other surface the variable
 * is not set, and the hooks write their own line.
 */
async function mark($: EngineInterface): Promise<void> {
  drawsDeliveries = (await $.session.surface()) === 'terminal'
  await $.env.set('PEERMESH_MOD', drawsDeliveries ? await $.session.id() : undefined)
}

/** Send `body` with `peers send` or `peers reply`, and record the send for the agent. */
async function send($: EngineInterface, args: readonly string[], body: string): Promise<void> {
  // A second Enter while a send runs would be a second message.
  if (body.trim() === '' || isSending) {
    return
  }
  isSending = true
  try {
    // The pane draws no text field while this is true, so a different message cannot be
    // typed and lost.
    await update($, sending, () => true)
    await update($, notice, () => null)
    await deliver($, args, body)
  } finally {
    isSending = false
    await update($, sending, () => false)
  }
}

async function deliver($: EngineInterface, args: readonly string[], body: string): Promise<void> {
  if (command === undefined) {
    await refresh($)
  }
  if (command === undefined) {
    await update($, notice, () => ({ text: 'The peers command did not start.', isError: true }))

    return
  }
  // A /clear or a /resume during the send gives this process a different conversation.
  const session = await $.session.id()
  // One attempt only. A send that ran too long, or that ended without its result, can be
  // delivered, and a second send would be a second message.
  const ran = await run($, command, ['--json', ...args, '--body', '-'], {
    stdin: body,
    timeoutMs: SEND_TIMEOUT_MS,
  }).catch(() => undefined)
  let sent: { msg_id?: string | null; to?: string; status?: string; note?: string } | undefined
  try {
    sent = (JSON.parse(ran?.stdout ?? '') as (typeof sent)[])[0]
  } catch {
    sent = undefined
  }
  // `peers` prints a result without an id, or ends with the status 2 and a reason, when it
  // refused the message before it sent anything.
  const reason = sent !== undefined ? sent.note : ran?.exitCode === 2 ? oneLine(ran.stderr, 200) : undefined
  if (sent === undefined && reason === undefined) {
    await update($, notice, () => ({
      text: 'The send gave no answer. The message can be delivered. Read the thread before you send it again.',
      isError: true,
    }))
    void refresh($)

    return
  }
  if (sent === undefined || !sent.msg_id) {
    await update($, notice, () => ({ text: `not sent · ${reason ?? ''}`, isError: true }))

    return
  }
  const id = sent.msg_id.slice(0, 8)
  // A note says why a message was refused. A note can also say that the message is with the
  // peer although the send did not confirm it, and that it must not be sent again.
  const hasWarning = sent.status === 'refused' || warnsOfResend(sent.note ?? '')
  const status = hasWarning && sent.note ? `${sent.status} · ${sent.note}` : `${sent.status}`
  const outcome = `${sent.status} · ${sent.to} · ${id}` + (hasWarning && sent.note ? ` · ${sent.note}` : '')
  await update($, notice, () => ({ text: outcome, isError: hasWarning }))
  // The ledger has the session that `peers` sent the message as. That is the one fact about the
  // sender; without it, a changed id says that the conversation changed during the send.
  await refresh($)
  const now = await $.session.id()
  const record = (await read($, messages)).find(m => m.id === sent.msg_id)
  const isOfThisConversation = record !== undefined ? record.from === `claude:${now}` : now === session
  if (!isOfThisConversation) {
    // A reply goes to the conversation that sent the message. A row here would tell the
    // wrong agent that the message is its own.
    await update($, notice, () => ({
      text: `${outcome} · Sent before this conversation changed. A reply goes to the earlier one.`,
      isError: true,
    }))

    return
  }
  // The agent of this session did not write the message. Without this row, it cannot
  // understand a reply to it.
  const quoted = body.split('\n').map(line => '> ' + line)
  const recorded = await $.session
    .append({
      message: {
        type: 'user',
        content: [
          {
            type: 'text',
            text: [
              'peermesh: The user sent a message from the peermesh pane of this session.',
              'The message has this session as its sender. A reply to it comes to this session.',
              '',
              `to ${sent.to} · msg ${id} · ${status}`,
              ...quoted,
            ].join('\n'),
          },
        ],
      },
    })
    .catch(() => undefined)
  // Claude Code or a different mod can refuse the row. The message is sent, so the notice
  // must say that the agent does not know of it.
  if (recorded === undefined || recorded.deny !== undefined) {
    await update($, notice, () => ({
      text: `${outcome} · The agent of this session has no record of this message. Tell it what you sent.`,
      isError: true,
    }))
  }
  // The send is complete. The read that shows it must not hold the next send.
  void refresh($)
}

/** Show a view of the pane and give the keyboard to the element with the key `focus`. */
async function show($: EngineInterface, target: View, focus: string): Promise<void> {
  await update($, view, () => target)
  // The element is in the next drawing. The engine waits for it; a pane that does not hold
  // the keyboard refuses, and nothing more is necessary.
  await $.ui.focus({ requestId: PANE, key: focus }).catch(() => undefined)
}

function known(all: readonly Message[], id: unknown): Message | undefined {
  return typeof id === 'string' && id.length >= 6 ? all.find(m => m.id.startsWith(id)) : undefined
}

/** The rows of one peers_send or peers_reply call, from its input and its result. */
function sentRows(
  call: {
    tool: string
    input: unknown
    isRunning: boolean
    isErrored: boolean
    isInterrupted: boolean
    output?: unknown
  },
  all: readonly Message[],
): Row[] {
  const input = (call.input ?? {}) as Record<string, unknown>
  const isReply = call.tool === REPLY
  const parent = isReply ? known(all, input.msg_id) : undefined
  const base = {
    isIncoming: false,
    kind: isReply ? (input.done === true ? 'reply · done' : 'reply') : text(input.kind) || 'info',
    body: text(input.body),
    re: parent !== undefined ? oneLine(parent.body, 60) : undefined,
  }
  const targets = Array.isArray(input.to) ? input.to.map(text).join(', ') : text(input.to)
  const name = parent?.fromName ?? (targets || '…')
  // An interrupted send has no result. The row must not look like a send that completed.
  const unknown = 'The message can be delivered. Read the thread before you send it again.'
  if (call.isInterrupted) {
    return [{ ...base, name, state: 'interrupted', note: unknown }]
  }
  if (call.isErrored) {
    // The tool gave an error and no result. The error text says what is known; the server can
    // fail after it sent the message, so the row does not say that the message was refused.
    return [{ ...base, name, state: 'error', note: `${oneLine(text(call.output), 200)} · ${unknown}` }]
  }
  if (typeof call.output !== 'string') {
    return [{ ...base, name, state: 'sending' }]
  }
  try {
    // The MCP server wraps a list in { result }. One send result comes without the wrapper.
    const parsed = JSON.parse(call.output) as Record<string, unknown>
    const result = parsed.result ?? parsed
    const results = (Array.isArray(result) ? result : [result]) as Record<string, unknown>[]

    return results.map(r => {
      // The ledger knows what happened after the send. The result holds the first outcome.
      const record = known(all, r.msg_id)
      const state = record?.state ?? text(r.status)
      // A warning against a second send stays, from the ledger or from the result. A message
      // that failed shows the reason that the ledger has now.
      const notes = [record?.note ?? '', text(r.note)].filter(note => note !== '')
      const isFailed = state === 'refused' || state === 'undelivered'

      return {
        ...base,
        name: text(r.to) || name,
        id: text(r.msg_id) || undefined,
        state,
        note: notes.find(warnsOfResend) ?? (isFailed ? notes[0] : undefined),
      }
    })
  } catch {
    return [{ ...base, name, state: 'no result', note: unknown }]
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await mark($)
    let count = 0
    $.clock.every(TICK_MS, () => {
      count += 1
      void tick($, count)
    })
    void refresh($)
    // Claude Code has a /peers command of its own, which lists the sessions that it can message.
    await $.command.register({
      name: COMMAND,
      description: 'Show the peermesh sessions and messages',
      argumentHint: '[session]',
      immediate: true,
    })

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    // A /clear gives the session a new id and starts no session.start.
    await mark($)
    const submitted = await next(e)
    void refresh($)

    return submitted
  })

  on('turn.complete', ($, e, next) => {
    void refresh($)

    return next(e)
  })

  on('tool.call', { tool: SEND }, async ($, e, next) => {
    const ran = await next(e)
    void refresh($)

    return ran
  })

  on('tool.call', { tool: REPLY }, async ($, e, next) => {
    const ran = await next(e)
    void refresh($)

    return ran
  })

  // A hook gives the agent a message in the middle of a turn, and the transcript has no row
  // for it. The Stop hook is not in this list: Claude Code shows its feedback in full.
  on('session.append', { door: 'hook-context' }, async ($, e, next) => {
    const stored = await next(e)
    const isDelivery =
      e.origin.kind === 'hook' &&
      (e.origin.event === 'PostToolUse' || e.origin.event === 'UserPromptSubmit') &&
      e.message.name === 'hook_additional_context'
    if (isDelivery && drawsDeliveries) {
      const context = e.message.content.map(block => (block.type === 'text' ? block.text : '')).join('\n')
      for (const item of parseFrame(context) ?? []) {
        const kind = item.isDone ? `${item.kind} · done` : item.kind
        $.ui.log(`← ${item.from} · ${kind} · ${item.id.slice(0, 8)} · ${oneLine(item.body, 200)}`)
      }
      void refresh($)
    }

    return stored
  })

  on('command.run', { command: 'mesh' }, async ($, e) => {
    await refresh($)
    const name = e.args.trim()
    const found = name === '' ? undefined : (await read($, peers)).find(p => p.name === name)
    await update($, notice, () =>
      name !== '' && found === undefined
        ? { text: `No live session has the name ${oneLine(name, 40)}.`, isError: true }
        : null,
    )
    const opened = await $.ui.open({ id: PANE, title: 'peermesh', focus: true, columns: 62, rows: 16 })
    const data = { me: await read($, me), peers: await read($, peers), messages: await read($, messages) }
    await (found !== undefined
      ? show($, { kind: 'peer', id: found.id }, `to:${found.id}`)
      : show($, { kind: 'home' }, homeFocus(data)))

    return opened.isPlaced ? {} : { text: 'The peermesh pane opens when the terminal is wider.' }
  })

  on(
    'ui.render',
    { component: 'UserMessage', props: { origin: { kind: 'peer' } } },
    async ($, e, next) => {
      // ctrl+o shows the frame as the agent read it.
      const items = e.props.isExpanded ? null : parseFrame(e.props.text)
      if (items === null) {
        return next(e)
      }
      const all = await read($, messages)
      const rows = items.map((item): Row => {
        const record = known(all, item.id)
        const parent = record?.inReplyTo ? all.find(m => m.id === record.inReplyTo) : undefined
        const isAnswered = record?.state === 'answered' || record?.state === 'done'

        return {
          isIncoming: true,
          name: item.from,
          kind: item.isDone ? `${item.kind} · done` : item.kind,
          id: item.id,
          body: item.body,
          re: parent !== undefined ? oneLine(parent.body, 60) : undefined,
          note: isAnswered ? 'this session replied' : undefined,
        }
      })

      return messageRows($.ui.resolve(e), rows)
    },
  )

  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const sends = e.props.calls.filter(call => call.tool === SEND || call.tool === REPLY)
    if (e.props.isExpanded || sends.length === 0) {
      return next(e)
    }
    const all = await read($, messages)
    const rows = sends.flatMap(call => sentRows(call, all))

    const T = $.ui.resolve(e)
    if (sends.length === e.props.calls.length) {
      return messageRows(T, rows)
    }
    // The group has other calls. The line that Claude Code draws for the group stays, because
    // it shows which of them run and which of them failed.
    const { Box } = T

    return (
      <Box flexDirection="column">
        {messageRows(T, rows)}
        {await next(e)}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) =>
    paneTree(
      $.ui.resolve(e),
      {
        me: await read($, me),
        peers: await read($, peers),
        messages: await read($, messages),
        view: await read($, view),
        notice: await read($, notice),
        isSending: await read($, sending),
        isCurrent: await read($, current),
        columns: e.props.bodyColumns,
      },
      {
        go: (target, focus) => {
          void show($, target, focus)
        },
        send: (peerId, body) => {
          void send($, ['send', peerId, '--kind', 'request'], body)
        },
        reply: (msgId, body) => {
          void send($, ['reply', msgId], body)
        },
      },
    ),
  )
}
