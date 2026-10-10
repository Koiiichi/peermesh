from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from peermesh import paths, registry
from peermesh.registry import (
    Peer,
    Registry,
    claude_native_status,
    claude_user_name,
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


def rename(pid: int, name: str, source: str = "user") -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({"name": name, "nameSource": source}))


def test_claude_user_name_reads_only_user_renames() -> None:
    rename(21, "rotom-infra")
    rename(22, "rotom-09", source="derived")
    rename(23, " Task gen / beta ")
    assert claude_user_name(21) == "rotom-infra"
    assert claude_user_name(22) is None
    assert claude_user_name(23) == "Task-gen-beta"
    assert claude_user_name(24) is None


def test_user_rename_replaces_name_and_keeps_given_name(make_peer: Callable[..., Peer]) -> None:
    reg = Registry()
    peer = make_peer()
    reg.put(peer)
    rename(peer.pid, "rotom-infra")
    [live] = reg.live()
    assert (live.name, live.auto_name) == ("rotom-infra", "claude-a-aa")
    got = reg.get(peer.id)
    assert got is not None and got.name == "rotom-infra"
    for ref in ("rotom-infra", "claude-a-aa", peer.id):
        found = reg.resolve(ref)
        assert found is not None and found.id == peer.id
    reg.put(got)
    assert json.loads(reg._file(peer.id).read_text())["name"] == "claude-a-aa"


def test_user_rename_in_use_gets_suffix(make_peer: Callable[..., Peer]) -> None:
    reg = Registry()
    reg.put(make_peer(id="claude:one", name="claude-a-01"))
    reg.put(make_peer(id="claude:two", name="claude-a-02"))
    rename(make_peer().pid, "rotom-infra")
    names = {p.id: p.name for p in reg.live()}
    suffix = hashlib.sha1(b"claude:two").hexdigest()[:4]
    assert names == {"claude:one": "rotom-infra", "claude:two": f"rotom-infra-{suffix}"}


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


def test_live_runs_ps_once_for_all_peers(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = Registry()
    for i in range(4):
        reg.put(make_peer(id=f"claude:s{i}", name=f"p{i}"))
    calls: list[list[str]] = []
    real = registry.subprocess.run

    def counting(args: list[str], **kw: Any) -> Any:
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(registry.subprocess, "run", counting)
    assert len(reg.live()) == 4
    assert len([c for c in calls if c[0] == "ps"]) == 1


def test_codex_record_without_rollout_is_pruned(
    make_peer: Callable[..., Peer], tmp_path: Path
) -> None:
    reg = Registry()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("")
    reg.put(make_peer(id="codex:open", runtime="codex", transcript=str(rollout)))
    reg.put(make_peer(id="codex:closed", runtime="codex", transcript=str(tmp_path / "gone")))
    assert [p.id for p in reg.live()] == ["codex:open"]
    assert reg.get("codex:closed") is None


def test_record_from_newer_version_loads(make_peer: Callable[..., Peer]) -> None:
    data = make_peer().to_json() | {"field_from_the_future": 1}
    assert Peer.from_json(data).id == "claude:s1"
