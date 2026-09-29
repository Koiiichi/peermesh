from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from peermesh import paths
from peermesh.registry import (
    Peer,
    Registry,
    claude_native_status,
    normalize_remote,
    repo_info,
    scoped,
)


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_put_get_roundtrip_private_mode(make_peer: Callable[..., Peer]) -> None:
    reg = Registry()
    peer = make_peer()
    reg.put(peer)
    assert reg.get(peer.id) == peer
    files = list(reg.dir.glob("*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(reg.dir.stat().st_mode) == 0o700


def test_live_prunes_dead_reused_and_corrupt(make_peer: Callable[..., Peer]) -> None:
    reg = Registry()
    alive = make_peer(id="claude:alive")
    reg.put(alive)
    reg.put(make_peer(id="claude:dead", pid=dead_pid()))
    reg.put(make_peer(id="codex:reused", runtime="codex", proc_start="Thu Jan  1 00:00:00 1970"))
    (reg.dir / "broken.json").write_text("{not json")
    assert [p.id for p in reg.live()] == ["claude:alive"]
    assert sorted(f.name for f in reg.dir.glob("*.json")) == ["claude_alive.json"]


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Org/Repo.git",
        "https://github.com/org/repo",
        "https://github.com/org/repo.git/",
        "ssh://git@github.com/org/repo.git",
    ],
)
def test_normalize_remote(url: str) -> None:
    assert normalize_remote(url) == "github.com/org/repo"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_repo_info_matches_worktrees_of_one_repo(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "f.txt").write_text("x")
    _git(main, "add", "f.txt")
    _git(main, "commit", "-qm", "init")
    _git(main, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature")
    a = repo_info(str(main))
    b = repo_info(str(tmp_path / "wt"))
    assert a.repo_key == b.repo_key is not None
    assert a.git_root == b.git_root == str(main.resolve())
    assert a.worktree != b.worktree
    assert b.branch == "feature"


def test_repo_info_outside_git(tmp_path: Path) -> None:
    info = repo_info(str(tmp_path))
    assert (info.git_root, info.worktree, info.branch, info.repo_key) == (None, None, None, None)


def test_unique_name_extends_on_collision(make_peer: Callable[..., Peer]) -> None:
    reg = Registry()
    first = make_peer(id="claude:one", git_root="/r/Shop Repo")
    first.name = reg.unique_name(first)
    reg.put(first)
    assert first.name.startswith("claude-shop-repo-")
    second = make_peer(id="claude:two", git_root="/r/Shop Repo")
    first.name = f"claude-shop-repo-{hashlib.sha1(b'claude:two').hexdigest()[:2]}"
    reg.put(first)
    name = reg.unique_name(second)
    assert name != first.name
    assert len(name.rsplit("-", 1)[1]) == 4


def test_scoped_five_peers(make_peer: Callable[..., Peer]) -> None:
    me = make_peer(id="claude:me", worktree="/r/a", repo_key="k/a")
    peers = [
        me,
        make_peer(id="claude:b", name="b", worktree="/r/a-wt", repo_key="k/a"),
        make_peer(id="codex:c", name="c", runtime="codex", worktree="/r/a", repo_key="k/a"),
        make_peer(id="codex:d", name="d", runtime="codex", worktree="/r/x", repo_key="k/x"),
        make_peer(id="codex:e", name="e", runtime="codex", worktree="/r/a-wt2", repo_key="k/a"),
    ]
    assert [p.name for p in scoped(peers, me, "repo")] == ["c", "b", "e"]
    assert [p.name for p in scoped(peers, me, "worktree")] == ["c"]
    assert [p.name for p in scoped(peers, me, "all")] == ["c", "b", "d", "e"]


def test_claude_native_status_mapping() -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "11.json").write_text(json.dumps({"status": "busy"}))
    (sessions / "12.json").write_text(json.dumps({"status": "waiting"}))
    (sessions / "13.json").write_text("garbage")
    assert claude_native_status(11) == "busy"
    assert claude_native_status(12) == "idle"
    assert claude_native_status(13) is None
    assert claude_native_status(14) is None


def test_liveness_ignores_locale_and_timezone(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    reg = Registry()
    reg.put(make_peer(id="claude:x"))
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LC_ALL", "C")
    assert [p.id for p in reg.live()] == ["claude:x"]
