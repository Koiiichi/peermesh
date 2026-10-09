# Changelog

## 0.2.1 — 2026-10-09

### Changed

- The loop limit counts quick exchanges only. A hop counts when a message answers its parent within 2 minutes; a slower answer starts the count again. Long collaborations between two sessions no longer reach the limit, and fast loops still stop after 8 messages.
- The per-thread cap is a flood limit: one sender can add at most 8 messages to one thread in 10 minutes.
- Claude Code agents are told to send messages to other sessions only with `peers_send` and `peers_reply`, and to keep `SendMessage` for agents that they started. Refusals tell the agent not to send the message through a different channel.

### Fixed

- A request that reaches a Claude Code session at the end of its turn continues the turn as context, not as a "Stop hook error" that showed the whole message to the user. Codex still continues the turn with a block decision, its only way.

Run `peers install` again to update the instruction block in `~/.claude/CLAUDE.md`.

## 0.2.0 — 2026-10-09

Peer messages reach a busy session at its next tool call, and a peer message no longer pushes an agent's final report out of view.

### Upgrade

1. Pull and reinstall: `git pull && uv tool install --editable .` (an editable install only needs the pull).
2. Run `peers install` again. It adds a `PostToolUse` hook and updates the other hooks, the instruction blocks and the Codex allow rule.
3. In Codex, trust the changed hooks once.

Until step 2, sessions keep the 0.1.0 delivery. The first send after the upgrade moves the existing ledger into `~/.peermesh/ledger-archive/`; no message is lost. Do not go back to 0.1.0 after that send: 0.1.0 misreads the new ledger entries, and `peers reply` and `peers log` fail or show empty bodies.

### Added

- Delivery at tool boundaries. Messages for a busy session wait in its inbox (`~/.peermesh/inbox/`), and the post-tool hook injects them at the next tool call, in send order and exactly once. This reaches a busy Codex turn without `turn/steer`, so the Codex app and IDE extensions get messages mid-turn.
- Protection for the final report. A message that can reach an agent after it finished its task asks it to restate its final report as its last message. A turn continues for peer messages at most once.
- `peers track MSG` and the `peers_track` tool: the delivery history of one message. The states are pending, injected (with the hook that did it), accepted, queued, undelivered, acknowledged (the peer replied) and acted (the peer replied with `--done`).
- `peers reply --done` and `done` on `peers_reply`, to report that the requested work is complete.
- `peers status` reports the messages waiting in the inbox of a peer.
- `peers doctor` checks the post-tool hook of each runtime.

### Changed

- An `info` message to an idle session waits for its next prompt or tool call; it no longer starts a turn. Requests, handoffs, review requests and replies still wake an idle session. Agents are told to send finished work that a peer waits for as `handoff`.
- At the end of a turn, waiting `info` keeps waiting, and a waiting request continues the turn once.
- A reply to a broadcast is allowed. It goes to the broadcast sender only.
- A `peers send` to a peer that messaged you in the last 15 minutes continues that thread, so it counts toward the 8-hop limit.
- The rate limit counts threads, not messages: a sender can start at most 6 threads with one peer per 10 minutes, and can add at most 8 messages to one thread.
- The ledger stores each message body once. Past 1 MiB of growth, final and old entries move to gzip archives; the newest 30 archives are kept.
- The Stop hook timeout is 45 seconds.

### Fixed

- Branch, working directory and worktree refresh on every prompt; before, they were fixed at session start.
- A closed or archived Codex thread leaves the peer list. A Codex app-server hosts many threads, so its running process did not show that a thread was open.
- A Codex session hosted by the app or an IDE extension is labelled `app-server`, not `daemon`, and no longer claims that `--urgent` can steer it.
- Refusals (rate limit, acknowledge-only replies, unknown targets, invalid messages) appear in the ledger and in `peers log`.
- One `ps` call checks all peers, and each send reads a small active ledger.
- A Claude session gets the peermesh rules at session start even when Claude Code has not written its inbox socket yet.
- Seen-set files and inboxes of ended sessions are removed. Waiting messages of an ended session are marked undelivered.

## 0.1.0 — 2026-10-06

First public version.

- `peers` CLI (`list`, `send`, `reply`, `status`, `whoami`, `log`) and the MCP tools `peers_list`, `peers_send`, `peers_reply` and `peers_status` for Claude Code.
- Session hooks for Claude Code and Codex that register each session and tell it which peers are live in the same repository.
- Delivery through the Claude Code inbox socket and `codex queue`, and `turn/steer` for urgent requests to threads that the Codex app-server daemon hosts.
- A message frame that marks every message as from an agent, not from the user, with body lines quoted.
- Limits: 8 hops per thread, 6 messages to one peer per 10 minutes, 8 KB bodies, no acknowledge-only replies, no replies to broadcasts.
- `peers install`, `uninstall` and `doctor`, and a two-session demo.
