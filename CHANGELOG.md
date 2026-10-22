# Changelog

## Unreleased

### Fixed

- A request to a Codex session whose last turn ended with an error starts its next turn. When Codex ended a turn with an error, no Stop hook reached peermesh, so peermesh kept the session busy and left each later request in its inbox until someone typed into the session. peermesh now also reads the last turn event in the session's rollout file, the way it reads Claude Code's status file, and the newer of the two wins.
- The Claude Code mod's message rows fill the transcript column in the Claude desktop app, VS Code and the mobile app. A row block had a width in character cells, the transcript width that the surface reported or 80 when it reported none, so in a narrower window its lines ran past the right edge and were cut there. On those surfaces the first line of a row also wraps, so the state and the option go under the sender in a narrow column. The terminal keeps the cell width, which its first line needs to put the state and the time at the right edge.
- A Claude desktop session that the app paused stays in the mesh. The Claude app ends the process of an idle session when it needs memory, and runs the session again later under the same session id. peermesh removed such a session when its process ended, so other sessions were told that it was not live, and messages that waited for it were marked undelivered. Now `peers list` shows it as `paused` while the app keeps it unarchived, a message to it waits with a note that says why, and the session reads the message at its next prompt after the app runs it again.
- Hooks that run at the same time no longer lose a session's registration. A hook of a process that had already ended, or a late `SessionEnd` of a Codex thread that opened again in the same app-server, could remove or overwrite the newer registration, and the messages that waited for it were marked undelivered. Each change to a record now holds the registry lock and checks that the record is still the registration that the hook read, and a `SessionEnd` ends only a registration stored before the end began. A registry record that cannot be read or has a field of the wrong type stays as it is, and it no longer stops `peers list`, a send or pruning for the other sessions.
- `peers install`, `peers uninstall` and `peers doctor` read and write `CLAUDE.md`, `AGENTS.md`, `settings.json` and `hooks.json` as UTF-8. Under a locale whose encoding cannot read a character in those files, such as `en_US.US-ASCII`, `peers install` refused a valid `settings.json` as "not a valid JSON object", and a write could change text outside ASCII.
- peermesh reads Claude Code's session records as UTF-8. Under a locale with another encoding, such as `en_US.ISO8859-1`, a session name or an inbox socket path with a character outside ASCII was read wrong: a name that the user gave, such as `café`, became `cafÃ©`, and a message to a session with such a socket path could not be delivered.

## 0.5.5 — 2026-10-21

A message to a Codex session works when the folder of the sending session was deleted, and `peers list` works in your own terminal.

### Upgrade

1. Pull: `git pull`. No reinstall is necessary. The hooks of `peers install` and of the plugin, and the Codex rule, are the same as in 0.5.4, so Codex does not ask you to trust the hooks again.
2. Nothing else for a session that started with 0.5.4: it gets this version at its next peermesh tool call. Reconnect a session that started before 0.5.4: in Claude Code, run `/mcp`, select `peermesh` and choose Reconnect.
3. If you use the Codex plugin, run `codex plugin add peermesh@peermesh` to show the new version; its hooks and tools are unchanged.

### Changed

- `peers list` works outside an agent session, for example in your own terminal or in a `!` command in Claude Code: it lists every live session by name. Before, it stopped with "This process is not inside a Claude Code or Codex session". Outside a session, `--scope repo` and `--scope worktree` still give an error, because they need a session to compare with.

### Fixed

- A message to a Codex session no longer fails with "codex queue failed: Error: failed to resolve config cwd: No such file or directory" when the folder that the sending session started in was deleted. peermesh now runs `codex queue` in the folder of the receiving Codex session, or in your home folder when that folder does not exist either, and runs `codex app-server proxy` in your home folder. The root folder is the last fallback. When the proxy cannot start, an urgent message goes to the queue.

## 0.5.4 — 2026-10-19

A running session gets an upgrade at its next peermesh tool call, with no reconnect.

