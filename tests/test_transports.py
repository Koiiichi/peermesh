from __future__ import annotations

import json
import os
import socket
import stat
import struct
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
            if os.environ.get("STUB_STEER_HANG"):
                continue
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


def test_steer_without_daemon_is_not_steered(stub_codex: Path) -> None:
    assert AppServerSteerer(os.environ["PEERMESH_CODEX"]).steer("t-1", "x") == "not_steered"


def test_unanswered_steer_is_not_queued_again(
    make_peer: Callable[..., Peer], stub_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon_socket()
    monkeypatch.setenv("STUB_STEER_HANG", "1")
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop", kind="request", urgency="now")
    transport = CodexTransport(steerer_factory=lambda exe: AppServerSteerer(exe, timeout=1.0))
    outcome = transport.deliver(b, "R", msg)
    assert outcome.status == "refused" and "Do not resend" in outcome.note
    assert [list(e) for e in _log(stub_codex)] == [["steer"]]


def test_steer_write_failure_is_not_steered() -> None:
    from peermesh.transports.codex import _Rpc

    class DeadProc:
        stdout: list[str] = []

        class stdin:  # noqa: N801 - mimics Popen.stdin
            @staticmethod
            def write(text: str) -> None:
                raise BrokenPipeError("proxy exited")

            @staticmethod
            def flush() -> None:
                pass

    steerer = AppServerSteerer("codex", timeout=1.0)
    rpc = _Rpc(DeadProc(), 1.0)  # type: ignore[arg-type]
    rpc.request = lambda method, params: (  # type: ignore[method-assign]
        {"thread": {"turns": [{"id": "t", "status": "inProgress"}]}}
        if method != "turn/steer"
        else _Rpc.request(rpc, method, params)
    )
    rpc.notify = lambda method, params: None  # type: ignore[method-assign]
    assert steerer._steer(rpc, "t-1", "x") == "not_steered"


def test_codex_queue_closed_thread_is_offline(
    make_peer: Callable[..., Peer],
    stub_codex: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail = tmp_path / "bin" / "codex-closed"
    fail.write_text(
        "#!/bin/sh\necho 'Error: thread/queue/add failed: no rollout found for thread id t-1' >&2\n"
        "exit 1\n"
    )
    fail.chmod(0o755)
    monkeypatch.setenv("PEERMESH_CODEX", str(fail))
    a, b = _pair(make_peer, status="idle")
    outcome = CodexTransport().deliver(b, "R", new_message(a, b, "hello"))
    assert outcome.status == "refused" and outcome.offline


class FakeDaemon:
    """A WebSocket app-server on a Unix socket, enough of RFC 6455 for one client."""

    def __init__(self, codex_home: Path, *, turn_status: str = "inProgress", hang: bool = False):
        self.steers: list[dict[str, object]] = []
        self.turn_status, self.hang = turn_status, hang
        # A Unix socket address has a short length limit, and pytest paths are long.
        self.dir = tempfile.mkdtemp(dir="/tmp", prefix="pm-ws-")
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(f"{self.dir}/d.sock")
        self.server.listen(1)
        control = codex_home / "app-server-control"
        control.mkdir(parents=True)
        (control / "app-server-control.sock").symlink_to(f"{self.dir}/d.sock")
        threading.Thread(target=self._serve, daemon=True).start()

    def _frame(self, opcode: int, payload: bytes, fin: bool = True) -> bytes:
        size = len(payload)
        length = bytes([size]) if size < 126 else bytes([126]) + struct.pack(">H", size)
        return bytes([(0x80 if fin else 0) | opcode]) + length + payload

    def _serve(self) -> None:
        conn, _ = self.server.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\r\n")
        buf = b""

        def read(size: int) -> bytes:
            nonlocal buf
            while len(buf) < size:
                chunk = conn.recv(4096)
                if not chunk:
                    raise EOFError
                buf += chunk
            out, buf = buf[:size], buf[size:]
            return out

        try:
            while True:
                first, second = read(2)
                assert second & 0x80, "a client frame must be masked"
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack(">H", read(2))[0]
                mask = read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read(size)))
                if first & 0x0F != 0x1:
                    continue
                msg = json.loads(payload)
                if "id" not in msg:
                    continue
                if msg["method"] == "thread/read":
                    result: dict[str, object] = {
                        "thread": {"turns": [{"id": "turn-9", "status": self.turn_status}]}
                    }
                elif msg["method"] == "turn/steer":
                    self.steers.append(msg["params"])
                    if self.hang:
                        continue
                    result = {"turnId": "turn-9"}
                else:
                    result = {}
                reply = json.dumps({"id": msg["id"], "result": result}).encode()
                conn.sendall(self._frame(0x9, b"ping"))
                conn.sendall(self._frame(0x1, b'{"method": "noise", "params": {}}'))
                # One message in two frames: a text frame and a continuation frame.
                conn.sendall(self._frame(0x1, reply[:5], fin=False) + self._frame(0x0, reply[5:]))
        except (EOFError, OSError):
            conn.close()


def test_urgent_busy_steers_through_a_websocket_daemon(
    make_peer: Callable[..., Peer], stub_codex: Path
) -> None:
    daemon = FakeDaemon(paths.codex_home())
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop: schema changed", kind="request", urgency="now")
    outcome = CodexTransport().deliver(b, "RENDERED " + "x" * 300, msg)
    assert outcome.status == "delivered" and "steered" in outcome.note
    assert daemon.steers == [
        {
            "threadId": "t-1",
            "expectedTurnId": "turn-9",
            "input": [{"type": "text", "text": "RENDERED " + "x" * 300}],
        }
    ]
    # The proxy of the stub was not used.
    assert _log(stub_codex) == []


def test_websocket_daemon_without_active_turn_falls_back_to_queue(
    make_peer: Callable[..., Peer], stub_codex: Path
) -> None:
    daemon = FakeDaemon(paths.codex_home(), turn_status="completed")
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop", kind="request", urgency="now")
    assert CodexTransport().deliver(b, "R", msg).status == "queued"
    assert daemon.steers == [] and [list(e) for e in _log(stub_codex)] == [["argv"]]


def test_unanswered_websocket_steer_is_not_queued_again(
    make_peer: Callable[..., Peer], stub_codex: Path
) -> None:
    daemon = FakeDaemon(paths.codex_home(), hang=True)
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop", kind="request", urgency="now")
    transport = CodexTransport(steerer_factory=lambda exe: AppServerSteerer(exe, timeout=1.0))
    outcome = transport.deliver(b, "R", msg)
    assert outcome.status == "refused" and "Do not resend" in outcome.note
    assert len(daemon.steers) == 1 and _log(stub_codex) == []


def test_daemon_that_does_not_upgrade_uses_the_proxy(
    make_peer: Callable[..., Peer], stub_codex: Path
) -> None:
    folder = tempfile.mkdtemp(dir="/tmp", prefix="pm-old-")
    server = socket.socket(socket.AF_UNIX)
    server.bind(f"{folder}/d.sock")
    server.listen(1)

    def refuse() -> None:
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()

    threading.Thread(target=refuse, daemon=True).start()
    control = paths.codex_home() / "app-server-control"
    control.mkdir(parents=True)
    (control / "app-server-control.sock").symlink_to(f"{folder}/d.sock")
    a, b = _pair(make_peer, status="busy")
    msg = new_message(a, b, "stop", kind="request", urgency="now")
    outcome = CodexTransport().deliver(b, "R", msg)
    assert outcome.status == "delivered" and [list(e) for e in _log(stub_codex)] == [["steer"]]
