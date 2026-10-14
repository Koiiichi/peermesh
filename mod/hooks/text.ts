// Text helpers for the rows that the mod draws. They do not use the engine.

import type { Message } from '../types'

/** Text from a peer on one line, with no control character, cut to `width` cells. */
export function oneLine(text: string, width: number): string {
  // A body can hold terminal control sequences. They must not reach the screen.
  const flat = text.replace(/[\u0000-\u001f\u007f-\u009f]+/g, ' ').replace(/\s+/g, ' ').trim()

  return flat.length > width ? flat.slice(0, Math.max(1, width - 1)) + '…' : flat
}

// A body line is cut after this many characters. One line of 8192 bytes is a valid body.
const LINE_CHARS = 400

/**
 * The lines of a body, with no control character, cut after `rows` lines. `more` is the
 * number of lines that are cut in full or in part.
 */
export function bodyLines(text: string, rows: number): { lines: string[]; more: number } {
  const all = text
    .split('\n')
    .map(line => line.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, '').replace(/\t/g, '  '))
  const lines = all.slice(0, rows).map(line => (line.length > LINE_CHARS ? line.slice(0, LINE_CHARS) + '…' : line))
  const long = all.slice(0, rows).filter(line => line.length > LINE_CHARS).length

  return { lines, more: all.length - lines.length + long }
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
