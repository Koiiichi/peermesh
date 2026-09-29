"""`peers`: the command surface for Codex, for hooks, and for people."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from typing import Any

from peermesh import paths
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


def _results(args: argparse.Namespace, results: list[SendResult]) -> int:
    _print(
        args,
        [r.to_json() for r in results],
        [f"{r.status:9} {r.to}  msg={r.msg_id or '-'}  {r.note}".rstrip() for r in results],
    )
    return 1 if any(r.status == "refused" for r in results) else 0


def _hook(args: argparse.Namespace) -> int:
    from peermesh import hooks

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out = hooks.handle(_mesh(), args.event, args.runtime, payload, env=os.environ)
        if out is not None:
            print(json.dumps(out))
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
    st = sub.add_parser("status")
    st.add_argument("target")
    sub.add_parser("whoami")
    lg = sub.add_parser("log")
    lg.add_argument("--limit", type=int, default=20)
    hk = sub.add_parser("hook")
    hk.add_argument("event", choices=["session-start", "prompt", "stop", "session-end"])
    hk.add_argument("--runtime", choices=["claude", "codex"], required=True)
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
            entries = mesh.ledger.entries()[-args.limit :]
            _print(
                args,
                entries,
                [
                    f"{e.get('ts', '')} {e.get('from_name')} -> {e.get('to_name')} "
                    f"[{e.get('kind')}/{e.get('outcome')}] {str(e.get('body', ''))[:80]!r}"
                    for e in entries
                ],
            )
            return 0
        if args.cmd == "status":
            data = mesh.status(args.target)
            _print(args, data, [f"{k}: {v}" for k, v in data.items()])
            return 0 if data["alive"] else 1
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
                    f"{p.name:28} {p.runtime:6} {p.status:5} {p.branch or '-':20} "
                    f"{p.worktree or p.cwd}"
                    for p in peers
                ]
                or ["No other live peer in this scope."],
            )
            return 0
        if args.cmd == "send":
            urgency = "now" if args.urgent else "normal"
            return _results(args, mesh.send(me, args.targets, _body(args.body), args.kind, urgency))
        if args.cmd == "reply":
            return _results(args, [mesh.reply(me, args.msg_id, _body(args.body))])
    except PeerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
