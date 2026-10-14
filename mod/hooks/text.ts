// Text helpers for the rows that the mod draws. They do not use the engine.

import type { Message } from '../types'

/** Text from a peer on one line, with no control character, cut to `width` cells. */
export function oneLine(text: string, width: number): string {
  // A body can hold terminal control sequences. They must not reach the screen.
  const flat = text.replace(/[\u0000-\u001f\u007f-\u009f]+/g, ' ').replace(/\s+/g, ' ').trim()

  return flat.length > width ? flat.slice(0, Math.max(1, width - 1)) + '…' : flat
}

/**
 * The first `rows` lines of a body as they were written, with no control character. `more` is
 * the number of lines with text that are not shown. `isCut` is true when a shown line lost its
 * end: a line shows `lineChars` characters at most, and the lines show `chars` in all.
 *
 * The lines keep their indents and their empty lines. Empty lines at the start, at the end and
 * at the cut are left out, and a run of empty lines is one empty line, so the room is not
 * spent on nothing.
 */
export function bodyLines(
  text: string,
  rows: number,
  lineChars: number,
  chars: number = rows * lineChars,
): { lines: string[]; more: number; isCut: boolean } {
  const all = text
    .split('\n')
    .map(line => line.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, '').replace(/\t/g, '  ').trimEnd())
    .filter((line, at, list) => line !== '' || (at > 0 && list[at - 1] !== ''))
  while (all[all.length - 1] === '') {
    all.pop()
  }
  const lines: string[] = []
  let left = chars
  let isCut = false
  for (const line of all.slice(0, rows)) {
    const room = Math.min(lineChars, left)
    if (room <= 0) {
      break
    }
    isCut = isCut || line.length > room
    lines.push(line.length > room ? line.slice(0, room) + '…' : line)
    left -= Math.min(line.length, room)
  }
  while (lines[lines.length - 1] === '') {
    lines.pop()
  }

  return { lines, more: all.slice(lines.length).filter(line => line !== '').length, isCut }
}

export function pad(text: string, width: number): string {
  const cut = text.length > width ? text.slice(0, Math.max(1, width - 1)) + '…' : text

  return cut.padEnd(width)
}

/** The local time of a message as hour:minute. */
export function clock(sentAt: number): string {
  const when = new Date(sentAt * 1000)

  return `${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}`
}

// The words that a person reads for the states of the ledger. "delivered" means that the
// runtime of the peer has the message. No runtime reports that an agent read a message.
const WORDS: Record<string, string> = {
  pending: 'pending',
  sending: 'pending',
  injected: 'delivered',
  delivered: 'delivered',
  queued: 'delivered',
  answered: 'answered',
  done: 'done',
  refused: 'failed',
  undelivered: 'failed',
}

export function stateWord(state: string): string {
  return WORDS[state] ?? state
}

export function stateColor(state: string): string | undefined {
  const word = stateWord(state)
  if (word === 'pending' || word === 'interrupted' || word === 'no result') {
    return 'warning'
  }
  if (word === 'failed' || word === 'error') {
    return 'error'
  }

  return word === 'delivered' || word === 'answered' || word === 'done' ? 'success' : undefined
}

/** The newest message of each thread, newest thread first, with the size of the thread. */
export function threads(messages: readonly Message[]): { last: Message; count: number }[] {
  const byThread = new Map<string, { last: Message; count: number }>()
  for (const message of messages) {
    const seen = byThread.get(message.thread)
    byThread.set(message.thread, { last: message, count: (seen?.count ?? 0) + 1 })
  }

  return [...byThread.values()].sort((a, b) => b.last.sentAt - a.last.sentAt)
}

/** The folder of a session with the home directory written as "~". */
export function shortDir(dir: string): string {
  return dir.replace(/^\/(?:Users|home)\/[^/]+/, '~')
}

/**
 * True for a delivery note that a person must see: peermesh ends such a note with "Do not
 * resend" when a message can be with the peer although the send did not confirm it.
 */
export function warnsOfResend(note: string): boolean {
  return /do not resend/i.test(note)
}

// The colors that tell the peers apart. They are the theme colors that carry no state and that
// a terminal of 256 colors shows as three clearly different colors. More names share a color.
const ACCENTS = ['claude', 'planMode', 'merged'] as const

/** The color of a peer: the same color for the same name in each row and each session. */
export function accent(name: string): string {
  let sum = 0
  for (const char of name) {
    sum = (sum * 31 + (char.codePointAt(0) ?? 0)) % 9973
  }

  return ACCENTS[sum % ACCENTS.length] ?? 'claude'
}
