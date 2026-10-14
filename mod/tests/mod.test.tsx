import { expect, mock, test } from 'claude-code/testing'

import { parseFrame } from '../hooks/frame'
import { FRAMES } from './frames'

const SEND = 'mcp__peermesh__peers_send'
const ME = 'claude:me'
const PEER = 'codex:x'
const ASKED = 'aa'.repeat(16)
const TIMED_OUT = 'cc'.repeat(16)
// What the Codex transport says when `codex queue` runs too long.
const QUEUE_NOTE = 'codex queue timed out. The message can be in the queue. Do not resend.'

// What `peers --json watch` prints: this session, one peer, and one request from the peer.
const SNAPSHOT = JSON.stringify({
  me: ME,
  peers: [
    { id: ME, name: 'rotom-infra', runtime: 'claude', status: 'busy', branch: 'main', worktree: '/w/rotom', waiting: 0 },
    { id: PEER, name: 'codex-x', runtime: 'codex', status: 'idle', branch: 'fix/cart', worktree: '/w/shop', waiting: 1 },
  ],
  messages: [
    {
      id: ASKED,
      from: PEER,
      from_name: 'codex-x',
      to: ME,
      to_name: 'rotom-infra',
      kind: 'request',
      body: 'Which branch has the cart fix?',
      sent_at: 1791936000,
      thread: ASKED,
      in_reply_to: null,
      state: 'delivered',
    },
    {
      id: TIMED_OUT,
      from: ME,
      from_name: 'rotom-infra',
      to: PEER,
      to_name: 'codex-x',
      kind: 'request',
      body: 'Run the tests.',
      sent_at: 1791936100,
      thread: TIMED_OUT,
      in_reply_to: null,
      state: 'refused',
      note: QUEUE_NOTE,
    },
  ],
})
const SENT = { msg_id: 'bb'.repeat(16), to: 'codex-x', status: 'delivered', note: '' }

test('the mod reads back each frame that peermesh writes', () => {
  for (const frame of FRAMES) {
    expect(parseFrame(frame.text)).toEqual(frame.items)
  }
  expect(parseFrame('Another Claude session sent a message:\nhello')).toBe(null)
  // A frame that lost a message is not drawn in part.
  expect(parseFrame((FRAMES[2]?.text ?? '').replace('[2] ', '[2] not '))).toBe(null)
})

test('a peer message is a short row on every surface', async $ => {
  const text = 'Another Claude session sent a message:\n' + (FRAMES[0]?.text ?? '')
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({
      plugin: 'peermesh',
      surface,
      component: 'UserMessage',
      props: { text, origin: { kind: 'peer' }, isExpanded: false },
    })
    expect(await ui.find({ type: 'Text', text: /rotom-infra/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Check docs\/schema\.md\./ })).toBeDefined()
    // The lines for the agent are not in the row. ctrl+o shows them.
    expect(await ui.find({ type: 'Text', text: /user authority|peers_reply/ })).toBeUndefined()
    await ui.unmount()
  }
})

