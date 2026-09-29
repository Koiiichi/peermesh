# Demo and live-test results

Date: 2026-09-29. Claude Code 2.1.285, codex-cli 0.155.1, macOS. Installed with `peers install`; `peers doctor` passed all seven checks.

## Live test

`PEERMESH_LIVE=1 uv run pytest -m live`: 1 passed. A `codex exec` session ran `peers send claude-live-test --body '…'` through its shell. The command ran outside the sandbox under the installed rule, identified the session from `CODEX_THREAD_ID`, and delivered a frame reading `(codex, id codex:…)` to a test-owned Claude inbox socket.

## Explicit demo (`demo/run.sh explicit`)

Claude was told to find the Codex peer, rename `total` to `sum_prices`, and ask Codex to update `checkout.py`.

```text
22:24:13 claude-repo-d3 -> codex-repo-51 [request/delivered] hop=0
   Interface change: in cart.py, the function "total" now has the name "sum_prices". …
   Commit: 264d362… on branch claude-work.
   Request: On branch codex-work, update checkout.py to use the new name. …
   When you complete the change, reply with the commit hash.
22:25:13 codex-repo-51 -> claude-repo-d3 [reply/delivered] hop=1
   Commit aa58d2d… on codex-work updates the import and call in checkout.py to sum_prices. …
   Checks for a populated cart and an empty cart passed with cart.py from commit 264d362….
```

Codex was idle; `codex queue` started its turn. It committed `aa58d2d` on `codex-work` and replied with `peers reply`. The reply reached the interactive Claude session through its inbox socket without a token, as a peer message: Claude Code showed the peermesh frame followed by its own "not typed by your user" notice, reported the reply to the user, and did not send an acknowledgement back. Both observations were checked visually in the two Terminal tabs.

This confirms that a socket write without a token reaches a prompting Claude Code session as a peer message.

## Implicit demo (`demo/run.sh implicit`)

Claude was told only: "In cart.py, rename the function total to sum_prices. Commit the change." Nothing mentioned Codex or peermesh.

```text
22:29:56 claude-repo-76 -> codex-repo-db [info/delivered] hop=0
   Interface change on branch claude-work, commit 9fb47ad: the function cart.total is now
   cart.sum_prices. … If your code on codex-work imports or calls cart.total, change it to
   cart.sum_prices before you merge with claude-work.
22:30:47 codex-repo-db -> claude-repo-76 [reply/delivered] hop=1
   Commit ad06684… on codex-work applies the changes from 9fb47ad. … Checks passed for empty
   prices, positive prices, and mixed positive and negative prices. The worktree is clean.
```

Claude sent the notice on its own, right after its commit. The session-start context and the instruction block were the only prompts to do so. One run is one observation, not a rate.

## Observations

- Claude Code appends its native peer notice to every inbound peer message and calls the sender "another Claude session" even when the peermesh frame says `codex`. The peermesh frame carries the correct runtime.
- The first explicit run inherited `CLAUDE_CODE_CHILD_SESSION` from the launching session, so Claude turned off transcript saving. `demo/run.sh` now clears inherited agent variables before it starts each session.
- No hook errors were logged during any run.