### Upgrade

1. Pull: `git pull`. No reinstall is necessary. The hooks of `peers install` and of the plugin, and the Codex rule, are the same as in 0.5.3, so Codex does not ask you to trust the hooks again.
2. Reconnect the peermesh MCP server in each running session one last time, or restart the session: in Claude Code, run `/mcp`, select `peermesh` and choose Reconnect. A server that started before 0.5.4 runs its old code. From 0.5.4 on, each tool call runs the installed peermesh.
3. If you use the Codex plugin, run `codex plugin add peermesh@peermesh` to show the new version; its hooks and tools are unchanged.

### Changed

- Each call of a peermesh MCP tool runs in a new process of the installed code, so a running session gets an upgrade at its next tool call, with no reconnect. A new process starts in a few hundredths of a second. The list of tools still comes from the MCP server that the session started with, so the first tool result after an upgrade says that the list can be out of date, one time. If the installed peermesh cannot run, for example while it is being reinstalled, the call runs the code that the server started with, and the result says so. A server that started before this version still runs its old code; reconnect it, or restart the session, one more time.

## 0.5.3 — 2026-10-19

A peermesh MCP server that runs older code says so, and agents see the text of each tool error.

### Upgrade

1. Pull: `git pull`. No reinstall is necessary. The hooks of `peers install` and of the plugin, and the Codex rule, are the same as in 0.5.2, so Codex does not ask you to trust the hooks again.
2. Reconnect the peermesh MCP server in each running session once, or restart the session: in Claude Code, run `/mcp`, select `peermesh` and choose Reconnect. A server that started before 0.5.3 has no version check, so it cannot tell you that it is old. From 0.5.3 on, a server that runs older code than the installed peermesh says so in each tool result.
3. If you use the Codex plugin, run `codex plugin add peermesh@peermesh` to show the new version; its hooks and tools are unchanged.

### Added

- A peermesh MCP server that runs older code than the installed peermesh says so: each tool result gets a `notice` field, and each tool error a sentence, that names both versions and tells the agent to ask you to reconnect the server. A session keeps its server across an upgrade, so a session could act on old behavior with no sign of it.

### Fixed

- An error of a peermesh tool reaches the agent with its text, for example `Error executing tool peers_list: The scope is not valid. Use 'repo', 'worktree' or 'all'.` The MCP library showed only `Error executing tool peers_list` for each error, so an agent did not see why a send or a list failed.

## 0.5.2 — 2026-10-17

Sessions find each other by the names that you gave them.

### Upgrade

1. Pull: `git pull`. No reinstall is necessary. The hooks of `peers install` and of the plugin, and the Codex rule, are the same as in 0.5.1, so Codex does not ask you to trust the hooks again.
2. A Claude Code session keeps its old MCP server, and so its old `peers_list`, until it restarts. To update it without a restart, run `/mcp` in the session, select `peermesh` and choose Reconnect. Hooks run the new code at once.
3. If you use the Codex plugin, run `codex plugin add peermesh@peermesh` to show the new version; its hooks and tools are unchanged.

### Fixed

- A Claude Code session that you named when it started, as the Claude desktop app does, now uses that name in peermesh. peermesh read only a name that Claude Code marked as a rename, so such a session kept a name like `claude-alpha-d5`, and other sessions could not find it by the name you gave it.
- `peers_list` and `peers --json list` show the name that you gave a session, with the name that peermesh gave it as `auto_name`. They showed only the name from peermesh, so an agent did not see a rename. Both names still work as an address.
- A name goes to a session only while no other live session wants the same name. When two sessions want it, both keep their automatic names, and a message to the shared name is refused with both automatic names in the note. So an address never moves from one live session to another. A new automatic name never repeats the automatic name of a renamed session.

## 0.5.1 — 2026-10-15

`peers doctor` knows the Codex plugin.

### Upgrade