test('a sent message shows the state that the ledger has, not a guess', async ($, on) => {
  on('process.run', () => ({
    value: { exitCode: 0, stdout: SNAPSHOT, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  const call = (output?: string) => ({
    tool_use_id: 'toolu_1',
    tool: SEND,
    input: { to: ['codex-x'], body: 'Run the tests.', kind: 'request' },
    isRunning: output === undefined,
    isErrored: false,
    isInterrupted: false,
    output,
  })
  const mount = (output?: string) =>
    $.ui.mount({
      plugin: 'peermesh',
      surface: 'terminal',
      component: 'ToolGroup',
      props: { calls: [call(output)], isActive: true, isExpanded: false },
    })
  const running = await mount()
  expect(await running.find({ type: 'Text', text: /pending/ })).toBeDefined()
  await running.unmount()
  const done = await mount(JSON.stringify({ result: [{ ...SENT, status: 'pending' }] }))
  expect(await done.find({ type: 'Text', text: /codex-x/ })).toBeDefined()
  expect(await done.find({ type: 'Text', text: /pending/ })).toBeDefined()
  expect(await done.find({ type: 'Text', text: /read|seen/ })).toBeUndefined()
  await done.unmount()
  // An interrupted send has no result. Its row says so and does not show a state of a send.
  const cut = await $.ui.mount({
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'ToolGroup',
    props: {
      calls: [{ ...call('Interrupted by user'), isInterrupted: true }],
      isActive: false,
      isExpanded: false,
    },
  })
  expect(await cut.find({ type: 'Text', text: /interrupted/ })).toBeDefined()
  expect(await cut.find({ type: 'Text', text: /can be delivered/ })).toBeDefined()
  expect(await cut.find({ type: 'Text', text: /delivered  |pending|answered/ })).toBeUndefined()
  await cut.unmount()
})

test('a group with other tool calls keeps the line that Claude Code draws for it', async ($, on) => {
  // The engine draws the group beneath the mod. This stands for its line.
  on('ui.render', { component: 'ToolGroup' }, ($$, e) => {
    const { Text } = $$.ui.resolve(e)
    const failed = e.props.calls.filter(call => call.isErrored).length

    return <Text>the engine line: {failed} failed</Text>
  })
  const state = { isRunning: false, isInterrupted: false }
  const ui = await $.ui.mount({
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'ToolGroup',
    props: {
      calls: [
        { ...state, tool_use_id: 'toolu_1', tool: 'Bash', input: { command: 'make' }, isErrored: true, output: 'Error' },
        {
          ...state,
          tool_use_id: 'toolu_2',
          tool: SEND,
          input: { to: ['codex-x'], body: 'The build fails.', kind: 'info' },
          isErrored: false,
          output: JSON.stringify({ result: [SENT] }),
        },
      ],
      isActive: false,
      isExpanded: false,
    },
  })
  expect(await ui.find({ type: 'Text', text: /The build fails\./ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /the engine line: 1 failed/ })).toBeDefined()
  await ui.unmount()
})

test('the pane lists the sessions, opens one and sends a message as this session', async ($, on) => {
  const runs: { argv: readonly string[]; stdin?: string; session?: string }[] = []
  on('process.run', (_$, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin, session: e.init?.env?.CLAUDE_CODE_SESSION_ID })
    const stdout = e.argv.includes('watch') ? SNAPSHOT : JSON.stringify([SENT])

    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // The host answers these calls in a session. Nothing answers them in a test.
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  const opened: string[] = []
  on('ui.open', (_$, e) => {
    opened.push(e.id)

    return { value: { isPlaced: true } }
  })
  const session = mock.session(on)
  await $.command.run({
    command: 'mesh',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 120 },
  })
  expect(opened).toEqual(['peermesh'])
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'peermesh',
      surface,
      component: 'Pane',
      requestId: 'peermesh',
      props: {
        title: 'peermesh',
        isFocused: true,
        bodyColumns: 60,
        placement: 'dock',
        scroll: { offset: 0, bodyRows: 30 },
        view: {},
      },
    })
    expect((await ui.find({ key: `peer:${PEER}` }))?.text).toMatch(/codex-x.*idle.*1 waiting/)
    expect((await ui.find({ key: `thread:${ASKED}` }))?.text).toMatch(/codex-x.*delivered/)

    await ui.press({ key: `peer:${PEER}` })
    expect(await ui.find({ type: 'Text', text: /fix\/cart/ })).toBeDefined()
    const before = runs.length
    await ui.input({ key: `to:${PEER}`, text: 'Use main.' })
    const sent = runs.slice(before).find(run => run.argv.includes('send'))
    // The id, not the name: a different session can take the name after the pane drew it.
    expect(sent?.argv.slice(1)).toEqual(['--json', 'send', PEER, '--kind', 'request', '--body', '-'])
    expect(sent?.stdin).toBe('Use main.')
    expect(sent?.session).toBe('session-1')
    expect(await ui.find({ type: 'Text', text: /delivered · codex-x · bbbbbbbb/ })).toBeDefined()

    await ui.press({ key: 'back' })
    await ui.press({ key: `thread:${ASKED}` })
    expect(await ui.find({ type: 'Text', text: /Which branch has the cart fix\?/ })).toBeDefined()
    const beforeReply = runs.length
    await ui.input({ key: `reply:${ASKED}`, text: 'main' })
    const reply = runs.slice(beforeReply).find(run => run.argv.includes('reply'))
    expect(reply?.argv.slice(1)).toEqual(['--json', 'reply', ASKED, '--body', '-'])
    await ui.press({ key: 'back' })
    await ui.unmount()
  }
  // `/mesh <session>` opens the pane on that session.
  await $.command.run({
    command: 'mesh',
    args: 'codex-x',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 80 },
  })
  const named = await $.ui.mount({
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'peermesh',
    props: {
      title: 'peermesh',
      isFocused: true,
      bodyColumns: 40,
      placement: 'inline',
      scroll: { offset: 0, bodyRows: 16 },
      view: {},
    },
  })
  expect(await named.find({ key: `to:${PEER}` })).toBeDefined()
  await named.unmount()
  // The agent of this session reads what its user sent in its name.
  const notes = session.appended().map(row => JSON.stringify(row.message.content))
  expect(notes.filter(note => note.includes('The user sent a message')).length).toBe(4)
  expect(notes.some(note => note.includes('> Use main.'))).toBe(true)
})

