// The text frame that peermesh gives an agent, read back into its messages.
// `render` in src/peermesh/envelope.py writes the frame; tests/frames.json holds frames that
// the Python tests keep equal to what `render` writes.

export type FrameItem = {
  kind: string
  from: string
  runtime: string
  id: string
  body: string
  isDone: boolean
}

const HEAD = /peermesh: (\d+) messages? from (?:another agent session|other agent sessions)/
const ITEM =
  /^(?:\[\d+\] )?(info|request|reply|handoff|review request) from (\S+) \((claude|codex)\) · msg ([0-9a-f]{8,32})(?: · (.*))?$/

/**
 * The messages of the first peermesh frame in `text`, or null when `text` holds no whole frame.
 *
 * peermesh quotes each body line with "> ", so a body cannot hold a line that reads as the
 * start of a message.
 */
export function parseFrame(text: string): FrameItem[] | null {
  const lines = text.split('\n')
  const start = lines.findIndex(line => HEAD.test(line))
  if (start < 0) {
    return null
  }
  const count = Number(HEAD.exec(lines[start] ?? '')?.[1])
  const items: FrameItem[] = []
  for (let i = start + 1; i < lines.length && items.length < count; i++) {
    const match = ITEM.exec(lines[i] ?? '')
    if (!match) {
      continue
    }
    const body: string[] = []
    while (i + 1 < lines.length && /^>( |$)/.test(lines[i + 1] ?? '')) {
      body.push((lines[++i] ?? '').slice(2))
    }
    items.push({
      kind: match[1] ?? '',
      from: match[2] ?? '',
      runtime: match[3] ?? '',
      id: match[4] ?? '',
      body: body.join('\n'),
      isDone: (match[5] ?? '').includes('requested work is complete'),
    })
  }

  return items.length === count ? items : null
}
