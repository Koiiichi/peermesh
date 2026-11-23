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
import fcntl
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import tty
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO, Any, cast

import peermesh
from peermesh import paths
from peermesh.envelope import KINDS, URGENCIES
from peermesh.errors import PeerError, ProcessCheckError
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
MAX_CAPABILITIES = 8
# Later than any clock this code runs under. A bound on an int needs no conversion to float, and
# a large enough int cannot convert.
MAX_SEEN = 10**11
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
ERROR_LOG = "errors.log"
LINK_LOG = "link.log"
LINK_LOCK = "link.lock"
LINK_LIFE = "link.life"
LOG_LOCK = "link.log.lock"
AUTOSTART_FILE = "autostart.json"
HOLD_FILE = "hold"
MAX_LINK_LOG = 256 * 1024
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
_CAPABILITY = re.compile(r"[a-z_]{1,32}")
# The fields of a peer list entry that every version sends.
_REQUIRED = ("id", "name", "runtime", "status", "repo")
_STATUSES = ("busy", "idle", "paused")
_ANSWERS = ("pending", "queued", "delivered", "refused", "sending", "injected", "undelivered")
_OPEN = ("pending", "queued", "sending")
# What can follow an answer. A refusal cannot: it would drop the message from the history that
# the loop limit reads.
_LATE = ("delivered", "injected", "undelivered")
_AGENT_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")
# All that a link started by a session start takes from that session. The link outlives the
# session, so it must not keep its identity or its AWS credentials.
_SESSION_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")
# Where the owner's shell finds the runtimes. The link delivers arriving messages to them.
_OWNER_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PEERMESH_CLAUDE_APP", "PEERMESH_CODEX")
_SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


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
        "auto_name": peer.auto_name,
        "runtime": peer.runtime,
        "status": peer.status,
        "repo": repo_tag(peer.repo_key) if peer.repo_key else None,
        "capabilities": peer.capabilities,
        "last_seen": peer.last_seen,
    }