test('a send that gives no answer is not sent again', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  // The command runs too long, then it ends without a result, then it refuses the message.
  const answers = [
    { deny: 'the command is still running' },
    { value: { exitCode: 1, stdout: '', stderr: 'Killed', isStdoutTruncated: false, isStderrTruncated: false } },
    {
      value: {
        exitCode: 2,
        stdout: '',
        stderr: 'The session codex-x is not live.\n',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    },
  ]
  let sends = 0
  on('process.run', (_$, e) => {
    if (e.argv.includes('send')) {
      sends += 1

      return answers[sends - 1] ?? { deny: 'no answer is left' }
    }

    return {
      value: { exitCode: 0, stdout: SNAPSHOT, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })
  const session = mock.session(on)
  await $.command.run({
    command: 'mesh',
    args: 'codex-x',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 120 },
  })
  const ui = await $.ui.mount({
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'peermesh',
    props: {
      title: 'peermesh',
      isFocused: true,
      bodyColumns: 60,
      placement: 'dock',
      scroll: { offset: 0, bodyRows: 30 },
      view: {},
    },
  })
  for (const attempt of [1, 2]) {
    await ui.input({ key: `to:${PEER}`, text: 'Use main.' })
    expect(sends).toBe(attempt)
    expect(await ui.find({ type: 'Text', text: /gave no answer.*can be delivered/ })).toBeDefined()
  }
  // A refusal is certain: `peers` gave the reason and sent nothing.
  await ui.input({ key: `to:${PEER}`, text: 'Use main.' })
  expect(await ui.find({ type: 'Text', text: /not sent · The session codex-x is not live\./ })).toBeDefined()
  // No message id is known, so the agent gets no row that claims a send.
  expect(session.appended().length).toBe(0)
  await ui.unmount()
})

test('a refused send keeps the warning of the transport', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_$, e) => {
    const refused = [{ msg_id: TIMED_OUT, to: 'codex-x', status: 'refused', note: QUEUE_NOTE }]
    const stdout = e.argv.includes('send') ? JSON.stringify(refused) : SNAPSHOT

    return {
      value: { exitCode: e.argv.includes('send') ? 1 : 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })
  const session = mock.session(on)
  await $.command.run({
    command: 'mesh',
    args: 'codex-x',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 120 },
  })
  const pane = {
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'peermesh',
    props: {
      title: 'peermesh',
      isFocused: true,
      bodyColumns: 60,
      placement: 'dock',
      scroll: { offset: 0, bodyRows: 30 },
      view: {},
    },
  } as const
  const ui = await $.ui.mount(pane)
  await ui.input({ key: `to:${PEER}`, text: 'Run the tests.' })
  expect(await ui.find({ type: 'Text', text: /refused · codex-x · cccccccc · .*Do not resend/ })).toBeDefined()
  expect(JSON.stringify(session.appended().map(row => row.message.content))).toMatch(/Do not resend/)
  // The thread shows the same warning later.
  await ui.press({ key: `thread:${TIMED_OUT}` })
  expect(await ui.find({ type: 'Text', text: /Do not resend/ })).toBeDefined()
  await ui.unmount()
})

