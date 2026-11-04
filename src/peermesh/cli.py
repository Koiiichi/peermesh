"""`peers`: the command surface for Codex, for hooks, and for people."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import sys
import time
import traceback
from typing import Any

from peermesh import paths, text, views
from peermesh.envelope import short_id
from peermesh.errors import NoSessionError, PeerError
from peermesh.service import Mesh, SendResult

WRITE_DENIED = (errno.EROFS, errno.EACCES, errno.EPERM)


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


def _person_at_terminal() -> bool:
    """True when a person, not an agent, reads the output in a terminal.

    Codex can run commands in a pty, so a terminal alone does not prove a person reads it; an
    agent shell carries the session variables of its runtime.
    """
    agent = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    return sys.stdout.isatty() and not agent


def _styled() -> bool:
    """True when a person reads the output in a terminal that shows ANSI styles."""
    return (
        _person_at_terminal() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
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
    when = views.clock_time(entry)
    outcome = str(entry.get("outcome", ""))
    sender, target = (
        views.printable(str(entry.get("from_name"))),
        views.printable(str(entry.get("to_name"))),
    )
    names = f"{when}  {sender} → {target}  {entry.get('kind')}  "
    tail = f"  msg {short_id(str(entry.get('id', '')))}  "
    body = views.one_line(entry)
    width = shutil.get_terminal_size().columns
    room = width - len(names + outcome + tail)
    if room < 2:
        # The fixed fields fill the line. Cut the plain line, so it never wraps.
        return (names + outcome + tail).rstrip()[: width - 1] + "…"
    if len(body) > room:
        body = body[: room - 1] + "…"
    return names + _style(outcome, _OUTCOME_COLOR.get(outcome, "dim")) + tail + body


def _statusline() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        print(views.statusline(_mesh(), payload if isinstance(payload, dict) else {}))
    except Exception:  # a status line must never show a traceback in the session
        pass
    return 0


def _hook(args: argparse.Namespace) -> int:
    from peermesh import hooks
    from peermesh.registry import process_began

    began = process_began()
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
            plugin=args.plugin,
            emit=lambda out: print(json.dumps(out), flush=True),
            began=hooks.UNKNOWN_START if began is None else began,
        )
    except Exception:  # a hook must never break the host session
        try:
            paths.ensure_private_dir(paths.home())
            with (paths.home() / "hook-errors.log").open("a") as fh:
                fh.write(f"{time.ctime()} {args.runtime} {args.event}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


def _bridge(args: argparse.Namespace, mesh: Mesh) -> int:
    from peermesh import bridge

    if args.action == "on":
        print(bridge.turn_on(args.host, os.environ))
    elif args.action == "setup":
        options = {
            "target": args.target,
            "home": args.remote_home,
            "tmux": args.tmux_socket,
            "run_as": args.run_as,
            "peers": args.remote_peers,
            "profile": args.profile,
            "region": args.region,
            "plugin_dir": args.plugin_dir,
            "label": args.label,
        }
        print(bridge.set_up(args.host, os.environ, options))
    elif args.action == "off":
        print("\n".join(bridge.turn_off(mesh)))
    elif args.action == "run":
        bridge.run_link(mesh, bridge.check_label(args.host))
    elif args.action == "serve":
        print(bridge.serve_unix(mesh, args.peer_host), file=sys.stderr)
    elif args.action == "dial":
        bridge.dial(args.peer_host, args.tmux_socket, sys.stdin.buffer, sys.stdout.buffer)
    else:
        links = bridge.status(mesh)
        lines = [
            f"{link['host']:20} {'connected' if link['connected'] else 'offline':10} "
            f"{'running' if link['running'] else 'stopped':8} waiting {link['waiting']}"
            + (f"  {link['error']}" if link["error"] else "")
            for link in links
        ]
        _print(args, links, lines or ["No bridge is on."])
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="peers", description="Message other agent sessions.")
    p.add_argument("--json", action="store_true", help="print JSON")
    # The Codex rule matches `peers <subcommand>`, so a command that runs outside the sandbox
    # has --json after the subcommand. SUPPRESS keeps the value of the top-level flag.
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", parents=[output])
    ls.add_argument(
        "--scope",
        choices=["repo", "worktree", "all"],
        help="default: repo; outside an agent session, all",
    )
    snd = sub.add_parser("send", parents=[output])
    snd.add_argument("targets", nargs="+")
    snd.add_argument("--body", required=True, help="text, or - for stdin")
    snd.add_argument(
        "--kind", default="info", choices=["info", "request", "handoff", "review_request"]
    )
    snd.add_argument("--urgent", action="store_true")
    rep = sub.add_parser("reply", parents=[output])
    rep.add_argument("msg_id")
    rep.add_argument("--body", required=True, help="text, or - for stdin")
    rep.add_argument("--done", action="store_true", help="the requested work is complete")
    st = sub.add_parser("status", parents=[output])
    st.add_argument("target")
    sub.add_parser("whoami", parents=[output])
    tr = sub.add_parser("track", parents=[output])
    tr.add_argument("msg_id")
    lg = sub.add_parser("log", parents=[output])
    lg.add_argument("--limit", type=int, default=20)
    th = sub.add_parser("thread", parents=[output], help="show one conversation in order")
    th.add_argument("msg_id", help="the id of any message in the thread")
    wa = sub.add_parser("watch", parents=[output], help="live view of sessions and messages")
    wa.add_argument("--interval", type=float, default=1.0, help="seconds between updates")
    wa.add_argument("--once", action="store_true", help="print the view one time")
    sub.add_parser("statusline", help="status line segment for Claude Code")
    hk = sub.add_parser("hook")
    hk.add_argument(
        "event", choices=["session-start", "prompt", "post-tool", "stop", "session-end"]
    )
    hk.add_argument("--runtime", choices=["claude", "codex"], required=True)
    hk.add_argument("--inbox", action="store_true", help="the post-tool hook is installed")
    hk.add_argument("--plugin", action="store_true", help="the hook runs from the Codex plugin")
    br = sub.add_parser("bridge", help="link to peermesh on one other machine")
    bridge = br.add_subparsers(dest="action", required=True)
    br_set = bridge.add_parser("setup", help="store how to reach a host")
    br_set.add_argument("host", help="the label of the host in peer addresses")
    br_set.add_argument("--target", required=True, help="the instance id for Session Manager")
    br_set.add_argument("--remote-home", required=True, help="PEERMESH_HOME on the host")
    br_set.add_argument("--tmux-socket", required=True, help="the tmux server of its service")
    br_set.add_argument("--run-as", help="the user of the sessions on the host")
    br_set.add_argument("--remote-peers", help="the peers command on the host, default: peers")
    br_set.add_argument("--profile", help="the AWS profile")
    br_set.add_argument("--region", help="the AWS region")
    br_set.add_argument("--plugin-dir", help="the folder of session-manager-plugin")
    br_set.add_argument("--as", dest="label", help="the label of this machine on the host")
    br_on = bridge.add_parser("on", help="connect to a host")
    br_on.add_argument("host")
    bridge.add_parser("off", help="end each connection and forget its peers")
    bridge.add_parser("status", parents=[output])
    br_run = bridge.add_parser("run", help=argparse.SUPPRESS)
    br_run.add_argument("host")
    br_serve = bridge.add_parser("serve", help="take one connection on a private socket")
    br_serve.add_argument("--peer-host", required=True, help="the label of the connecting machine")
    br_dial = bridge.add_parser("dial", help="start serve through tmux and pass the bytes on")
    br_dial.add_argument("--peer-host", required=True, help="the label of the connecting machine")
    br_dial.add_argument("--tmux-socket", required=True)
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
    if args.cmd == "statusline":
        return _statusline()
    if args.cmd in ("doctor", "install", "uninstall"):
        from peermesh import install

        return install.run(args)
    mesh = _mesh()
    try:
        if args.cmd == "bridge":
            return _bridge(args, mesh)
        if args.cmd == "log":
            entries = mesh.ledger.latest()[-args.limit :]
            _print(args, entries, [_log_line(e) for e in entries])
            return 0
        if args.cmd == "watch" and args.json:
            try:
                me = mesh.whoami()
            except PeerError:
                me = None
            print(json.dumps(views.snapshot(mesh, me)))
            return 0
        if args.cmd == "watch":
            from rich.console import Console

            from peermesh import watch

            # An agent shell gets one plain frame. A person without colors still gets the
            # live view.
            console = Console(no_color=not _styled(), highlight=False)
            once = args.once or not _person_at_terminal()
            watch.watch(mesh, console, interval=args.interval, once=once)
            return 0
        if args.cmd == "thread":
            record = mesh.ledger.find(mesh.full_id(args.msg_id))
            if record is None:
                raise PeerError(f"No message with id {args.msg_id!r} is in the ledger.")
            records = mesh.ledger.thread(str(record.get("thread", record["id"])))
            width = shutil.get_terminal_size().columns
            _print(args, records, views.thread_lines(records, width, _style))
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
        if args.cmd == "list":
            try:
                me = mesh.whoami()
            except NoSessionError:
                if args.scope not in (None, "all"):
                    raise PeerError(
                        f"--scope {args.scope} needs an agent session. "
                        "Run it from the shell of an agent session, or use --scope all."
                    ) from None
                me = None
            except PeerError:
                # Every session is the answer to --scope all, whichever session this is.
                if args.scope != "all":
                    raise
                me = None
            peers = (
                mesh.list_peers(me, args.scope or "repo")
                if me
                else sorted(mesh.all_peers(), key=lambda p: p.name)
            )
            _print(
                args,
                [p.to_json() for p in peers],
                [
                    f"{p.name:28} {p.runtime:6} "
                    f"{_style(f'{p.status:6}', 'yellow' if p.status == 'busy' else 'dim')} "
                    f"{p.branch or '-':20} {p.worktree or p.cwd or f'on {p.remote_host}'}"
                    for p in peers
                ]
                or ["No other live peer in this scope." if me else "No live session."],
            )
            return 0
        me = mesh.whoami()
        if args.cmd == "whoami":
            _print(args, me.to_json(), [f"{me.name}  ({me.id})"])
            return 0
        if args.cmd == "send":
            urgency = "now" if args.urgent else "normal"
            return _results(args, mesh.send(me, args.targets, _body(args.body), args.kind, urgency))
        if args.cmd == "reply":
            return _results(args, [mesh.reply(me, args.msg_id, _body(args.body), done=args.done)])
    except PeerError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        # EPERM is the macOS sandbox's answer. Without a path, the error is not about the home.
        if exc.errno not in WRITE_DENIED or exc.filename is None:
            raise
        print(text.CANNOT_WRITE.format(path=exc.filename, reason=exc.strerror), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