Pull: `git pull`. No reinstall is necessary. The hooks of `peers install` and of the plugin, and the Codex rule, are the same as in 0.5.0, so Codex does not ask you to trust the hooks again. If you use the Codex plugin, run `codex plugin add peermesh@peermesh` to show the new version; its hooks and tools are unchanged.

### Fixed

- `peers doctor` knows the Codex plugin. With the plugin enabled, it checks that each Codex hook event has a hook that runs: the hook of `peers install` for an event that it serves, because the plugin hook then does nothing, and the plugin hook for every other event. A hook runs when its trust record exists and is not disabled. It names each event with no such hook. It asks for the Codex rule only while the session-start or prompt hook of `peers install` serves Codex, because such a session is told to use the shell commands. A `hooks.json` with no hooks, such as `{"hooks": {}}`, no longer counts as installed hooks.

### Changed

- The README recommends the Claude Code mod right after the install, in "Use the mod in Claude Code".

## 0.5.0 — 2026-10-14

Codex can get peermesh as a plugin, with the MCP tools that Claude Code uses, and the line for a message in the middle of a turn shows its text.

### Upgrade

1. Pull: `git pull`. No reinstall is necessary. The hooks, the Codex rule and the instruction blocks of `peers install` are the same as in 0.4.1, so Codex does not ask you to trust the hooks again.
2. A Claude Code session that started before the pull keeps the old MCP server until it restarts. Hooks run the new code at once.
3. To use the Codex plugin, which is optional, follow "Install the Codex part as a plugin" in the README.

### Added

- A Codex plugin in `codex/`, which you can use instead of the Codex part of `peers install`. It runs the same hooks and gives Codex the peermesh MCP tools in place of the shell commands. Install it with `codex plugin marketplace add "$PWD"` and `codex plugin add peermesh@peermesh` in the checkout. The README shows how to update and remove it.
- The MCP server finds the Codex thread of each tool call in the metadata that Codex sends with the call.

### Changed

- The line that a hook shows you when it gives the agent a message mid-turn now has the sender, the kind and the start of the text: `peermesh: ← shop-db · info  The orders table gets a new column, shipped_at.` It was `peermesh: gave the agent info from shop-db`, with no text. The line stays one line of bounded length: a body with several lines is put on one line, control characters show as escapes, and a batch adds `(+1 more)`. The agent still gets each message in full. Codex CLI shows the line as `↳ Hook · …`, and Claude Code without the mod as `PostToolUse:Bash says: …`.
- The README states what session identity guarantees. The threads of one Codex process can act as each other when a command runs outside the Codex sandbox; this was true before the plugin.

## 0.4.1 — 2026-10-14

The message rows of the Claude Code mod read as a conversation.

### Upgrade

Pull: `git pull`, then run `/reload-plugins` in each open Claude Code session. No reinstall is necessary.

### Changed

- **One line for the sender, the text below it.** The arrow and the name of the peer come first, in a color of their own for each peer, and the time is at the right end.
- **A reply quotes the message that it answers.** The quote is above the reply, with a bar, dim and in italics.
- **Less on the first line.** The id of a message shows only in an open row. A sent row shows `pending`, `delivered` or `failed`, and nothing after its reply came. `done` stays on a reply whose sender reports the work as complete.
- **A long message folds to its first lines, with its format kept.** In fullscreen mode, `▸ 11 more lines` in the first line opens the row and `▾ collapse` folds it. Outside fullscreen mode, ctrl+o shows the full message.
- **The line for a message in the middle of a turn is shorter:** `← shop-db · info  The orders table gets …`.

## 0.4.0 — 2026-10-13

### Upgrade

1. Pull: `git pull`. No reinstall is necessary.
2. To get the mod, which is optional, run `claude plugin marketplace add ./` and `claude plugin install peermesh@peermesh` in the checkout. Sessions that start after that have it.

### Added

