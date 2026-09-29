"""Codex delivery: `codex queue` (thread/queue/add), and turn/steer through the daemon."""

from __future__ import annotations

import json
import os
import queue
import shutil
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
        self._write({"id": rid, "method": method, "params": params})
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
        proc = subprocess.Popen(
            [self.exe, "app-server", "proxy"],
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

    def _steer(self, rpc: _Rpc, thread_id: str, text: str) -> SteerResult:
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
            return Outcome("refused", f"codex queue failed: {detail}")
        if target.status == "idle":
            return Outcome("delivered", "queued; an idle thread starts a new turn")
        return Outcome("queued", "The peer reads the message at its next turn.")
