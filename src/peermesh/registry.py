"""The peer registry: one JSON record per live session, pruned on every read."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from peermesh import paths

Runtime = Literal["claude", "codex"]
Status = Literal["busy", "idle", "offline"]
Scope = Literal["repo", "worktree", "all"]


@dataclass
class Peer:
    id: str
    name: str
    runtime: Runtime
    pid: int
    proc_start: str
    cwd: str
    git_root: str | None
    worktree: str | None
    branch: str | None
    repo_key: str | None
    status: Status
    last_seen: float
    endpoint: str
    host: str = "local"
    capabilities: list[str] = field(default_factory=list)
    # The Codex rollout file of the thread. An app-server process hosts many threads, so the
    # process can outlive a thread; a missing rollout file means the thread is closed.
    transcript: str | None = None
    # The name that peermesh gave the session, set when a user rename replaces `name`. The
    # rename exists only in memory, so the record keeps the given name as a stable address.
    auto_name: str | None = None

    @property
    def has_tools(self) -> bool:
        """True when the session has the peermesh MCP tools: Claude Code or the Codex plugin."""
        return self.runtime == "claude" or "mcp_tools" in self.capabilities

    def to_json(self) -> dict[str, Any]:
        """The peer as callers see it: `name` is the name that the user gave the session, and
        `auto_name` is the name that peermesh gave it, or None when the user gave none."""
        return asdict(self)

    def to_record(self) -> dict[str, Any]:
        """The peer as the registry stores it, under the name that peermesh gave it."""
        data = asdict(self)
        data["name"] = data.pop("auto_name") or self.name
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Peer:
        # Records written by a newer peermesh can carry fields that this version does not know.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def proc_start(pid: int) -> str | None:
    """Return the start time `ps` prints for pid, or None when the pid is not running."""
    return proc_starts([pid]).get(pid)


def proc_starts(pids: list[int]) -> dict[int, str]:
    """The start time `ps` prints for each running pid, from one `ps` call."""
    wanted = sorted({pid for pid in pids if pid > 0})
    if not wanted:
        return {}
    # lstart text depends on locale and time zone; pin both so every session compares alike.
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    result = subprocess.run(
        ["ps", "-o", "pid=,lstart=", "-p", ",".join(map(str, wanted))],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    starts: dict[int, str] = {}
    for line in result.stdout.splitlines():
        pid_text, _, start = line.strip().partition(" ")
        if pid_text.isdigit() and start.strip():
            starts[int(pid_text)] = start.strip()
    return starts


def is_alive(peer: Peer, starts: dict[int, str] | None = None) -> bool:
    """A record is live when its pid runs with the recorded start time, and its thread is open.

    `starts` is a result of `proc_starts` that covers peer.pid; without it, `ps` runs once.
    """
    if starts is None:
        starts = proc_starts([peer.pid])
    if starts.get(peer.pid) != peer.proc_start:
        return False
    return peer.transcript is None or Path(peer.transcript).exists()


@dataclass(frozen=True)
class RepoInfo:
    git_root: str | None
    worktree: str | None
    branch: str | None
    repo_key: str | None


def normalize_remote(url: str) -> str:
    """Reduce ssh, scp-style and https remotes of one repository to one key."""
    text = url.strip()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.sub(r"^[^@/]+@", "", text)
    if re.match(r"^[^/]+:[^/]", text):
        text = text.replace(":", "/", 1)
    text = re.sub(r"\.git/?$", "", text.rstrip("/"))
    return text.lower()


def repo_info(cwd: str) -> RepoInfo:
    def git(*args: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, timeout=5
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    top = git("rev-parse", "--show-toplevel")
    if top is None:
        return RepoInfo(None, None, None, None)
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    origin = git("config", "--get", "remote.origin.url")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git_root = str(Path(common).resolve().parent) if common else str(Path(top).resolve())
    key = normalize_remote(origin) if origin else (str(Path(common).resolve()) if common else top)
    return RepoInfo(
        git_root=git_root, worktree=str(Path(top).resolve()), branch=branch, repo_key=key
    )


def current_branch(cwd: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _label(peer: Peer) -> str:
    base = Path(peer.git_root or peer.cwd).name.lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "session"


def _same_repo(a: Peer, b: Peer) -> bool:
    if a.repo_key and b.repo_key:
        return a.repo_key == b.repo_key
    return a.cwd == b.cwd


def scoped(peers: list[Peer], me: Peer, scope: Scope) -> list[Peer]:
    """Peers other than me, filtered by scope; same worktree first, then by name."""
    others = [p for p in peers if p.id != me.id]
    if scope == "worktree":
        others = [p for p in others if p.worktree is not None and p.worktree == me.worktree]
    elif scope == "repo":
        others = [p for p in others if _same_repo(p, me)]
    return sorted(others, key=lambda p: (p.worktree is None or p.worktree != me.worktree, p.name))


def claude_native_status(pid: int) -> Status | None:
    """Read Claude Code's own session status. The file format is internal; failure is normal."""
    state = claude_native_state(pid)
    return state[0] if state else None


def claude_session(pid: int) -> dict[str, Any] | None:
    """Claude Code's own session file. The format is internal; failure is normal."""
    try:
        # Claude Code writes UTF-8, whatever the locale of this process is.
        text = (paths.claude_dir() / "sessions" / f"{pid}.json").read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claude_native_state(pid: int) -> tuple[Status, float] | None:
    """Claude Code's own session status and the epoch second it changed.

    The time is infinite when the file does not give it, so the native status wins.
    """
    data = claude_session(pid)
    if data is None:
        return None
    raw = data.get("status")
    status: Status
    if raw == "busy":
        status = "busy"
    elif raw in ("idle", "waiting"):
        status = "idle"
    else:
        return None
    changed = data.get("statusUpdatedAt")
    return status, changed / 1000 if isinstance(changed, int | float) else float("inf")


