"""Codex delivery: `codex queue` (thread/queue/add), and turn/steer through the daemon."""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from peermesh import __version__, paths
from peermesh.envelope import Message
from peermesh.registry import Peer
from peermesh.transports import Outcome


def codex_bin() -> str | None:
    override = os.environ.get("PEERMESH_CODEX")
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which("codex")


def _start_folder(preferred: str | None = None) -> str:
    """A folder that exists, for a codex command to start in: preferred, else the home folder.

    Codex reads its configuration for the folder that it starts in, and stops with "failed to
    resolve config cwd" when that folder does not exist. The folder of this process can be gone:
    a session keeps the folder that it started in after someone deletes it.
    """
    if preferred and os.path.isdir(preferred):
        return preferred
    return str(Path.home())


def daemon_socket() -> Path:
    return paths.codex_home() / "app-server-control" / "app-server-control.sock"


class RpcError(Exception):
    pass


class _Rpc:
    """Newline-delimited JSON-RPC over a child process (the app-server omits `jsonrpc`)."""

    def __init__(self, proc: subprocess.Popen[str], timeout: float) -> None:
        self._proc = proc
        self._timeout = timeout
        self._next = 0
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)

    def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        try:
            self._write({"id": rid, "method": method, "params": params})
        except OSError as exc:
            # The request did not leave this process, so the server did not receive it.
            raise RpcError(f"{method} was not sent: {exc}") from exc
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(method) from None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue
            if "error" in msg:
                raise RpcError(str(msg["error"]))
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


class _WsRpc:
    """JSON-RPC over a WebSocket on the daemon socket, one message in each text frame.

    Codex 0.162 serves the control socket this way. The client side of RFC 6455 that the
    daemon needs is small, so it is written here and peermesh takes no WebSocket dependency.
    """

    HANDSHAKE_S = 2.0

    def __init__(self, sock: socket.socket, timeout: float, buffered: bytes) -> None:
        self._sock = sock
        self._timeout = timeout
        self._buf = buffered
        self._next = 0

    @classmethod
    def connect(cls, path: Path, timeout: float) -> _WsRpc | None:
        """Open the socket and upgrade it. None means that it does not serve WebSocket."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(cls.HANDSHAKE_S)
        key = base64.b64encode(os.urandom(16)).decode()
        try:
            # The daemon socket is a symlink into a short directory. The resolved path stays
            # under the length limit of a Unix socket address when CODEX_HOME is deep.
            sock.connect(os.path.realpath(path))
            sock.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("closed during the handshake")
                data += chunk
        except OSError:
            sock.close()
            return None
        head, _, rest = data.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            sock.close()
            return None
        sock.settimeout(timeout)
        return cls(sock, timeout, rest)

    def close(self) -> None:
        self._sock.close()

    def _send(self, opcode: int, payload: bytes) -> None:
        size = len(payload)
        if size < 126:
            length = bytes([0x80 | size])
        elif size < 1 << 16:
            length = bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            length = bytes([0x80 | 127]) + struct.pack(">Q", size)
        # A client must mask every frame (RFC 6455, section 5.3).
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes([0x80 | opcode]) + length + mask + masked)

    def _arm(self, deadline: float) -> None:
        """Limit the next socket call to the time that remains before the deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self._sock.settimeout(remaining)

    def _read(self, size: int, deadline: float) -> bytes:
        while len(self._buf) < size:
            self._arm(deadline)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def _recv(self, deadline: float) -> Any:
        """The next complete message. Answers a ping, and joins the frames of one message."""
        parts = b""
        while True:
            # Frames that are in the buffer already need no read, and a pong is a write.
            self._arm(deadline)
            first, second = self._read(2, deadline)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2, deadline))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8, deadline))[0]
            payload = self._read(size, deadline)
            opcode = first & 0x0F
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode == 0x8:
                raise EOFError
            elif opcode in (0x0, 0x1, 0x2):
                parts += payload
                if first & 0x80:
                    return json.loads(parts)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send(0x1, json.dumps({"method": method, "params": params}).encode())

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        try:
            self._send(0x1, json.dumps({"id": rid, "method": method, "params": params}).encode())
        except OSError as exc:
            # sendall raises only when the kernel did not take every byte, so the server has
            # at most a part of one frame. It cannot read a part of a frame as a request.
            raise RpcError(f"{method} was not sent: {exc}") from exc
        # One deadline covers every frame, so pings and other messages cannot extend the wait.
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                msg = self._recv(deadline)
            except (OSError, EOFError, ValueError):
                # The request was sent. No answer does not show that the server dropped it.
                raise TimeoutError(method) from None
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue
            if "error" in msg:
                raise RpcError(str(msg["error"]))
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