def _fit(peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The peer list in a size that one frame holds. A full list of long entries passes the frame
    limit with the optional fields, and usually fits without them. Entries of the longest shape
    can pass it even then, so the last entries stay out: a frame past the limit closes the link,
    and the next connection sends the same list."""
    if _frame_size(peers) <= MAX_FRAME:
        return peers
    basic = [{key: p[key] for key in _REQUIRED} for p in peers]
    while basic and _frame_size(basic) > MAX_FRAME:
        basic.pop()
    return basic


def _frame_size(peers: list[dict[str, Any]]) -> int:
    return len(json.dumps({"type": "peers", "peers": peers}).encode())


def _inbound_peers(items: object) -> list[dict[str, Any]]:
    """The valid entries of a peer list, with only the fields that peermesh knows. A bad entry
    costs only that entry, and a bad optional field costs only that field."""
    if not isinstance(items, list):
        raise _Closed("a peer list is not a list")
    return [_known_fields(item) for item in items[:MAX_PEERS] if _valid_peer(item)]


def _known_fields(item: dict[str, Any]) -> dict[str, Any]:
    # An older peermesh sends none of the optional fields.
    entry = {key: item.get(key) for key in _REQUIRED}
    if _matches(_NAME, item.get("auto_name")):
        entry["auto_name"] = item["auto_name"]
    capabilities = item.get("capabilities")
    if (
        isinstance(capabilities, list)
        and len(capabilities) <= MAX_CAPABILITIES
        and all(_matches(_CAPABILITY, c) for c in capabilities)
    ):
        entry["capabilities"] = capabilities
    seen = item.get("last_seen")
    # JSON true loads as True, which Python counts as an int. NaN fails both comparisons.
    if isinstance(seen, int | float) and not isinstance(seen, bool) and 0 <= seen <= MAX_SEEN:
        entry["last_seen"] = float(seen)
    return entry


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


def _encodes(value: object) -> bool:
    """True for text that UTF-8 can hold. JSON can carry a lone surrogate, which it cannot."""
    try:
        return isinstance(value, str) and bool(value.encode())
    except UnicodeEncodeError:
        return False


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
        and _encodes(record.get("body"))
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
            if status in _OPEN:
                self._open[msg_id] = status
            self._answer(msg_id, status, note)
        except (PeerError, OSError) as exc:
            self._answer(msg_id, "refused", _printable(exc))
        except _Closed:
            pass  # the sender resends on the next connection, and the ledger answers
        except Exception:
            # This runs in a worker, where an error reaches no one, and `serve` runs in a pane.
            # No answer goes back, so the sender resends until the message expires.
            with (self.folder / ERROR_LOG).open("a") as log:
                log.write(f"{time.ctime()} message {msg_id}\n{traceback.format_exc()}\n")
        finally:
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
        if not _matches(_MSG_ID, msg_id) or outcome not in _LATE:
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
            # The other machine keeps only the first MAX_PEERS entries.
            peers = [_outbound_peer(p) for p in self.mesh.local_peers()][:MAX_PEERS]
        except PeerError:
            return  # ps gave no answer, so the last list stays
        if peers != self._peers:
            self._peers = peers
            self._send({"type": "peers", "peers": _fit(peers)})

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
    # A pane gets the environment of the server, so the home is named here.
    serve_command = shlex.join(
        [
            "env",
            f"PEERMESH_HOME={paths.home()}",
            sys.executable,
            "-m",
            "peermesh.cli",
            "bridge",
            "serve",
            "--peer-host",
            host,
        ]
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
    words += [remote.get("peers", "peers"), "bridge", "dial", "--peer-host", remote["label"]]
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
    # A relative entry would name a folder of whichever project started this process.
    if plugin_dir and os.path.isabs(plugin_dir):
        env["PATH"] = f"{plugin_dir}{os.pathsep}{env.get('PATH', '')}"
    with log.open("w") as errors:
        # Its own session gives it no terminal, so the plugin cannot change the user's terminal,
        # and a signal to its process group also reaches the plugin that `aws` starts.
        try:
            link = subprocess.Popen(
                _connect_command(host),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return False, _printable(f"The connection did not start: {exc}")
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
    # `off` writes the hold and then reads this file, so one of the two sees the other.
    if _held(host):
        return
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
    if "plugin_dir" in remote:
        remote["plugin_dir"] = _from_here(remote["plugin_dir"])
    remote["label"] = check_label(remote.get("label") or socket.gethostname().split(".")[0])
    folder = paths.ensure_private_dir(paths.bridge_dir(host))
    _write_json(folder / REMOTE_FILE, remote)
    return f"The bridge to {host} is set up. Run `peers bridge on {host}` to connect."


@contextlib.contextmanager
def _file_lock(path: Path, *, wait: bool = True) -> Iterator[bool]:
    """Hold a lock on `path`. Yields False when `wait` is False and another process holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _link_lock(host: str, *, wait: bool = True) -> contextlib.AbstractContextManager[bool]:
    """The lock of `host`, around a running check and what depends on it."""
    return _file_lock(paths.bridge_dir(host) / LINK_LOCK, wait=wait)


def _link_alive(host: str) -> bool:
    """Whether a link process holds the lifetime lock of `host`. It holds the lock from the
    moment it exists, before it can write the link file."""
    try:
        fd = os.open(paths.bridge_dir(host) / LINK_LIFE, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        # Shared, so that two checks do not read each other as a link.
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _link_runs(host: str) -> bool:
    """Whether a link runs. A link of an earlier version has only its link file."""
    return _link_alive(host) or _running(host) is not None


def _rotate_log(log: Path) -> None:
    """Move a full log aside. A running link keeps writing to the file that it has open. The
    caller holds the lock of the log, so that a second writer does not move a new log over the
    old lines."""
    if log.exists() and log.stat().st_size >= MAX_LINK_LOG:
        os.replace(log, log.with_name(f"{LINK_LOG}.1"))


def _from_here(path: str | Path) -> str:
    """`path` as an absolute path. It keeps each `..`, which can follow a symlink elsewhere."""
    return os.path.join(os.getcwd(), path)


def _home() -> str:
    return _from_here(paths.home())


def _located(env: Mapping[str, str]) -> dict[str, str]:
    """`env` with each location absolute, because a link runs in its private folder."""
    found = {**env, "PEERMESH_HOME": _home()}
    for name in _OWNER_ENV:
        if found.get(name):
            found[name] = _from_here(found[name])
    return found


def _start_link(host: str, env: Mapping[str, str] | None = None) -> None:
    """Start the link process of `host`, with `env` or else this environment. The caller holds
    the lock of the host and found no running link."""
    folder = paths.bridge_dir(host)
    log = folder / LINK_LOG
    with _file_lock(folder / LOG_LOCK):
        _rotate_log(log)
    life = os.open(folder / LINK_LIFE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # The child shares this lock through the descriptor, so no moment exists when the process
        # runs and the lock is free, also if this process ends right after the start. Only a
        # check can hold it here, and only briefly.
        fcntl.flock(life, fcntl.LOCK_EX)
        with log.open("a") as errors:
            subprocess.Popen(
                # -P and the private folder keep the modules of a project out of the link.
                [sys.executable, "-P", "-m", "peermesh.cli", "bridge", "run", host],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                env=_located(os.environ if env is None else env),
                cwd=folder,
                pass_fds=(life,),
                start_new_session=True,
            )
    finally:
        os.close(life)


def _refuse_relative_plugin_dir(host: str, remote: Mapping[str, Any]) -> None:
    """An earlier setup stored the folder as given, and a link runs in its private folder."""
    plugin_dir = remote.get("plugin_dir")
    if plugin_dir and not os.path.isabs(plugin_dir):
        raise PeerError(
            f"The plugin folder of {host} is a relative path. "
            f"Run `peers bridge setup {host}` again with an absolute `--plugin-dir`."
        )


def turn_on(host: str, env: Mapping[str, str]) -> str:
    """Start the link process of `host`, and end the hold that `peers bridge off` left."""
    check_label(host)
    _refuse_agent(env)
    remote = _remote(host)
    if not remote:
        raise PeerError(f"The bridge to {host} is not set up. Run `peers bridge setup {host}`.")
    _refuse_relative_plugin_dir(host, remote)
    with _link_lock(host):
        (paths.bridge_dir(host) / HOLD_FILE).unlink(missing_ok=True)
        if _link_runs(host):
            return f"The bridge to {host} is already on."
        _start_link(host)
    return f"The bridge to {host} is on. Run `peers bridge status` to see the connection."


def enable_autostart(host: str, env: Mapping[str, str]) -> str:
    """Record that a registered session start can start the link of `host`, with the tools that
    the owner's PATH finds: the PATH of a desktop app session can be short."""
    check_label(host)
    _refuse_agent(env)
    remote = _remote(host)
    if not remote:
        raise PeerError(f"The bridge to {host} is not set up. Run `peers bridge setup {host}`.")
    _refuse_relative_plugin_dir(host, remote)
    plugin_dir = remote.get("plugin_dir")
    search = env.get("PATH", "")
    plugin_search = os.pathsep.join(filter(None, (plugin_dir, search)))
    aws = shutil.which("aws", path=search)
    plugin = shutil.which("session-manager-plugin", path=plugin_search)
    if aws is None or plugin is None:
        missing = "aws" if aws is None else "session-manager-plugin"
        raise PeerError(f"Autostart for {host} needs `{missing}`, and this PATH does not hold it.")
    # A delivery to a Codex session runs `codex`, and an npm install of it runs `node`. A plugin
    # folder from setup applies to the connection only, so it stays out of this search, unless
    # the plugin is not there and this PATH found it.
    in_setup_folder = plugin_dir and os.path.dirname(plugin) == plugin_dir.rstrip(os.sep)
    tools = [aws, *([] if in_setup_folder else [plugin])]
    tools += filter(None, (shutil.which(name, path=search) for name in ("codex", "node")))
    folders = {os.path.dirname(_from_here(tool)) for tool in tools}
    # In the owner's order, so that each name finds the same program as it does for the owner.
    order = [
        os.path.dirname(_from_here(os.path.join(entry, "tool")))
        for entry in search.split(os.pathsep)
    ]
    record = {
        "path": sorted(folders, key=order.index),
        "env": {name: _from_here(env[name]) for name in _OWNER_ENV if env.get(name)},
    }
    _write_json(paths.bridge_dir(host) / AUTOSTART_FILE, record)
    if _held(host):
        return (
            f"Autostart is on for {host}, and it waits: `peers bridge off` left a hold. "
            f"Run `peers bridge on {host}` to end the hold."
        )
    return f"Autostart is on for {host}. The next agent session that starts connects the bridge."


def disable_autostart(host: str, env: Mapping[str, str]) -> str:
    """Remove the autostart record of `host`. A running link keeps running."""
    check_label(host)
    _refuse_agent(env)
    record = paths.bridge_dir(host) / AUTOSTART_FILE
    if record.exists():
        with _link_lock(host):
            record.unlink(missing_ok=True)
    return f"Autostart is off for {host}. A running link keeps running."


def _autostart_record(host: str) -> dict[str, Any] | None:
    """The record of `host`, or None when it is absent or has a wrong shape. Any process of this
    user can write the file, so a value that it holds does not reach the link unchecked."""
    try:
        record = json.loads((paths.bridge_dir(host) / AUTOSTART_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    folders, owner_env = record.get("path"), record.get("env")
    if not isinstance(folders, list) or not isinstance(owner_env, dict):
        return None
    if not all(isinstance(folder, str) and os.path.isabs(folder) for folder in folders):
        return None
    if not all(
        name in _OWNER_ENV and isinstance(value, str) and os.path.isabs(value)
        for name, value in owner_env.items()
    ):
        return None
    return {"path": folders, "env": owner_env}


def _held(host: str) -> bool:
    return (paths.bridge_dir(host) / HOLD_FILE).exists()


def _session_env(env: Mapping[str, str]) -> dict[str, str]:
    return {name: env[name] for name in _SESSION_ENV if name in env}


def _link_env(record: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, str]:
    """The environment of a link that a session start begins, from an allowlist."""
    return {
        **_session_env(env),
        **record["env"],
        "PATH": os.pathsep.join([*record["path"], *_SYSTEM_PATH]),
        "PEERMESH_HOME": _home(),
    }


def _restore_link(host: str, env: Mapping[str, str]) -> None:
    if not (paths.bridge_dir(host) / AUTOSTART_FILE).exists():
        return
    # A process that finds the lock taken leaves the start to its holder.
    with _link_lock(host, wait=False) as locked:
        if not locked or _held(host) or not _remote(host) or _link_runs(host):
            return
        record = _autostart_record(host)
        if record is not None:
            _start_link(host, _link_env(record, env))


def _log_failure(host: str, failure: object) -> None:
    log = paths.bridge_dir(host) / LINK_LOG
    with contextlib.suppress(OSError):
        # A log that cannot rotate still takes the reason.
        with contextlib.suppress(OSError), _file_lock(log.with_name(LOG_LOCK)):
            _rotate_log(log)
        with log.open("a") as fh:
            fh.write(f"{time.ctime()} autostart failed: {_printable(failure)}\n")


def restore_links(env: Mapping[str, str]) -> None:
    """Start each link that has autostart on and no hold. Does not wait for a connection. A
    failure goes to the log of its host."""
    for host in _hosts():
        try:
            _restore_link(host, env)
        except Exception as exc:  # one host must not stop the others
            _log_failure(host, exc)


def _wants_restore(host: str) -> bool:
    try:
        return (
            (paths.bridge_dir(host) / AUTOSTART_FILE).exists()
            and not _held(host)
            and not _link_alive(host)
        )
    except OSError:
        # The restore process meets the same failure and writes it to the log of this host.
        return True


def request_restore(env: Mapping[str, str]) -> None:
    """Start a detached process that runs `restore_links` for the session start with `env`. A
    hook has a short time limit, and a process check can take longer."""
    hosts = [host for host in _hosts() if _wants_restore(host)]
    if not hosts:
        return
    try:
        subprocess.Popen(
            [sys.executable, "-P", "-m", "peermesh.cli", "bridge", "restore"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                **_session_env(env),
                "PATH": os.pathsep.join(_SYSTEM_PATH),
                "PEERMESH_HOME": _home(),
            },
            cwd=paths.bridge_dir(),
            start_new_session=True,
        )
    except OSError as exc:
        for host in hosts:
            _log_failure(host, exc)


def _hosts() -> list[str]:
    root = paths.bridge_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _signal_link(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def _end_link(mesh: Mesh, host: str, *, clean: bool = True) -> tuple[bool, str]:
    """End the link of `host`. With `clean`, also forget its peers and end what waits for it. The
    caller holds the lock of the host when `clean` is True."""
    try:
        signalled = _running(host)
        if signalled is not None:
            _signal_link(signalled)
        # The wait starts after the signal, because the first process check can take longer.
        deadline = time.monotonic() + STOP_WAIT_S
        while _link_runs(host) and time.monotonic() < deadline:
            # A link that is starting holds its lifetime lock and has not named its pid yet.
            pid = _running(host)
            if pid is not None and pid != signalled:
                _signal_link(pid)
                signalled = pid
            time.sleep(0.1)
        if _link_runs(host):
            # The link file holds the only record of this process, so it stays.
            running = _running(host) or "unknown"
            return False, f"The bridge to {host} did not stop: process {running} runs."
        if clean:
            mesh.expire_outbox(host, every=True)
            folder = paths.bridge_dir(host)
            (folder / PEERS_FILE).unlink(missing_ok=True)
            (folder / LINK_FILE).unlink(missing_ok=True)
    except (OSError, ProcessCheckError) as exc:
        return False, f"The bridge to {host} did not finish stopping: {_printable(exc)}."
    waits = f" Autostart waits for `peers bridge on {host}`." if _autostart_record(host) else ""
    return True, f"The bridge to {host} is off.{waits}"


def _end_host(mesh: Mesh, host: str, *, wait: bool) -> tuple[bool, str] | None:
    """Hold the link of `host` off and end it, under its lock, so that `on` runs before or after
    the whole stop. A hold or a lock that fails does not keep the link running. Returns None
    when `wait` is False and the lock is busy."""
    hold_failure = None
    try:
        with _link_lock(host, wait=wait) as locked:
            if not locked:
                return None
            try:
                (paths.bridge_dir(host) / HOLD_FILE).touch(mode=0o600)
            except OSError as exc:
                hold_failure = exc
            stopped, line = _end_link(mesh, host)
    except OSError as exc:
        _, line = _end_link(mesh, host, clean=False)
        return False, f"{line} Its lock failed, so its files stay: {_printable(exc)}."
    if hold_failure is not None:
        return False, f"{line} Its hold was not written: {_printable(hold_failure)}."
    return stopped, line


def turn_off(mesh: Mesh) -> tuple[list[str], bool]:
    """Hold each link off, end it, forget its peers, and end the messages that wait for it.
    Returns one line for each host, and whether each stop and each hold succeeded."""
    hosts = _hosts()
    # Before any process check, which can stall or fail. `_end_host` writes the hold again under
    # the lock that it keeps, because an `on` can run between the two.
    for host in hosts:
        with contextlib.suppress(OSError):
            (paths.bridge_dir(host) / HOLD_FILE).touch(mode=0o600)
    results: dict[str, tuple[bool, str]] = {}
    # A host with a busy lock waits until the other links have stopped.
    for wait in (False, True):
        for host in hosts:
            if host not in results and (result := _end_host(mesh, host, wait=wait)):
                results[host] = result
    lines = [results[host][1] for host in hosts]
    return lines or ["No bridge is on."], all(stopped for stopped, _ in results.values())


def status(mesh: Mesh) -> list[dict[str, Any]]:
    """One entry for each host, with the reason of the last failure."""
    now = time.time()
    found: list[dict[str, Any]] = []
    for host in _hosts():
        state = link_state(host)
        autostart = _autostart_record(host) is not None
        hold = _held(host)
        lock_error = ""
        try:
            alive = _link_alive(host)
        except OSError as exc:
            alive, lock_error = False, f"The link lock cannot be read: {_printable(exc)}"
        if state or autostart or hold or alive or lock_error:
            found.append(
                {
                    "host": host,
                    "connected": link_up(state, now),
                    "running": alive or _running(host) is not None,
                    "waiting": len(mesh.outbox(host).waiting(OUTBOX)),
                    "error": lock_error or state.get("error", ""),
                    "autostart": autostart,
                    "hold": hold,
                }
            )
    return found
