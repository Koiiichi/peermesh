"""The peer registry: one JSON record per live session, pruned on every read."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
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

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Peer:
        return cls(**data)


def proc_start(pid: int) -> str | None:
    """Return the start time `ps` prints for pid, or None when the pid is not running."""
    if pid <= 0:
        return None
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or None


def is_alive(peer: Peer) -> bool:
    """A record is live when its pid runs and still has the recorded start time."""
    return proc_start(peer.pid) == peer.proc_start


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
    try:
        data = json.loads((paths.claude_dir() / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    raw = data.get("status") if isinstance(data, dict) else None
    if raw == "busy":
        return "busy"
    if raw in ("idle", "waiting"):
        return "idle"
    return None


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
            return Peer.from_json(json.loads(self._file(peer_id).read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def remove(self, peer_id: str) -> None:
        self._file(peer_id).unlink(missing_ok=True)

    def live(self) -> list[Peer]:
        """Live peers. Records of dead processes, reused pids and corrupt files are deleted."""
        if not self.dir.is_dir():
            return []
        peers: list[Peer] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                peer = Peer.from_json(json.loads(path.read_text()))
            except (OSError, ValueError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if not is_alive(peer):
                path.unlink(missing_ok=True)
                continue
            peers.append(peer)
        return peers

    def resolve(self, target: str) -> Peer | None:
        for peer in self.live():
            if target in (peer.id, peer.name):
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
