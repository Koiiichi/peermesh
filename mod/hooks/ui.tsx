// The trees that the mod draws: message rows in the transcript and the views of the pane.
// Nothing here uses the engine; register.tsx reads the state and gives it to these functions.

import type { ElementTable, RenderElement } from 'claude-code'

import type { Message, Notice, Peer, View } from '../types'
import { accent, bodyLines, clock, oneLine, pad, shortDir, stateColor, stateWord, threads } from './text'

/** One message as a transcript row draws it. */
export type Row = {
  isIncoming: boolean
  name: string
  /** The kind of a message that is not a reply. A reply shows the message that it answers. */
  kind?: string
  id?: string
  body: string
  /** A state of the delivery that is not clear from the rows: it waits, it arrived, it failed. */
  state?: string
  /** The local time of the message as hour:minute. */
  time?: string
  /** The start of the message that this one answers. */
  quote?: string
  /** Why a send failed. */
  note?: string
}

// A transcript row shows this many body lines of a message. The option in its first line
// opens the row and folds it again. ctrl+o shows the message as the agent read it.
const INCOMING_ROWS = 8
const OUTGOING_ROWS = 3
// One block of rows shows this many body lines in all, so a broadcast to many peers and a
// batch of many messages stay under the limit of Claude Code for the text of one tree.
const BLOCK_ROWS = 60
// A folded row shows this many characters of a line, and an open block this many in all.
const LINE_CHARS = 400
const BLOCK_CHARS = 40000
// Below this width the first line of a row has no time.
const NARROW = 60
// The first line keeps this many columns for the arrow, the name and the kind.
const SENDER_ROOM = 24

/** How a block of message rows shows a message that is longer than its room. */
export type Fold = {
  isOpen: boolean
  /** Present where a click reaches the row. Absent, the row names ctrl+o. */
  toggle?: () => void
}

/**
 * A block of rows. `columns` is the width of the transcript. `isTerminal` is false on a
 * graphical surface, which lays the block out in its own column: there the block fills that
 * column, because a width in cells runs past a narrower window and is cut at its edge.
 */
export function messageRows(
  T: ElementTable,
  rows: readonly Row[],
  fold: Fold,
  columns: number,
  isTerminal: boolean,
): RenderElement {
  const { Box } = T
  const share = {
    rows: Math.max(1, Math.floor(BLOCK_ROWS / rows.length)),
    chars: Math.floor(BLOCK_CHARS / rows.length),
  }

  // The blank line above is the one that Claude Code puts above each block of its own.
  return (
    <Box flexDirection="column" marginTop={1} width={isTerminal ? columns : '100%'}>
      {rows.map((row, index) => messageRow(T, row, index, fold, share, columns))}
    </Box>
  )
}

/**
 * One message: the message that it answers, then a line with its sender, then its text.
 * `share` is the room that its block has for each row when the block is open.
 */