class _Channel(Protocol):
    def notify(self, method: str, params: dict[str, Any]) -> None: ...

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


SteerResult = Literal["steered", "not_steered", "unknown"]


class Steerer(Protocol):
    def steer(self, thread_id: str, text: str) -> SteerResult: ...


class AppServerSteerer:
    """Inject text into the active turn of a daemon-hosted thread.

    "not_steered" means the text is not in the thread: use the queue. "unknown" means turn/steer
    was sent without an answer: the text can be in the turn, so it must not be queued too.
    """

    def __init__(self, exe: str, timeout: float = 10.0) -> None:
        self.exe = exe
        self.timeout = timeout

    def steer(self, thread_id: str, text: str) -> SteerResult:
        if not daemon_socket().exists():
            return "not_steered"
        ws = _WsRpc.connect(daemon_socket(), self.timeout)
        if ws is not None:
            try:
                return self._steer(ws, thread_id, text)
            finally:
                ws.close()
        # Codex 0.155 serves newline-delimited JSON, which `app-server proxy` relays.
        proc = subprocess.Popen(
            [self.exe, "app-server", "proxy"],
            cwd=_start_folder(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            return self._steer(_Rpc(proc, self.timeout), thread_id, text)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _steer(self, rpc: _Channel, thread_id: str, text: str) -> SteerResult:
        try:
            rpc.request("initialize", {"clientInfo": {"name": "peermesh", "version": __version__}})
            rpc.notify("initialized", {})
            thread = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            turns = thread.get("thread", {}).get("turns", [])
            active = [t for t in turns if isinstance(t, dict) and t.get("status") == "inProgress"]
            if not active:
                return "not_steered"
            turn_id = str(active[-1]["id"])
        except (RpcError, OSError, ValueError, KeyError, TimeoutError):
            return "not_steered"
        try:
            rpc.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": text}],
                },
            )
        except RpcError:
            return "not_steered"
        except (OSError, TimeoutError):
            return "unknown"
        return "steered"


class CodexTransport:
    def __init__(
        self,
        timeout: float = 30.0,
        steerer_factory: Callable[[str], Steerer] = AppServerSteerer,
    ) -> None:
        self.timeout = timeout
        self.steerer_factory = steerer_factory

    def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome:
        exe = codex_bin()
        if exe is None:
            return Outcome("refused", "The codex executable is not found. Install Codex CLI.")
        if msg.urgency == "now" and target.status == "busy":
            steered = self.steerer_factory(exe).steer(target.endpoint, rendered)
            if steered == "steered":
                return Outcome("delivered", "steered into the active turn")
            if steered == "unknown":
                return Outcome(
                    "refused",
                    "The steer request got no answer. The message can be in the active turn. "
                    "Do not resend.",
                )
        try:
            result = subprocess.run(
                [exe, "queue", "--thread", target.endpoint, "--message", rendered],
                # Codex reads project configuration for this folder; the target thread runs there.
                cwd=_start_folder(target.cwd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Outcome(
                "refused", "codex queue timed out. The message can be in the queue. Do not resend."
            )
        except OSError as exc:
            return Outcome("refused", f"codex queue did not start: {exc}.")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-300:]
            # Codex reports a closed or archived thread this way; the record is stale.
            gone = "no rollout found" in detail
            return Outcome("refused", f"codex queue failed: {detail}", offline=gone)
        if target.status == "idle":
            return Outcome("delivered", "queued; an idle thread starts a new turn")
        return Outcome("queued", "The peer reads the message at its next turn.")