# A turn writes much more than its start and end, so the tail holds the last turn events of a
# rollout unless one tool output is larger than this.
ROLLOUT_TAIL = 1 << 20
_TURN_EVENTS = (b"task_started", b"task_complete", b"turn_aborted")


def codex_native_state(rollout: str) -> tuple[Status, float] | None:
    """The status that the last top-level turn event of a Codex rollout shows, and its epoch second.

    Codex writes the end of a turn also when it runs no Stop hook, as after a turn that ends with
    an error. A nested turn, such as a guardian review, has another root_turn_id than its turn_id
    and runs inside a top-level turn, so it is ignored. The rollout format is internal; None when
    the file or the end of its tail shows no top-level turn event.
    """
    try:
        with open(rollout, "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - ROLLOUT_TAIL))
            tail = fh.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        kind = next((k for k in _TURN_EVENTS if b'"' + k + b'"' in line), None)
        if kind is None:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # the first line of the tail can be a part of a line
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != kind.decode():
            continue
        if payload.get("root_turn_id", payload.get("turn_id")) != payload.get("turn_id"):
            continue
        try:
            when = datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            continue
        status: Status = "busy" if kind == b"task_started" else "idle"
        return status, when.timestamp()
    return None


def claude_user_name(pid: int) -> str | None:
    """The name that the user gave the Claude session, made safe to type, or None.

    Claude Code records the source of a name. A name given when the session starts has no
    source, and a rename has "user" or "peer"; Claude Code treats these three as the name of the
    session. A name from the folder, the first prompt, a hook or a name collision ("derived",
    "auto", "hook", "collision") is ignored.
    """
    data = claude_session(pid)
    if data is None or data.get("nameSource") not in (None, "user", "peer"):
        return None
    if not isinstance(data.get("name"), str):
        return None
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", data["name"]).strip("-")[:40] or None


class Registry:
    def __init__(self, root: Path | None = None) -> None:
        self.dir = (root or paths.home()) / "peers"

    def _file(self, peer_id: str) -> Path:
        return self.dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", peer_id) + ".json")

    def put(self, peer: Peer) -> None:
        paths.ensure_private_dir(self.dir)
        target = self._file(peer.id)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(peer.to_record(), fh)
        os.replace(tmp, target)

    def get(self, peer_id: str) -> Peer | None:
        try:
            peer = Peer.from_json(json.loads(self._file(peer_id).read_text()))
        except (OSError, ValueError, TypeError):
            return None
        if peer.runtime == "claude":
            # A user rename is unique only among the live peers, so take it from the live list.
            return next((p for p in self.live() if p.id == peer_id), peer)
        return peer

    def remove(self, peer_id: str) -> None:
        self._file(peer_id).unlink(missing_ok=True)

    def live(self) -> list[Peer]:
        """Live peers. Records of dead processes, reused pids and corrupt files are deleted."""
        if not self.dir.is_dir():
            return []
        records: list[tuple[Path, Peer]] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                records.append((path, Peer.from_json(json.loads(path.read_text()))))
            except (OSError, ValueError, TypeError):
                path.unlink(missing_ok=True)
        starts = proc_starts([peer.pid for _, peer in records])
        peers: list[Peer] = []
        for path, peer in records:
            if not is_alive(peer, starts):
                path.unlink(missing_ok=True)
                continue
            peers.append(peer)
        _apply_user_names(peers)
        return peers

    def claimants(self, name: str) -> list[Peer]:
        """The live peers whose user names are `name`, when more than one wants it."""
        peers = [p for p in self.live() if p.runtime == "claude" and p.name != name]
        found = [p for p in peers if claude_user_name(p.pid) == name]
        return found if len(found) > 1 else []

    def resolve(self, target: str) -> Peer | None:
        for peer in self.live():
            if target in (peer.id, peer.name, peer.auto_name):
                return peer
        return None

    def unique_name(self, peer: Peer) -> str:
        # A renamed peer keeps its given name as an address, so that name stays taken.
        others = [p for p in self.live() if p.id != peer.id]
        taken = {p.name for p in others} | {p.auto_name for p in others if p.auto_name}
        digest = hashlib.sha1(peer.id.encode()).hexdigest()
        for width in (2, 4, 8, 40):
            name = f"{peer.runtime}-{_label(peer)}-{digest[:width]}"
            if name not in taken:
                return name
        raise RuntimeError("A full sha1 name collides. This cannot occur.")


def _apply_user_names(peers: list[Peer]) -> None:
    """Replace the name of each named Claude session with the name that the user gave it.

    The user's name replaces the automatic name only when no other live peer wants that name or
    has it as an automatic name. So an address never moves from one live session to another:
    an automatic name belongs to one session, and a user name names only its one claimant.
    """
    automatic = {p.name for p in peers}
    claims: dict[str, list[Peer]] = {}
    for peer in peers:
        wanted = claude_user_name(peer.pid) if peer.runtime == "claude" else None
        if wanted is not None and wanted != peer.name:
            claims.setdefault(wanted, []).append(peer)
    for wanted, owners in claims.items():
        if len(owners) == 1 and wanted not in automatic:
            owners[0].auto_name, owners[0].name = owners[0].name, wanted
