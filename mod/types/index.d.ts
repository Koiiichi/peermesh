/** One live session, as `peers --json watch` reports it. */
export type Peer = {
  id: string
  name: string
  runtime: string
  status: string
  branch: string | null
  dir: string
  waiting: number
}

/** One message of the ledger. `state` is the last thing that happened to it. */
export type Message = {
  id: string
  from: string
  fromName: string
  to: string
  toName: string
  kind: string
  body: string
  sentAt: number
  thread: string
  inReplyTo: string | null
  state: string
  /** What the transport said about the delivery, such as why it refused the message. */
  note: string
}

/** What the pane shows: the lists, one session, or one thread. */
export type View =
  | { kind: 'home' }
  | { kind: 'peer'; id: string }
  | { kind: 'thread'; id: string }

/** The answer to the last send from the pane, or why peermesh gave no data. */
export type Notice = { text: string; isError: boolean } | null

declare module 'claude-code' {
  interface PluginState {
    peermesh: {
      me: string | null
      peers: Peer[]
      messages: Message[]
      view: View
      notice: Notice
      isSending: boolean
      isCurrent: boolean
    }
  }
}