function messageRow(
  T: ElementTable,
  row: Row,
  index: number,
  fold: Fold,
  share: { rows: number; chars: number },
  columns: number,
): RenderElement {
  const { Box, Button, Text } = T
  const room = Math.min(row.isIncoming ? INCOMING_ROWS : OUTGOING_ROWS, share.rows)
  const closed = bodyLines(row.body, room, LINE_CHARS)
  const open = bodyLines(row.body, Math.max(room, share.rows), share.chars, share.chars)
  const { lines } = fold.isOpen ? open : closed
  // A row has a fold when it does not show all of its message: lines, or the end of a line.
  const hasFold = closed.more > 0 || closed.isCut
  const color = accent(row.name)
  // The right end of the first line: what the delivery did, the fold, and the time. Each part
  // is there only when it says something.
  const count = closed.more === 0 ? 'more' : `${closed.more} more ${closed.more === 1 ? 'line' : 'lines'}`
  // An open row that still does not show all of its message says so in the option, which
  // never gives way: a row must not look complete when it is not.
  const rest = columns >= NARROW ? 'rest in ctrl+o' : 'partial'
  const collapse = open.more > 0 || open.isCut ? `▾ collapse · ${rest}` : '▾ collapse'
  const label = fold.toggle === undefined ? `${count} · ctrl+o` : fold.isOpen ? collapse : `▸ ${count}`
  // The right end of the first line, in the order shown. Each part is there only when it
  // says something. `drop` is the order in which the id and the time give way to the sender.
  // The state and the option never give way: on a terminal with no room, the name is cut.
  const parts = [
    row.state !== undefined && {
      drop: 9,
      text: stateWord(row.state),
      draw: (
        <Text color={stateColor(row.state)} dimColor={stateColor(row.state) === undefined}>
          {stateWord(row.state)}
        </Text>
      ),
    },
    hasFold && {
      drop: 9,
      text: label,
      draw:
        fold.toggle !== undefined ? (
          <Button key={`row:${index}`} plain onPress={fold.toggle}>
            <Text color="suggestion">{label}</Text>
          </Button>
        ) : (
          <Text dimColor>{label}</Text>
        ),
    },
    fold.isOpen && hasFold && row.id !== undefined && {
      drop: 1,
      text: row.id.slice(0, 8),
      draw: <Text dimColor>{row.id.slice(0, 8)}</Text>,
    },
    columns >= NARROW && row.time !== undefined && { drop: 3, text: row.time, draw: <Text dimColor>{row.time}</Text> },
  ].filter(part => part !== false)
  const sender = 2 + row.name.length + (row.kind !== undefined ? 3 + row.kind.length : 0)
  const width = (list: typeof parts) => list.reduce((sum, part) => sum + part.text.length + 3, 0)
  let right = parts
  for (const drop of [1, 3]) {
    if (Math.min(sender, SENDER_ROOM) + 2 + width(right) > columns) {
      right = right.filter(part => part.drop !== drop)
    }
  }

  return (
    <Box flexDirection="column">
      {row.quote !== undefined && (
        // The bar marks a quote. With no arrow and no name, it cannot read as a message of
        // its own.
        <Box paddingLeft={2}>
          <Text dimColor italic wrap="truncate-end">
            ▎ {row.quote}
          </Text>
        </Box>
      )}
      <Box justifyContent="space-between">
        <Box flexShrink={1}>
          <Text wrap="truncate-end">
            <Text color={color}>{row.isIncoming ? '←' : '→'} </Text>
            <Text bold color={color}>
              {row.name}
            </Text>
            {row.kind !== undefined && <Text dimColor> · {row.kind}</Text>}
          </Text>
        </Box>
        <Box flexShrink={0} marginLeft={2}>
          {right.map((part, at) => [at > 0 && <Text dimColor> · </Text>, part.draw])}
        </Box>
      </Box>
      <Box flexDirection="column" paddingLeft={2}>
        {lines.map(line => (
          <Text dimColor={!row.isIncoming}>{line || ' '}</Text>
        ))}
        {row.note !== undefined && <Text color="error">{row.note}</Text>}
      </Box>
    </Box>
  )
}

export type PaneData = {
  me: string | null
  peers: readonly Peer[]
  messages: readonly Message[]
  view: View
  notice: Notice
  /** A send from the pane runs. The pane draws no text field until it ends. */
  isSending: boolean
  /** False when the last read of peermesh failed. The lists are then the last ones read. */
  isCurrent: boolean
  columns: number
}

export type PaneHandlers = {
  /** Show `view` and move the focus to the element with the key `focus`. */
  go: (view: View, focus: string) => void
  /** Send to the session with this id. A name can pass to a different session. */
  send: (peerId: string, body: string) => void
  reply: (msgId: string, body: string) => void
}

// The pane lists this many threads. `peers log` and `peers thread` show older messages.
const HOME_THREADS = 8
const THREAD_BODY_ROWS = 30
// A thread view shows this many of its newest messages, so its tree stays under the limit
// of Claude Code for the text of one tree.
const THREAD_MESSAGES = 12

export function paneTree(T: ElementTable, data: PaneData, on: PaneHandlers): RenderElement {
  const { Box, Text } = T
  const peer = data.view.kind === 'peer' ? data.peers.find(p => p.id === (data.view as { id: string }).id) : undefined
  const thread =
    data.view.kind === 'thread'
      ? data.messages.filter(m => m.thread === (data.view as { id: string }).id)
      : []

  return (
    <Box flexDirection="column">
      {!data.isCurrent && (
        <Text color="warning">The peers command gave no answer. The lists can be old. Run peers doctor.</Text>
      )}
      {data.isSending && <Text color="warning">sending…</Text>}
      {data.notice !== null && (
        <Text color={data.notice.isError ? 'error' : 'success'}>{data.notice.text}</Text>
      )}
      {peer !== undefined
        ? peerView(T, data, peer, on)
        : thread.length > 0
          ? threadView(T, data, thread, on)
          : homeView(T, data, on)}
    </Box>
  )
}

