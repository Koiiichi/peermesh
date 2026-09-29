from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from peermesh import paths
from peermesh.envelope import new_message
from peermesh.registry import Peer, proc_start
from peermesh.transports.claude import CLAUDE_HOLD_NOTE, ClaudeSocketTransport
from peermesh.transports.codex import AppServerSteerer, CodexTransport

STUB = r"""#!/usr/bin/env python3
import json, os, sys
log = os.environ["STUB_LOG"]
args = sys.argv[1:]
if args[:1] == ["queue"]:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": args}) + "\n")
    sys.exit(int(os.environ.get("STUB_QUEUE_EXIT", "0")))
if args[:2] == ["app-server", "proxy"]:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg["method"]
        if method == "initialize":
            result = {}
        elif method == "thread/read":
            status = os.environ.get("STUB_TURN_STATUS", "inProgress")
            result = {"thread": {"turns": [{"id": "turn-9", "status": status}]}}
        elif method == "turn/steer":
            with open(log, "a") as fh:
                fh.write(json.dumps({"steer": msg["params"]}) + "\n")
            result = {"turnId": "turn-9"}
        else:
            print(json.dumps({"id": msg["id"], "error": {"message": "unknown"}}), flush=True)
            continue
        print(json.dumps({"method": "noise/notification", "params": {}}), flush=True)
        print(json.dumps({"id": msg["id"], "result": result}), flush=True)
"""


@pytest.fixture
def stub_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "codex"
    exe.write_text(STUB)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "stub.log"
    monkeypatch.setenv("PEERMESH_CODEX", str(exe))
    monkeypatch.setenv("STUB_LOG", str(log))
    return log


def _log(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _pair(make_peer: Callable[..., Peer], **target: object) -> tuple[Peer, Peer]:
    a = make_peer(id="claude:a", name="a")
    b = make_peer(id="codex:t-1", name="b", runtime="codex", endpoint="t-1", **target)
    return a, b


def test_claude_socket_frame() -> None:
    sock_dir = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(sock_dir, "s.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    received: list[bytes] = []

    def accept() -> None:
        conn, _ = server.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            received.append(buf)

    thread = threading.Thread(target=accept)
    thread.start()
    target = Peer(
        id="claude:t",
        name="t",
        runtime="claude",
        pid=os.getpid(),
        proc_start=proc_start(os.getpid()) or "",
        cwd="/",
        git_root=None,
        worktree=None,
        branch=None,
        repo_key=None,
        status="busy",
        last_seen=0.0,
        endpoint=path,
    )
    sender = Peer(**{**target.to_json(), "id": "codex:s", "name": "s", "runtime": "codex"})
    msg = new_message(sender, target, "need review", kind="request", urgency="now")
    outcome = ClaudeSocketTransport().deliver(target, "RENDERED", msg)
    thread.join(timeout=5)
    server.close()
    assert outcome.status == "delivered" and outcome.note == CLAUDE_HOLD_NOTE
    frame = json.loads(received[0])
    assert "token" not in json.dumps(frame)
    assert frame == {
        "type": "user",
        "message": {"role": "user", "content": "RENDERED"},
        "priority": "now",
        "msg_id": msg.id,
    }


def test_claude_socket_missing_is_offline(make_peer: Callable[..., Peer]) -> None:
    a = make_peer(id="codex:a", runtime="codex")
    b = make_peer(id="claude:b", endpoint="/tmp/peermesh-does-not-exist.sock")
    outcome = ClaudeSocketTransport().deliver(b, "x", new_message(a, b, "hi there"))
    assert outcome.status == "refused" and outcome.offline


def test_codex_queue_idle_is_delivered(make_peer: Callable[..., Peer], stub_codex: Path) -> None:
    a, b = _pair(make_peer, status="idle")
    outcome = CodexTransport().deliver(b, "RENDERED", new_message(a, b, "hello"))
    assert outcome.status == "delivered"
    assert _log(stub_codex) == [{"argv": ["queue", "--thread", "t-1", "--message", "RENDERED"]}]


def test_codex_queue_busy_is_queued(make_peer: Callable[..., Peer], stub_codex: Path) -> None:
    a, b = _pair(make_peer, status="busy")
    outcome = CodexTransport().deliver(b, "R", new_message(a, b, "hello"))
    assert outcome.status == "queued"


def test_codex_queue_failure_is_refused(
    make_peer: Callable[..., Peer], stub_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_QUEUE_EXIT", "3")
    a, b = _pair(make_peer, status="idle")
    assert CodexTransport().deliver(b, "R", new_message(a, b, "hello")).status == "refused"


def test_codex_missing_binary(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _pair(make_peer, status="idle")
    monkeypatch.setenv("PEERMESH_CODEX", "/nonexistent/codex")
    monkeypatch.setenv("PATH", "/nonexistent")
    outcome = CodexTransport().deliver(b, "R", new_message(a, b, "hello"))
    assert outcome.status == "refused" and "codex" in outcome.note


def _daemon_socket() -> None:
    sock = paths.codex_home() / "app-server-control" / "app-server-control.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()


def test_urgent_busy_steers_through_daemon(
    make_peer: Callable[..., Peer], stub_codex: Path
) -> None:
    _daemon_socket()
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop: schema changed", kind="request", urgency="now")
    outcome = CodexTransport().deliver(b, "RENDERED", msg)
    assert outcome.status == "delivered" and "steered" in outcome.note
    assert _log(stub_codex) == [
        {
            "steer": {
                "threadId": "t-1",
                "expectedTurnId": "turn-9",
                "input": [{"type": "text", "text": "RENDERED"}],
            }
        }
    ]


def test_urgent_without_active_turn_falls_back_to_queue(
    make_peer: Callable[..., Peer], stub_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon_socket()
    monkeypatch.setenv("STUB_TURN_STATUS", "completed")
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop", kind="request", urgency="now")
    outcome = CodexTransport().deliver(b, "R", msg)
    assert outcome.status == "queued"
    assert [e.get("argv", [None])[0] for e in _log(stub_codex)] == ["queue"]  # type: ignore[index]


def test_steer_without_daemon_returns_false(stub_codex: Path) -> None:
    assert AppServerSteerer(os.environ["PEERMESH_CODEX"]).steer("t-1", "x") is False
