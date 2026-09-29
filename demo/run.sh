#!/usr/bin/env bash
# Starts a Codex session and a Claude Code session in two worktrees of a throwaway repo.
# Usage: demo/run.sh explicit|implicit [demo-dir]
set -euo pipefail
MODE=${1:?explicit or implicit}
DEMO=${2:-/tmp/peermesh-demo}
HERE=$(cd "$(dirname "$0")" && pwd)

rm -rf "$DEMO"
mkdir -p "$DEMO/repo"
cd "$DEMO/repo"
git init -q -b main
printf 'def total(prices: list[float]) -> float:\n    return sum(prices)\n' > cart.py
printf 'from cart import total\n\n\ndef checkout(prices: list[float]) -> str:\n    return f"Total: {total(prices):.2f}"\n' > checkout.py
git add cart.py checkout.py
git -c user.email=demo@example.invalid -c user.name=demo commit -qm "demo: initial"
git worktree add -q "$DEMO/wt-claude" -b claude-work
git worktree add -q "$DEMO/wt-codex" -b codex-work

# Each session must start clean: variables inherited from the launching agent session make the
# demo sessions look like its children (Claude then turns off transcript saving).
CLEAN='for v in $(env | grep -E "^(CLAUDE_CODE_|CODEX_|CLAUDECODE=|CLAUDE_PID=)" | cut -d= -f1); do unset "$v"; done'
cat > "$DEMO/start-codex.sh" <<EOS
$CLEAN
cd "$DEMO/wt-codex" && exec codex "\$(cat "$HERE/codex-prompt.txt")"
EOS
cat > "$DEMO/start-claude.sh" <<EOS
$CLEAN
cd "$DEMO/wt-claude" && exec claude "\$(cat "$HERE/claude-prompt-$MODE.txt")"
EOS

osascript -e "tell application \"Terminal\" to do script \"bash $DEMO/start-codex.sh\""
sleep 10
osascript -e "tell application \"Terminal\" to do script \"bash $DEMO/start-claude.sh\""
echo "Two sessions started. Watch both tabs. Ledger: peers log --limit 20"
