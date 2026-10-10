"""`peers`: the command surface for Codex, for hooks, and for people."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from typing import Any

from peermesh import paths
from peermesh.envelope import short_id
from peermesh.errors import PeerError
from peermesh.service import Mesh, SendResult


def _mesh() -> Mesh:
    return Mesh.default()


def _body(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _print(args: argparse.Namespace, data: Any, lines: list[str]) -> None:
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print("\n".join(lines))


_COLORS = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33"}
_OUTCOME_COLOR = {
    "delivered": "green",
    "queued": "green",
    "injected": "green",
    "pending": "yellow",
    "sending": "yellow",
    "refused": "red",
    "undelivered": "red",
}


def _styled() -> bool:
    """True when a person reads the output in a terminal that shows ANSI styles.

    Codex can run commands in a pty, so a terminal alone does not prove a person reads it; an
    agent shell carries the session variables of its runtime.
    """
    agent = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    return (
        sys.stdout.isatty()
        and not agent
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    )


def _style(text: str, color: str) -> str:
    return f"\033[{_COLORS[color]}m{text}\033[0m" if _styled() else text


def _results(args: argparse.Namespace, results: list[SendResult]) -> int:
    _print(
        args,
        [r.to_json() for r in results],
        [
            f"{_style(f'{r.status:9}', _OUTCOME_COLOR.get(r.status, 'dim'))} {r.to}  "
            f"msg {short_id(r.msg_id) if r.msg_id else '-'}  {r.note}".rstrip()
            for r in results
        ],
    )
    return 1 if any(r.status == "refused" for r in results) else 0


def _log_line(entry: dict[str, Any]) -> str:
    """One ledger entry on one terminal line: time, sender, target, kind, outcome, id, body."""
    try:
        when = datetime.fromisoformat(str(entry.get("ts"))).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        when = "--"
    outcome = str(entry.get("outcome", ""))
    names = f"{when}  {entry.get('from_name')} → {entry.get('to_name')}  {entry.get('kind')}  "
    tail = f"  msg {short_id(str(entry.get('id', '')))}  "
    body = " ".join(str(entry.get("body") or entry.get("note") or "").split())
    room = max(20, shutil.get_terminal_size().columns - len(names + outcome + tail))
    if len(body) > room:
        body = body[: room - 1] + "…"
    return names + _style(outcome, _OUTCOME_COLOR.get(outcome, "dim")) + tail + body


def _hook(args: argparse.Namespace) -> int:
    from peermesh import hooks

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        hooks.handle(
            _mesh(),
            args.event,
            args.runtime,
            payload,
            env=os.environ,
            inbox=args.inbox,
            emit=lambda out: print(json.dumps(out), flush=True),
        )
    except Exception:  # a hook must never break the host session
        try:
            paths.ensure_private_dir(paths.home())
            with (paths.home() / "hook-errors.log").open("a") as fh:
                fh.write(f"{time.ctime()} {args.runtime} {args.event}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="peers", description="Message other agent sessions.")
    p.add_argument("--json", action="store_true", help="print JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--scope", choices=["repo", "worktree", "all"], default="repo")
    snd = sub.add_parser("send")
    snd.add_argument("targets", nargs="+")
    snd.add_argument("--body", required=True, help="text, or - for stdin")
    snd.add_argument(
        "--kind", default="info", choices=["info", "request", "handoff", "review_request"]
    )
    snd.add_argument("--urgent", action="store_true")
    rep = sub.add_parser("reply")
    rep.add_argument("msg_id")
    rep.add_argument("--body", required=True, help="text, or - for stdin")
    rep.add_argument("--done", action="store_true", help="the requested work is complete")
    st = sub.add_parser("status")
    st.add_argument("target")
    sub.add_parser("whoami")
    tr = sub.add_parser("track")
    tr.add_argument("msg_id")
    lg = sub.add_parser("log")
    lg.add_argument("--limit", type=int, default=20)
    hk = sub.add_parser("hook")
    hk.add_argument(
        "event", choices=["session-start", "prompt", "post-tool", "stop", "session-end"]
    )
    hk.add_argument("--runtime", choices=["claude", "codex"], required=True)
    hk.add_argument("--inbox", action="store_true", help="the post-tool hook is installed")
    sub.add_parser("doctor")
    ins = sub.add_parser("install")
    ins.add_argument("--dry-run", action="store_true")
    ins.add_argument("--runtime", choices=["claude", "codex", "both"], default="both")
    un = sub.add_parser("uninstall")
    un.add_argument("--runtime", choices=["claude", "codex", "both"], default="both")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "hook":
        return _hook(args)
    if args.cmd in ("doctor", "install", "uninstall"):
        from peermesh import install

        return install.run(args)
    mesh = _mesh()
    try:
        if args.cmd == "log":
            entries = mesh.ledger.latest()[-args.limit :]
            _print(args, entries, [_log_line(e) for e in entries])
            return 0
        if args.cmd == "status":
            data = mesh.status(args.target)
            _print(args, data, [f"{k}: {v}" for k, v in data.items()])
            return 0 if data["alive"] else 1
        if args.cmd == "track":
            data = mesh.track(args.msg_id)
            lines = [
                f"msg {short_id(data['id'])} to {data['to']} [{data['kind']}]",
                f"state: {data['state']}",
            ]
            lines += [f"  {h['outcome']} {h.get('via') or ''}".rstrip() for h in data["history"]]
            lines += [
                f"  reply {short_id(r['id'])}{' done' if r['done'] else ''}"
                for r in data["replies"]
            ]
            _print(args, data, lines)
            return 0
        me = mesh.whoami()
        if args.cmd == "whoami":
            _print(args, me.to_json(), [f"{me.name}  ({me.id})"])
            return 0
        if args.cmd == "list":
            peers = mesh.list_peers(me, args.scope)
            _print(
                args,
                [p.to_json() for p in peers],
                [
                    f"{p.name:28} {p.runtime:6} "
                    f"{_style(f'{p.status:5}', 'yellow' if p.status == 'busy' else 'dim')} "
                    f"{p.branch or '-':20} {p.worktree or p.cwd}"
                    for p in peers
                ]
                or ["No other live peer in this scope."],
            )
            return 0
        if args.cmd == "send":
            urgency = "now" if args.urgent else "normal"
            return _results(args, mesh.send(me, args.targets, _body(args.body), args.kind, urgency))
        if args.cmd == "reply":
            return _results(args, [mesh.reply(me, args.msg_id, _body(args.body), done=args.done)])
    except PeerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
