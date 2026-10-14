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

A message that wakes the session, and a message that the agent sends, each take two or three lines:

```text
→ shop-web  request  answered  84b01ebb
  Which port does the web dev server use? The API must allow it in CORS.

← shop-web  reply  5b457c23
  ▎ you  Which port does the web dev server use? The API must allow it in CORS.
  The web dev server uses port 5173. Allow http://localhost:5173 as an origin in the API's CORS config.
```

`←` is a message that the agent of this session received. The row shows the sender, the kind and the id. The line with the bar `▎` is a quote: the start of the message that a reply answers, with `you` or the name of the session that wrote it. A row shows the first eight lines of a received message and the first three of a sent one, without their empty lines. A row with more text ends with the number of lines that it does not show, such as `… 8 more lines`. In fullscreen mode, such a row has the option `▸ expand` on its first line, in a color of its own. Click that line to open the row; the option is then `▾ collapse`, and a click folds the row.

`→` is a message that the agent sent. Its state is the state in the ledger now, so the row changes after it is drawn:

| Word | What happened |
| --- | --- |
| `pending` | The message waits for the next tool call or prompt of the peer. |
| `delivered` | The runtime of the peer has the message. No runtime reports that an agent read it. |
| `answered` | The peer sent a reply. |
| `done` | The peer sent a reply and reported that the work is complete. |
| `failed` | The message was refused, or the peer closed before it got the message. A refused message shows the reason. |

Press ctrl+o to see a message as the agent read it, with the lines that peermesh and Claude Code add for the agent.

A message that a hook gives the agent in the middle of a turn is one line, because Claude Code lets a mod add only a line there:

```text
⏺ peermesh: ← peermesh-arch · info · cddf33c9 · The schema file moved to docs/schema.md. The old path src/schema.md is gone as of commit 4be1f0a.
```

Without the mod, the same delivery shows as `PostToolUse:Bash says: peermesh: gave the agent info from peermesh-arch`, without the text.

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