function homeView(T: ElementTable, data: PaneData, on: PaneHandlers): RenderElement {
  const { Box, Text } = T
  const mine = data.peers.find(p => p.id === data.me)
  const others = data.peers.filter(p => p.id !== data.me)
  const list = threads(data.messages)

  return (
    <Box flexDirection="column">
      <Text dimColor>sessions</Text>
      {mine !== undefined && (
        <Text wrap="truncate-end">
          <Text color="claude">◆</Text> {mine.name} <Text dimColor>this session</Text>
        </Text>
      )}
      {others.map(p => peerButton(T, data, p, on))}
      {others.length === 0 && <Text dimColor>No other session is live.</Text>}
      <Box marginTop={1} flexDirection="column">
        <Text dimColor>threads</Text>
        {list.slice(0, HOME_THREADS).map(t => threadButton(T, data, t.last, t.count, on))}
        {list.length === 0 && <Text dimColor>No message yet.</Text>}
        {list.length > HOME_THREADS && (
          <Text dimColor>… {list.length - HOME_THREADS} older · peers log</Text>
        )}
      </Box>
    </Box>
  )
}

/** The key of the element that takes the focus on the home view: its first row. */
export function homeFocus(data: Pick<PaneData, 'me' | 'peers' | 'messages'>): string {
  const first = data.peers.find(p => p.id !== data.me)
  const newest = threads(data.messages)[0]

  return first !== undefined ? `peer:${first.id}` : newest !== undefined ? `thread:${newest.last.thread}` : 'back'
}

/** The message of a thread that a reply from this session answers: the newest one to it. */
function toAnswer(data: PaneData, thread: string): Message | undefined {
  return [...data.messages].reverse().find(m => m.thread === thread && m.to === data.me)
}

function replyKey(data: PaneData, thread: string): string {
  const message = toAnswer(data, thread)

  return message !== undefined ? `reply:${message.id}` : 'back'
}

function nameWidth(columns: number): number {
  return Math.max(10, Math.min(24, Math.floor(columns / 3)))
}

function peerButton(T: ElementTable, data: PaneData, peer: Peer, on: PaneHandlers): RenderElement {
  const { Button, Text } = T
  const isBusy = peer.status === 'busy'
  const where = [peer.dir.split('/').pop() ?? '', peer.branch ?? ''].filter(Boolean).join(' · ')
  // The name, the status and the waiting count stay on a narrow pane. The place gives way.
  const room = data.columns - nameWidth(data.columns) - 18

  return (
    <Button key={`peer:${peer.id}`} plain onPress={() => on.go({ kind: 'peer', id: peer.id }, `to:${peer.id}`)}>
      <Text color={isBusy ? 'warning' : 'inactive'}>{isBusy ? '●' : '○'}</Text>{' '}
      {pad(peer.name, nameWidth(data.columns))}{' '}
      <Text dimColor>
        {pad(peer.status, 5)}
        {room >= 8 ? ' ' + oneLine(where, room) : ''}
      </Text>
      {peer.waiting > 0 && <Text color="warning"> {peer.waiting} waiting</Text>}
    </Button>
  )
}

function threadButton(
  T: ElementTable,
  data: PaneData,
  last: Message,
  count: number,
  on: PaneHandlers,
): RenderElement {
  const { Button, Text } = T
  const isOut = last.from === data.me
  const isIn = last.to === data.me
  // A thread between two other sessions names both of them, so it gets two name columns.
  const isMine = isOut || isIn
  const who = isOut ? last.toName : isIn ? last.fromName : `${last.fromName} → ${last.toName}`
  const whoWidth = Math.min(nameWidth(data.columns) * (isMine ? 1 : 2) + (isMine ? 0 : 3), data.columns - 19)
  const word = stateWord(last.state)
  const room = data.columns - whoWidth - 21

  return (
    <Button key={`thread:${last.thread}`} plain onPress={() => on.go({ kind: 'thread', id: last.thread }, replyKey(data, last.thread))}>
      <Text dimColor>{clock(last.sentAt)}</Text>{' '}
      <Text color={isIn ? 'suggestion' : 'subtle'}>{isOut ? '→' : isIn ? '←' : '·'}</Text>{' '}
      {pad(who, whoWidth)}{' '}
      <Text color={stateColor(last.state)} dimColor={stateColor(last.state) === undefined}>
        {pad(word, 9)}
      </Text>
      {room >= 8 && (
        <Text dimColor>
          {' '}
          {oneLine((count > 1 ? `(${count}) ` : '') + last.body, room)}
        </Text>
      )}
    </Button>
  )
}