test('a second Enter while a send runs does not send again', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  let sends = 0
  let finish = () => {}
  const running = new Promise<void>(resolve => {
    finish = resolve
  })
  on('process.run', async (_$, e) => {
    const isSend = e.argv.includes('send')
    if (isSend) {
      sends += 1
      await running
    }
    const stdout = isSend ? JSON.stringify([SENT]) : SNAPSHOT

    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  await $.command.run({
    command: 'mesh',
    args: 'codex-x',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 120 },
  })
  const ui = await $.ui.mount({
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'peermesh',
    props: {
      title: 'peermesh',
      isFocused: true,
      bodyColumns: 60,
      placement: 'dock',
      scroll: { offset: 0, bodyRows: 30 },
      view: {},
    },
  })
  const first = ui.input({ key: `to:${PEER}`, text: 'Use main.' })
  const second = ui.input({ key: `to:${PEER}`, text: 'Use main.' })
  await second
  expect(sends).toBe(1)
  finish()
  await first
  expect(sends).toBe(1)
  expect(await ui.find({ type: 'Text', text: /delivered · codex-x/ })).toBeDefined()
  await ui.unmount()
})

test('an event during a read makes one more read', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  let reads = 0
  let finish = () => {}
  const running = new Promise<void>(resolve => {
    finish = resolve
  })
  on('process.run', async () => {
    reads += 1
    if (reads === 1) {
      await running
    }

    return {
      value: { exitCode: 0, stdout: SNAPSHOT, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })
  const open = () =>
    $.command.run({
      command: 'mesh',
      args: '',
      origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 120 },
    })
  const first = open()
  // The first read is not complete. Its answer can be older than this second event.
  await open()
  expect(reads).toBe(1)
  finish()
  await first
  expect(reads).toBe(2)
})

test('a sent message that the conversation refused is still reported as sent', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_$, e) => {
    const stdout = e.argv.includes('send') ? JSON.stringify([SENT]) : SNAPSHOT

    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.append', { door: 'note' }, () => ({ deny: 'this run takes no rows' }))
  await $.command.run({
    command: 'mesh',
    args: 'codex-x',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 120 },
  })
  const ui = await $.ui.mount({
    plugin: 'peermesh',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'peermesh',
    props: {
      title: 'peermesh',
      isFocused: true,
      bodyColumns: 60,
      placement: 'dock',
      scroll: { offset: 0, bodyRows: 30 },
      view: {},
    },
  })
  await ui.input({ key: `to:${PEER}`, text: 'Use main.' })
  expect(await ui.find({ type: 'Text', text: /delivered · codex-x · bbbbbbbb · .*has no record/ })).toBeDefined()
  await ui.unmount()
})

test('the timer does not start a read while a read runs', async ($, on) => {
  mock.env(on, { HOME: '/home/user' })
  on('session.id', () => ({ value: 'session-1' }))
  on('env.set', () => ({ value: undefined }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('ui.panes', () => ({ value: [{ id: 'peermesh', title: 'peermesh', isPlaced: true, isShown: true, isFocused: false }] }))
  const clock = mock.clock(on)
  let reads = 0
  let finish = () => {}
  const running = new Promise<void>(resolve => {
    finish = resolve
  })
  on('process.run', async () => {
    reads += 1
    await running

    return {
      value: { exitCode: 0, stdout: SNAPSHOT, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })
  await $.session.start({ cwd: '/w/rotom', surface: 'terminal', isInteractive: true })
  // The pane is open and the first read is slow: five ticks pass during it.
  await clock.advance(15000)
  expect(reads).toBe(1)
  finish()
  await clock.settle()
  await clock.advance(3000)
  expect(reads).toBe(2)
})
