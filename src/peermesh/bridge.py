"""A link between peermesh on this machine and peermesh on one other machine.

This machine starts an AWS Session Manager session, which lands outside the sandboxed service that
hosts the sessions over there. So `peers bridge dial` starts `peers bridge serve` through the tmux
server of the service, and passes the bytes on. The two sides exchange marked lines that hold peer
lists and messages. No frame can name a file, a command or a tool call.

The other machine is not trusted to follow the rules of this one. Each field of a frame is
checked here, each sender gets the label of the connection as its host, and `Mesh.ingest` applies
the limits again. A message from the other machine reads as peer text, like any other.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import tty
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO, Any, cast

import peermesh
from peermesh import paths
from peermesh.envelope import KINDS, URGENCIES
from peermesh.errors import PeerError
from peermesh.inbox import Waiting
from peermesh.registry import (
    LINK_DEAD_S,
    LINK_FILE,
    PEERS_FILE,
    link_state,
    link_up,
    proc_start,
    remote_peer,
    repo_tag,
)
from peermesh.service import OUTBOX, Mesh

PROTOCOL = 1
# A message body is at most 8 KiB, and JSON can escape each byte of it in six.
MAX_FRAME = 64 * 1024
# A frame is one line of this mark and base64. Session Manager writes its own lines into the same
# stream, and base64 holds no byte that a terminal acts on.
MARK = b"PM1 "
MAX_LINE = len(MARK) + (MAX_FRAME * 4) // 3 + 8
MAX_PEERS = 256
MAX_NOTE = 300
TICK_S = 0.25
PING_S = 15.0
PEERS_S = 5.0
# A delivery can take tens of seconds, so a shorter wait resends each slow message.
ACK_WAIT_S = 45.0
BACKOFF_MAX_S = 60.0
WORKERS = 4
# Frames that can wait for the writer. More means that the other machine stopped reading.
MAX_QUEUED = 1024
STOP_WAIT_S = 10.0
REMOTE_FILE = "remote.json"
SESSION_DOCUMENT = "AWS-StartInteractiveCommand"
REMOTE_PATH = "/usr/local/bin:/usr/bin:/bin"
SERVE_SOCKET = "serve.sock"
TMUX_SESSION = "peermesh-bridge"
# macOS allows 104 bytes for a Unix socket path, and Linux allows 108.
MAX_SOCKET_PATH = 103
# Long enough for a tmux server to start a pane on a loaded machine.
SERVE_START_S = 30.0

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_PEER_ID = re.compile(r"(claude|codex):[A-Za-z0-9_.-]{1,128}")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_MSG_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REPO = re.compile(r"[0-9a-f]{16}")
_STATUSES = ("busy", "idle", "paused")
_ANSWERS = ("pending", "queued", "delivered", "refused", "sending", "injected", "undelivered")
_OPEN = ("pending", "queued", "sending")
_AGENT_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")


class _Closed(Exception):
    """The connection cannot go on. The text says why."""


def check_label(host: str) -> str:
    """Return host when it can be a label: it becomes part of peer addresses and of a path."""
    if not _LABEL.fullmatch(host):
        raise PeerError(
            f"The host label {host!r} is not valid. Use letters, digits, '_', '.' and '-', "
            "and start with a letter or a digit."
        )
    return host


def _printable(text: object) -> str:
    return "".join(c if c.isprintable() else " " for c in str(text))[:MAX_NOTE]


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _outbound_peer(peer: Any) -> dict[str, Any]:
    """What the other machine learns about a session. A path, a pid or an endpoint never goes."""
    return {
        "id": peer.id,
        "name": peer.name,
        "runtime": peer.runtime,
        "status": peer.status,
        "repo": repo_tag(peer.repo_key) if peer.repo_key else None,
    }


def _inbound_peers(items: object) -> list[dict[str, Any]]:
    """The valid entries of a peer list. A bad entry costs only that entry."""
    if not isinstance(items, list):
        raise _Closed("a peer list is not a list")
    return [item for item in items[:MAX_PEERS] if _valid_peer(item)]


def _valid_peer(item: object) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and _PEER_ID.fullmatch(item["id"]) is not None
        and isinstance(item.get("name"), str)
        and _NAME.fullmatch(item["name"]) is not None
        and item.get("runtime") in ("claude", "codex")
        and item.get("status") in _STATUSES
        and (item.get("repo") is None or _matches(_REPO, item["repo"]))
    )


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_message(record: object) -> bool:
    """True when each field that peermesh renders or stores outside the quoted body has the
    shape it has here. A name with a line break could otherwise forge a line of the frame."""
    return (
        isinstance(record, dict)
        and _matches(_MSG_ID, record.get("id"))
        and _matches(_PEER_ID, record.get("from"))
        and _matches(_NAME, record.get("from_name"))
        and record.get("from_runtime") in ("claude", "codex")
        and _matches(_PEER_ID, record.get("to"))
        and _matches(_MSG_ID, record.get("thread"))
        and (record.get("in_reply_to") is None or _matches(_MSG_ID, record["in_reply_to"]))
        and record.get("kind") in KINDS
        and record.get("urgency") in URGENCIES
        and isinstance(record.get("body"), str)
        and type(record.get("hop")) is int
        and record["hop"] >= 0
        and type(record.get("broadcast")) is bool
        and type(record.get("done")) is bool
    )


class Session:
    """One connection. `run` returns when the connection ends, and sets `error` with the reason.

    `greets` is True for the side that speaks first. The side that starts the connection waits
    for that hello before it writes: until the far terminal is raw, it echoes what it gets.
    """

    def __init__(
        self,
        mesh: Mesh,
        host: str,
        reader: IO[bytes],
        writer: IO[bytes],
        *,
        greets: bool,
        stop: threading.Event | None = None,
        superseded: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.mesh = mesh
        self.host = host
        self.folder = paths.ensure_private_dir(paths.bridge_dir(host))
        self.greets = greets
        self.stop = stop or threading.Event()
        self.superseded = superseded
        self.clock = clock
        self.error = ""
        self.connected = False
        self._reader = reader
        self._writer = writer
        self._frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._outgoing: queue.Queue[bytes | None] = queue.Queue(MAX_QUEUED)
        self._sent: dict[str, float] = {}
        self._taking: set[str] = set()
        self._open: dict[str, str] = {}
        self._peers: list[dict[str, Any]] | None = None
        self._last_frame = clock()
        self._last_ping = 0.0
        self._last_peers = 0.0
        self._pool = ThreadPoolExecutor(max_workers=WORKERS)

    def run(self) -> None:
        threading.Thread(target=self._read, daemon=True).start()
        writer = threading.Thread(target=self._write, daemon=True)
        writer.start()
        try:
            if self.greets:
                self._hello()
            while not self.stop.is_set():
                if self.superseded():
                    raise _Closed("a newer connection took over")
                try:
                    frame = self._frames.get(timeout=TICK_S)
                except queue.Empty:
                    frame = {}
                if frame is None:
                    raise _Closed(self.error or "the connection closed")
                if frame:
                    self._last_frame = self.clock()
                    self._handle(frame)
                if self.connected:
                    self._tick()
                if self.clock() - self._last_frame >= LINK_DEAD_S:
                    raise _Closed(f"no frame for {LINK_DEAD_S:.0f} seconds")
            self._send({"type": "bye"})
        except (_Closed, OSError) as exc:
            self.error = str(exc)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
            with contextlib.suppress(queue.Full):
                self._outgoing.put_nowait(None)
            writer.join(1.0)  # a last frame gets a moment; a stuck peer does not hold this up
            if not self.superseded():
                self._write_link(connected=False)

    def _read(self) -> None:
        """Queue each frame, and skip each line without the mark. A marked line that is not a
        frame ends the connection: the two sides no longer agree where a frame starts."""
        while True:
            line = self._reader.readline(MAX_LINE + 1)
            if not line:
                break
            if not line.startswith(MARK):
                continue
            try:
                if len(line) > MAX_LINE:
                    raise ValueError("a frame is too large")
                frame = json.loads(base64.b64decode(line[len(MARK) :].strip(), validate=True))
                if not isinstance(frame, dict) or not frame:
                    raise ValueError("a frame is not a JSON object")
            except ValueError as exc:
                self.error = f"bad frame: {exc}"
                break
            self._frames.put(frame)
        self._frames.put(None)

    def _send(self, frame: dict[str, Any]) -> None:
        """Queue a frame. The writer thread does the write, so a far side that stops reading
        cannot hold up the stop event or the silence limit."""
        data = MARK + base64.b64encode(json.dumps(frame).encode()) + b"\n"
        try:
            self._outgoing.put_nowait(data)
        except queue.Full:
            raise _Closed(f"{self.host} stopped reading") from None

    def _write(self) -> None:
        while (data := self._outgoing.get()) is not None:
            try:
                self._writer.write(data)
                self._writer.flush()
            except OSError:
                self._frames.put(None)
                return

    def _hello(self) -> None:
        self._send({"type": "hello", "protocol": PROTOCOL, "version": peermesh.__version__})

    def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "hello":
            if frame.get("protocol") != PROTOCOL:
                raise _Closed(
                    f"protocol {_printable(frame.get('protocol'))} on {self.host}, "
                    f"{PROTOCOL} here; install the same peermesh version on both machines"
                )
            if not self.greets:
                self._hello()
            self.connected = True
        elif not self.connected:
            raise _Closed(f"a {_printable(kind)} frame came before hello")
        elif kind == "peers":
            _write_json(self.folder / PEERS_FILE, _inbound_peers(frame.get("peers")))
        elif kind == "msg":
            self._take(frame.get("message"))
        elif kind == "ack":
            self._on_ack(frame)
        elif kind == "outcome":
            self._on_outcome(frame)
        elif kind == "bye":
            (self.folder / PEERS_FILE).unlink(missing_ok=True)
            raise _Closed(f"{self.host} turned the bridge off")
        self._write_link(connected=True)

    def _take(self, record: Any) -> None:
        if not _valid_message(record):
            msg_id = record.get("id") if isinstance(record, dict) else None
            if _matches(_MSG_ID, msg_id):
                self._answer(str(msg_id), "refused", "The message has a field that is not valid.")
            return
        msg_id = record["id"]
        # A resend of a message that is still in delivery gets its answer from that delivery.
        if msg_id not in self._taking:
            self._taking.add(msg_id)
            self._pool.submit(self._deliver, record)

    def _deliver(self, record: dict[str, Any]) -> None:
        msg_id = record["id"]
        sender = remote_peer(
            self.host,
            {"id": record["from"], "name": record["from_name"], "runtime": record["from_runtime"]},
        )
        try:
            result = self.mesh.ingest(sender, record)
            status, note = result.status, result.note
        except (PeerError, OSError) as exc:
            status, note = "refused", _printable(exc)
        if status in _OPEN:
            self._open[msg_id] = status
        try:
            self._answer(msg_id, status, note)
        except _Closed:
            pass  # the sender resends on the next connection, and the ledger answers
        self._taking.discard(msg_id)

    def _answer(self, msg_id: str, outcome: str, note: str) -> None:
        self._send({"type": "ack", "id": msg_id, "outcome": outcome, "note": note})

    def _on_ack(self, frame: dict[str, Any]) -> None:
        """Record an answer. Only a message in this outbox can get one, so the other machine
        cannot change any other record of the ledger."""
        outcome = frame.get("outcome")
        if outcome not in _ANSWERS:
            return
        item = self._waiting(frame.get("id"))
        if item is not None:
            self.mesh.settle(self.host, item, str(outcome), _printable(frame.get("note", "")))
            self._sent.pop(item.id, None)

    def _waiting(self, msg_id: object) -> Waiting | None:
        waiting = self.mesh.outbox(self.host).waiting(OUTBOX)
        return next((item for item in waiting if item.id == msg_id), None)

    def _on_outcome(self, frame: dict[str, Any]) -> None:
        """Record what became of a message after its answer. Only a message that this machine
        sent to that host can change."""
        msg_id, outcome = frame.get("id"), frame.get("outcome")
        if not _matches(_MSG_ID, msg_id) or outcome not in _ANSWERS:
            return
        record = self.mesh.ledger.find(str(msg_id))
        if record is not None and str(record.get("to", "")).startswith(f"{self.host}/"):
            with self.mesh.ledger.locked():
                self.mesh.ledger.append(
                    {
                        "id": msg_id,
                        "outcome": outcome,
                        "note": _printable(frame.get("note", "")),
                        "at": self.mesh.clock(),
                    }
                )

    def _tick(self) -> None:
        now = self.clock()
        self.mesh.expire_outbox(self.host)
        for item in self.mesh.outbox(self.host).waiting(OUTBOX):
            if now - self._sent.get(item.id, 0.0) >= ACK_WAIT_S:
                self._sent[item.id] = now
                self._send({"type": "msg", "message": self._outbound(item)})
        if now - self._last_ping >= PING_S:
            self._last_ping = now
            self._send({"type": "ping"})
        if now - self._last_peers >= PEERS_S:
            self._last_peers = now
            self._share_peers()
            self._report_outcomes()

    def _outbound(self, item: Waiting) -> dict[str, Any]:
        """The record as the other machine knows the target, which has no host prefix there."""
        record = {k: v for k, v in item.record.items() if k != "queued_at"}
        return record | {"to": str(record["to"]).removeprefix(f"{self.host}/")}

    def _share_peers(self) -> None:
        try:
            peers = [_outbound_peer(p) for p in self.mesh.local_peers()]
        except PeerError:
            return  # ps gave no answer, so the last list stays
        if peers != self._peers:
            self._peers = peers
            self._send({"type": "peers", "peers": peers})

    def _report_outcomes(self) -> None:
        for msg_id, reported in list(self._open.items()):
            record = self.mesh.ledger.find(msg_id)
            outcome = str((record or {}).get("outcome"))
            if outcome != reported:
                self._send(
                    {
                        "type": "outcome",
                        "id": msg_id,
                        "outcome": outcome,
                        "note": (record or {}).get("note", ""),
                    }
                )
                self._open[msg_id] = outcome
            if outcome not in _OPEN:
                del self._open[msg_id]

    def _write_link(self, *, connected: bool) -> None:
        state = link_state(self.host) | {
            "connected": connected,
            "seen": self.clock(),
            "error": self.error,
        }
        _write_json(self.folder / LINK_FILE, state)


def serve(mesh: Mesh, host: str, reader: IO[bytes], writer: IO[bytes]) -> str:
    """Answer one connection from `host` until it ends. A newer connection takes over from this
    one, because a lost connection can leave this process running until its next read fails."""
    folder = paths.ensure_private_dir(paths.bridge_dir(check_label(host)))
    token = uuid.uuid4().hex
    claim = folder / "serve.token"
    claim.write_text(token)
    session = Session(
        mesh,
        host,
        reader,
        writer,
        greets=True,
        superseded=lambda: claim.read_text() != token,
    )
    session.run()
    return session.error


def _socket_path(host: str) -> Path:
    path = paths.ensure_private_dir(paths.bridge_dir(check_label(host))) / SERVE_SOCKET
    if len(str(path)) > MAX_SOCKET_PATH:
        raise PeerError(f"The path {path} is too long for a socket. Use a shorter PEERMESH_HOME.")
    return path


def serve_unix(mesh: Mesh, host: str) -> str:
    """Take one connection on a socket in the private folder of `host`, and answer it. Nothing
    listens after that connection arrives, or after `SERVE_START_S` without one."""
    path = _socket_path(host)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(SERVE_START_S)
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            return "no connection arrived"
        finally:
            path.unlink(missing_ok=True)
    with connection:
        reader = connection.makefile("rb")
        writer = cast(IO[bytes], connection.makefile("wb", buffering=0))
        return serve(mesh, host, reader, writer)


def _start_serve(host: str, tmux_socket: str) -> None:
    """Start `serve` as a pane of the tmux server. The server forks each pane, so the pane is
    inside its service with the environment of the service, wherever this client runs."""
    tmux = [os.environ.get("PEERMESH_TMUX") or "tmux", "-S", tmux_socket]
    serve_command = shlex.join(
        [sys.executable, "-m", "peermesh.cli", "bridge", "serve", "--peer-host", host]
    )
    # An earlier connection can leave its session behind, and the name is this bridge's own.
    target = f"={TMUX_SESSION}"
    try:
        subprocess.run([*tmux, "kill-session", "-t", target], capture_output=True, check=False)
        result = subprocess.run(
            [*tmux, "new-session", "-d", "-s", TMUX_SESSION, serve_command],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PeerError(f"tmux cannot start the bridge: {exc.strerror}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-200:]
        raise PeerError(f"tmux cannot start the bridge: {detail}")


def _connect_serve(path: Path) -> socket.socket:
    deadline = time.monotonic() + SERVE_START_S
    while True:
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.connect(str(path))
            return connection
        except OSError as exc:
            connection.close()
            if time.monotonic() >= deadline:
                raise PeerError(f"The bridge did not start: {exc.strerror}.") from exc
            time.sleep(0.1)


def _pump(read: Callable[[], bytes], write: Callable[[bytes], Any]) -> None:
    try:
        while data := read():
            write(data)
    except OSError:
        pass  # the other direction ends the connection


def dial(host: str, tmux_socket: str, reader: IO[bytes], writer: IO[bytes]) -> None:
    """Start `serve` inside the service and pass bytes between it and this connection, until
    either end closes. This process reads no frame: `serve` does all the checks."""
    path = _socket_path(host)
    path.unlink(missing_ok=True)
    _start_serve(host, tmux_socket)
    with _connect_serve(path) as connection:
        if os.isatty(reader.fileno()):
            # Session Manager gives a terminal, which echoes input and limits a line to 4 KiB.
            tty.setraw(reader.fileno())
        finished = threading.Event()

        def pump(read: Callable[[], bytes], write: Callable[[bytes], Any]) -> None:
            _pump(read, write)
            finished.set()

        for read, write in (
            (lambda: connection.recv(MAX_FRAME), lambda data: _write_all(writer, data)),
            (lambda: os.read(reader.fileno(), MAX_FRAME), connection.sendall),
        ):
            threading.Thread(target=pump, args=(read, write), daemon=True).start()
        finished.wait()


def _write_all(writer: IO[bytes], data: bytes) -> None:
    writer.write(data)
    writer.flush()


def _remote(host: str) -> dict[str, Any]:
    try:
        remote = json.loads((paths.bridge_dir(host) / REMOTE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return remote if isinstance(remote, dict) else {}


def _remote_command(host: str) -> str:
    """The command that answers on the other machine. A Session Manager login there has neither
    the user nor the paths of the service, so the command names them."""
    remote = _remote(host)
    words = ["env", f"PATH={REMOTE_PATH}", f"PEERMESH_HOME={remote['home']}"]
    words += ["peers", "bridge", "dial", "--peer-host", remote["label"]]
    words += ["--tmux-socket", remote["tmux"]]
    if remote.get("run_as"):
        words = ["sudo", "-n", "runuser", "-u", remote["run_as"], "--", *words]
    return shlex.join(words)


def _connect_command(host: str) -> list[str]:
    remote = _remote(host)
    command = [os.environ.get("PEERMESH_BRIDGE_CONNECT") or "aws"]
    for option in ("profile", "region"):
        if remote.get(option):
            command += [f"--{option}", remote[option]]
    return [
        *command,
        "ssm",
        "start-session",
        "--target",
        remote["target"],
        "--document-name",
        SESSION_DOCUMENT,
        "--parameters",
        json.dumps({"command": [_remote_command(host)]}),
    ]


def _last_line(path: Path) -> str:
    try:
        lines = path.read_text(errors="replace").strip().splitlines()
    except OSError:
        return ""
    return _printable(lines[-1]) if lines else ""


def _connect_once(mesh: Mesh, host: str, stop: threading.Event) -> tuple[bool, str]:
    """Run one connection. Returns whether the two sides spoke, and the reason it ended."""
    log = paths.bridge_dir(host) / "connect.log"
    env = dict(os.environ)
    plugin_dir = _remote(host).get("plugin_dir")
    if plugin_dir:
        env["PATH"] = f"{plugin_dir}{os.pathsep}{env.get('PATH', '')}"
    with log.open("w") as errors:
        # Its own session gives it no terminal, so the plugin cannot change the user's terminal,
        # and a signal to its process group also reaches the plugin that `aws` starts.
        link = subprocess.Popen(
            _connect_command(host),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=env,
            start_new_session=True,
        )
    assert link.stdin is not None and link.stdout is not None
    session = Session(mesh, host, link.stdout, link.stdin, greets=False, stop=stop)
    try:
        session.run()
    finally:
        _end_group(link)
    return session.connected, session.error if session.connected else _last_line(log)


def _end_group(link: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(link.pid, signal.SIGTERM)
        link.wait(timeout=5)
    except (ProcessLookupError, PermissionError):
        # The group is gone. macOS gives EPERM for a group that holds only a dead process.
        link.wait()
    except subprocess.TimeoutExpired:
        os.killpg(link.pid, signal.SIGKILL)
        link.wait()


def run_link(mesh: Mesh, host: str) -> None:
    """Keep the connection to `host` until SIGTERM, with a growing wait after each failure.

    One process runs for each host: `peers bridge on` starts it, and `peers bridge off` ends it.
    """
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    pid = os.getpid()
    folder = paths.ensure_private_dir(paths.bridge_dir(host))
    state = {"pid": pid, "started": proc_start(pid), "connected": False, "seen": 0.0}
    _write_json(folder / LINK_FILE, state)
    delay = 1.0
    while not stop.is_set():
        spoke, reason = _connect_once(mesh, host, stop)
        _write_json(folder / LINK_FILE, link_state(host) | {"connected": False, "error": reason})
        mesh.expire_outbox(host)
        delay = 1.0 if spoke else min(delay * 2, BACKOFF_MAX_S)
        stop.wait(delay)


def _running(host: str) -> int | None:
    """The pid of the link process of `host`, when it runs."""
    state = link_state(host)
    pid = state.get("pid")
    if isinstance(pid, int) and proc_start(pid) == state.get("started"):
        return pid
    return None


def _refuse_agent(env: Mapping[str, str]) -> None:
    """Refuse an agent session. It is a guard against a mistake only: a process of this user can
    also start the link another way."""
    if any(env.get(name) for name in _AGENT_ENV):
        raise PeerError("Only the owner changes the bridge, from a terminal. Ask the user.")


def set_up(host: str, env: Mapping[str, str], options: Mapping[str, str | None]) -> str:
    """Store how to reach `host`, outside the repository of any project. `label` is the name of
    this machine over there."""
    check_label(host)
    _refuse_agent(env)
    remote = {key: value for key, value in options.items() if value}
    remote["label"] = check_label(remote.get("label") or socket.gethostname().split(".")[0])
    folder = paths.ensure_private_dir(paths.bridge_dir(host))
    _write_json(folder / REMOTE_FILE, remote)
    return f"The bridge to {host} is set up. Run `peers bridge on {host}` to connect."


def turn_on(host: str, env: Mapping[str, str]) -> str:
    """Start the link process of `host`."""
    check_label(host)
    _refuse_agent(env)
    if not _remote(host):
        raise PeerError(f"The bridge to {host} is not set up. Run `peers bridge setup {host}`.")
    if _running(host) is not None:
        return f"The bridge to {host} is already on."
    with (paths.bridge_dir(host) / "link.log").open("w") as errors:
        subprocess.Popen(
            [sys.executable, "-m", "peermesh.cli", "bridge", "run", host],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )
    return f"The bridge to {host} is on. Run `peers bridge status` to see the connection."


def _hosts() -> list[str]:
    root = paths.bridge_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def turn_off(mesh: Mesh) -> list[str]:
    """End each link, forget its peers, and end the messages that wait for it."""
    lines: list[str] = []
    for host in _hosts():
        pid = _running(host)
        if pid is not None:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + STOP_WAIT_S
            while _running(host) is not None and time.monotonic() < deadline:
                time.sleep(0.1)
            if _running(host) is not None:
                # The link file holds the only record of this process, so it stays.
                lines.append(f"The bridge to {host} did not stop: process {pid} runs.")
                continue
        mesh.expire_outbox(host, every=True)
        folder = paths.bridge_dir(host)
        (folder / PEERS_FILE).unlink(missing_ok=True)
        (folder / LINK_FILE).unlink(missing_ok=True)
        lines.append(f"The bridge to {host} is off.")
    return lines or ["No bridge is on."]


def status(mesh: Mesh) -> list[dict[str, Any]]:
    """One entry for each host, with the reason of the last failure."""
    now = time.time()
    found: list[dict[str, Any]] = []
    for host in _hosts():
        state = link_state(host)
        if state:
            found.append(
                {
                    "host": host,
                    "connected": link_up(state, now),
                    "running": _running(host) is not None,
                    "waiting": len(mesh.outbox(host).waiting(OUTBOX)),
                    "error": state.get("error", ""),
                }
            )
    return found