function backButton(T: ElementTable, data: PaneData, on: PaneHandlers): RenderElement {
  const { Button } = T

  return (
    <Button key="back" plain label="‹ back" onPress={() => on.go({ kind: 'home' }, homeFocus(data))} />
  )
}

function peerView(T: ElementTable, data: PaneData, peer: Peer, on: PaneHandlers): RenderElement {
  const { Box, Text } = T
  const list = threads(data.messages.filter(m => m.from === peer.id || m.to === peer.id))

  return (
    <Box flexDirection="column">
      {backButton(T, data, on)}
      <Text wrap="truncate-end">
        <Text bold>{peer.name}</Text>
        <Text dimColor>
          {'  '}
          {peer.runtime} · {peer.status}
          {peer.waiting > 0 ? ` · ${peer.waiting} waiting` : ''}
        </Text>
      </Text>
      <Text dimColor wrap="truncate-start">
        {shortDir(peer.dir)}
        {peer.branch !== null ? ` · ${peer.branch}` : ''}
      </Text>
      {'Input' in T && !data.isSending && peer.id !== data.me && (
        <Box marginTop={1}>
          <T.Input
            key={`to:${peer.id}`}
            placeholder={`Message ${peer.name}`}
            submitLabel="send"
            onSubmit={value => on.send(peer.id, value)}
          />
        </Box>
      )}
      <Box marginTop={1} flexDirection="column">
        <Text dimColor>threads</Text>
        {list.slice(0, HOME_THREADS).map(t => threadButton(T, data, t.last, t.count, on))}
        {list.length === 0 && <Text dimColor>No message with this session yet.</Text>}
      </Box>
    </Box>
  )
}

function threadView(
  T: ElementTable,
  data: PaneData,
  thread: readonly Message[],
  on: PaneHandlers,
): RenderElement {
  const { Box, Text } = T
  const name = (id: string, fallback: string) => (id === data.me ? 'you' : fallback)
  const answered = toAnswer(data, thread[0]?.thread ?? '')

  return (
    <Box flexDirection="column">
      {backButton(T, data, on)}
      {thread.length > THREAD_MESSAGES && (
        <Text dimColor>
          … {thread.length - THREAD_MESSAGES} earlier · peers thread {(thread[0]?.id ?? '').slice(0, 8)}
        </Text>
      )}
      {thread.slice(-THREAD_MESSAGES).map(m => {
        const { lines, more, isCut } = bodyLines(m.body, THREAD_BODY_ROWS, LINE_CHARS)

        return (
          <Box flexDirection="column" marginTop={1}>
            <Text wrap="truncate-end">
              <Text dimColor>{clock(m.sentAt)} </Text>
              <Text bold>{name(m.from, m.fromName)}</Text>
              <Text dimColor> → </Text>
              {name(m.to, m.toName)}
              <Text dimColor>  {m.kind.replace('_', ' ')}</Text>
              <Text color={stateColor(m.state)} dimColor={stateColor(m.state) === undefined}>
                {'  '}
                {stateWord(m.state)}
              </Text>
            </Text>
            <Box flexDirection="column" paddingLeft={2}>
              {lines.map(line => (
                <Text>{line || ' '}</Text>
              ))}
              {(more > 0 || isCut) && <Text dimColor>more of this message: peers thread {m.id.slice(0, 8)}</Text>}
              {stateWord(m.state) === 'failed' && m.note !== '' && <Text color="error">{m.note}</Text>}
            </Box>
          </Box>
        )
      })}
      {'Input' in T && !data.isSending && answered !== undefined && (
        <Box marginTop={1}>
          <T.Input
            key={`reply:${answered.id}`}
            placeholder={`Reply to ${answered.fromName}`}
            submitLabel="send"
            onSubmit={value => on.reply(answered.id, value)}
          />
        </Box>
      )}
    </Box>
  )
}
