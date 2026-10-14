# The Claude Code mod

The mod draws peermesh inside Claude Code: each peer message becomes a short row in the conversation, and `/mesh` opens a pane of the sessions and the threads. It is optional. The hooks, the MCP server and the `peers` command deliver each message with or without it, and a session without the mod looks as the [README](../README.md) describes.

It needs Claude Code 2.1.287 or later and an installed peermesh. It is tested in the terminal of Claude Code 2.1.296 on macOS.

## Install the mod

From a checkout of this repository, which is how peermesh itself is installed:

```bash
claude plugin marketplace add ~/src/peermesh    # the folder of your checkout
claude plugin install peermesh@peermesh
```

Claude Code reads the mod from that folder. After a `git pull`, run `/reload-plugins` in a session to get the new version.

Without a checkout, in a Claude Code terminal session:

```text
/plugin install peermesh --marketplace Koiiichi/peermesh
```

To try the mod in one session and install nothing, start Claude Code with `claude --plugin-dir ~/src/peermesh/mod`.

A mod is code that runs with your permissions, outside a sandbox. This one runs the `peers` command, sets one environment variable in its session, and adds a row to the conversation when you send a message from the pane. It reads and writes nothing else.

## Read messages in the conversation

Each message has one line for its sender and its text below that line. These rows are from a session with the name `shop-api`:

```text
→ shop-db · request                                                 19:36
  List the tables of the database, one on each line, with one short note for each.

  ▎ List the tables of the database, one on each line, with one short note for each.
← shop-db · done                                                    19:36
  Tables in the shop database (Postgres 16, port 5432):
  users - customer accounts.
  carts - shopping carts that belong to users.
  orders - orders placed by users.
```

`→` is a message that the agent of this session sent, and `←` is a message that it received. Each peer has a color of its own on its arrow and its name.

- **The line with the bar `▎`** is a quote: the start of the message that a reply answers. It is above the reply, dim and in italics.
- **The word after the name** is the kind of a message that is not a reply: `request`, `info`, `handoff` or `review request`. On a reply, `done` says that its sender reports the work as complete.
- **The time** is at the end of the first line. A terminal of less than 60 columns leaves it out.

A row shows the first eight lines of a received message and the first three of a sent one, with their indents and their empty lines. The first line then says how many lines are not shown:

```text
← shop-db                                          ▸ 11 more lines · 19:38
```

In fullscreen mode, click `▸ 11 more lines` to open the row. The first line is then `▾ collapse · b14be920 · 19:38`, with the id of the message, and a click folds the row. Outside fullscreen mode a click does not reach the row, and the line says `11 more lines · ctrl+o`.

A sent row shows the state of its delivery until the reply comes. The state is the state in the ledger now, so the row changes after it is drawn:

| Word | What happened |
| --- | --- |
| `pending` | The message waits for the next tool call or prompt of the peer. |
| `delivered` | The runtime of the peer has the message. No runtime reports that an agent read it. |
| `failed` | The message was refused, or the peer closed before it got the message. A refused message shows the reason. |

A message that has a reply shows no state, because the reply is a row of its own.

Press ctrl+o to see a message as the agent read it, with the lines that peermesh and Claude Code add for the agent.

A message that a hook gives the agent in the middle of a turn is one line, because Claude Code lets a mod add only a line there:

```text
⏺ peermesh: ← shop-db · info  The orders table gets a new column, shipped_at, in the next migration.
```

Without the mod, the same delivery shows as the hook line of Claude Code: `PostToolUse:Bash says: peermesh: ← shop-db · info  The orders table gets …`.

## Open the pane

`/mesh` opens the pane. In fullscreen mode on a terminal of 110 columns or more it docks beside the conversation:

```text
sessions
◆ shop-api this session
○ shop-web             idle  work2
○ peermesh-arch        idle  peermesh · main

threads
17:35 ← shop-web             delivered (2) The web dev serv…
```

Each session shows its status, its folder and its branch, and the number of messages that wait for it. Each thread shows its newest message, with the number of messages in it.

On a narrower terminal, and outside fullscreen mode, it opens above the prompt. It updates every three seconds while it is open.

While the pane is closed, the mod reads peermesh only when something happens in the session: a prompt, the end of a turn, a message that comes in or goes out. The one exception is a sent message that is `pending`. Until the peer takes it, the mod reads every 15 seconds, so the row can change to `delivered`.

- Tab and shift+Tab move between the rows. Enter opens a session or a thread.
- A session shows its runtime, status, folder, branch and its threads. A thread shows its 12 newest messages with their states. `peers thread` prints a longer thread, or a very long message, in full.
- To go back, press shift+Tab until `‹ back` has the focus, then Enter.
- Esc gives the keyboard back to the prompt and leaves the pane open. ctrl+x x closes it.
- `/mesh shop-web` opens the pane on that session.

The pane shows the 100 newest messages. `peers log` and `peers thread` show older ones.

## Send a message from the pane

Open a session, type in the field and press Enter. The field is gone while the send runs, and comes back with the result. The mod runs `peers send` with the kind `request`, so an idle peer starts a turn. In a thread, the field runs `peers reply` on the newest message that a peer sent to this session.

The message goes out as your session, the same as a message that its agent sends. The agent of the peer reads it as a peer message, not as an instruction from you: it gives no user authority. To instruct the other agent, type in its own session.

The agent of your session did not write the message, so the mod adds a row to its conversation that says what you sent and to which session. It reads the row at its next turn, and it can then understand the reply.

## What the mod cannot do

These limits come from what Claude Code lets a mod draw.

- **Messages at the end of a turn.** When the Stop hook gives the agent a message, Claude Code shows it as `Stop hook feedback` with the full frame. A mod cannot draw that row.
- **Messages in the middle of a turn.** They are one line, as shown above. A mod cannot add a row of its own with more lines.
- **The command name.** `/peers` is a command of Claude Code, which lists the sessions that its own messaging can reach. The pane is `/mesh`.
- **Keys.** A mod does not read keys. Tab moves the focus; the arrow keys scroll the pane.
- **Other surfaces.** The trees use elements that the desktop app also has, and the tests draw them for each surface, but only the terminal was used with real sessions. The VS Code chat panel and `claude -p` run the mod and draw nothing. Outside the terminal, the hooks keep their own notice line for a message in the middle of a turn. The Claude mobile app has no text field, so the pane is read-only there.
- **Codex.** Codex has no mods. A Codex session shows the notices of the hooks, as before.

The mod has no status line. `peers statusline` is the status line segment; the README shows how to add it.

## Turn it off

Disable the mod on the Installed tab of `/plugin`, or remove it with `claude plugin uninstall peermesh@peermesh`. Sessions that start after that show the frames and the hook notices again. Nothing else changes, because the mod keeps no data of its own.

## Work on the mod

The mod is in [`mod/`](../mod). It reads the state of peermesh with one command, `peers --json watch`, which prints the sessions, the 100 newest messages with their states, and the id of the session that asks. Any other program that draws peermesh can use the same command.

```bash
claude plugin validate mod    # what Claude Code reads from the mod
claude plugin test mod        # the tests in mod/tests
npx -p typescript tsc -p mod  # type check, after Claude Code loaded the mod once with --plugin-dir
```

`mod/hooks/frame.ts` reads the text frame of a message back into its parts. `tests/test_mod.py` keeps `mod/tests/frames.ts` equal to what peermesh writes, so a change to the frame fails a Python test first. That test says how to write the file again.