- A mod for Claude Code 2.1.287 or later, in `mod/`. It draws each peer message as a short row in the conversation, with the state that the ledger has now, and `/mesh` opens a pane of the sessions and the threads. From the pane you can read a thread and send a message to a peer as your session. The mod is optional and delivery does not use it. [docs/claude-code-mod.md](docs/claude-code-mod.md) shows how to install it and what it cannot draw.
- `peers --json watch` prints the sessions, the 100 newest messages with their states, and the id of the session that asks. The mod reads this, and any other program that draws peermesh can.

### Changed

- In a Claude Code session that has the mod, the hooks do not write the line `peermesh: gave the agent ...`, because the mod draws the message. A session without the mod shows the line as before.

## 0.3.2 — 2026-10-13

### Fixed

- `--urgent` steers a busy Codex session on codex-cli 0.162 again. That version serves the daemon control socket as WebSocket, and the newline-delimited JSON that peermesh sent through `codex app-server proxy` was dropped. Each urgent message waited 10 seconds and then ran as the next turn. peermesh now speaks WebSocket to the socket and keeps the proxy for older versions.

## 0.3.1 — 2026-10-13

### Changed

- Ephemeral Codex threads, such as the one-time review threads of the Codex plugin for Claude Code, are not registered as peers by the hooks. They have no rollout file, so they cannot be woken, and they stayed in the peer list until their host process ended. Such a thread still registers if its agent runs a `peers` command.
- `peers statusline` shows message status, not the session name and the peer count: the messages that wait for the session, and the messages it sent in the last hour as `pending`, `delivered`, `answered` or `failed`. `peers install` does not add the segment: set `peers statusline` as the `statusLine` command of Claude Code, or add it as a custom command widget in ccstatusline, a separate tool. The README shows both.

## 0.3.0 — 2026-10-13

Peer messages are shorter, a hook tells you when it gives the agent a message, and three commands let you watch the sessions.

### Upgrade

1. Pull and reinstall: `git pull && uv tool install --editable . --force`. The reinstall adds the new dependency `rich`.
2. Run `peers install` again. It updates the `SessionEnd` hook timeout.
3. Restart long-running Claude Code sessions when convenient. Until then they keep working and get full message ids, because their MCP server still runs the older code.

### Changed

- Peer messages are shorter. One header states once that the messages are not from the user; each message has one line with its kind, sender and short id, then its quoted body. The frame no longer shows thread, hop or session ids; the ledger keeps them.
- Messages that a hook injects together form one numbered batch with one set of reply rules.
- `peers reply` and `peers track` accept the 8-character message id that the frame shows. An id prefix that matches two messages is refused. A Claude Code session whose MCP server started with an older version gets full ids until it restarts.
- A peer message that reaches an agent after its task asks it to restate the final report only if it does work for the message; otherwise it writes one short sentence.
- `peers log` shows one line per message in local time, fits the terminal width, and colors the outcome when a person reads it in a terminal. Agent shells and `NO_COLOR` get plain text.

### Added

- `peers watch`: a live view of all sessions and the newest messages with their state. It needs the new dependency `rich`.
- `peers thread MSG`: one conversation in order with full bodies, archived messages included.
- `peers statusline`: a status line segment for Claude Code with the peermesh name of the session, its live peers and its waiting messages.
- A hook that gives the agent peer messages mid-turn shows the user one line, for example `peermesh: gave the agent request from claude-shop-4b`. Claude Code and Codex show hook context only to the model.

### Fixed

- Codex no longer warns at every start that the `SessionEnd` hook timeout was clamped to 3 seconds. Run `peers install` again to update the hook.

## 0.2.1 — 2026-10-09

### Added

- A Claude Code session renamed with `/rename` uses that name in peermesh: in `peers list`, in the frame of its messages, and as an address. The name that peermesh gave it still works as an address. A name that another live peer uses gets a short suffix. Codex threads keep their peermesh names, because Codex does not record which thread titles a user set.

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
