"""The peer registry: one JSON record per live session, pruned on every read."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, fields
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

    def to_json(self) -> dict[str, Any]:
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


def _claude_session(pid: int) -> dict[str, Any] | None:
    """Claude Code's own session file. The format is internal; failure is normal."""
    try:
        data = json.loads((paths.claude_dir() / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claude_native_state(pid: int) -> tuple[Status, float] | None:
    """Claude Code's own session status and the epoch second it changed.

    The time is infinite when the file does not give it, so the native status wins.
    """
    data = _claude_session(pid)
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


def claude_user_name(pid: int) -> str | None:
    """The name that the user gave the Claude session with `/rename`, made safe to type, or None.

    A name that Claude Code derived from the directory has nameSource "derived" and is ignored.
    """
    data = _claude_session(pid)
    if data is None or data.get("nameSource") != "user" or not isinstance(data.get("name"), str):
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
            json.dump(peer.to_json(), fh)
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

    def resolve(self, target: str) -> Peer | None:
        for peer in self.live():
            if target in (peer.id, peer.name, peer.auto_name):
                return peer
        return None

    def unique_name(self, peer: Peer) -> str:
        taken = {p.name for p in self.live() if p.id != peer.id}
        digest = hashlib.sha1(peer.id.encode()).hexdigest()
        for width in (2, 4, 8, 40):
            name = f"{peer.runtime}-{_label(peer)}-{digest[:width]}"
            if name not in taken:
                return name
        raise RuntimeError("A full sha1 name collides. This cannot occur.")


def _apply_user_names(peers: list[Peer]) -> None:
    """Replace the name of each renamed Claude session with the name that the user gave it.

    A rename that another peer already uses gets a suffix from the session id. Sessions are
    taken in id order, so every caller gives the same name to the same session.
    """
    taken = {p.name for p in peers}
    for peer in sorted(peers, key=lambda p: p.id):
        wanted = claude_user_name(peer.pid) if peer.runtime == "claude" else None
        if wanted is None or wanted == peer.name:
            continue
        if wanted in taken:
            wanted = f"{wanted}-{hashlib.sha1(peer.id.encode()).hexdigest()[:4]}"
            if wanted in taken:
                continue
        taken.add(wanted)
        peer.auto_name, peer.name = peer.name, wanted
