import { expect, mock, test } from 'claude-code/testing'

import { parseFrame } from '../hooks/frame'
import { FRAMES } from './frames'

const SEND = 'mcp__peermesh__peers_send'
const ME = 'claude:me'
const PEER = 'codex:x'
const ASKED = 'aa'.repeat(16)

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
    expect(sent?.argv.slice(1)).toEqual(['--json', 'send', 'codex-x', '--kind', 'request', '--body', '-'])
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
