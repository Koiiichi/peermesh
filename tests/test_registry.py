from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from peermesh import paths, registry
from peermesh.ledger import Ledger
from peermesh.registry import (
    Peer,
    Registry,
    claude_native_status,
    claude_user_name,
    normalize_remote,
    proc_start,
    repo_info,
    scoped,
)
from peermesh.service import Mesh


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


def test_live_skips_dead_reused_and_corrupt_and_prune_removes_the_exited(
    make_peer: Callable[..., Peer],
) -> None:
    reg = Registry()
    alive = make_peer(id="claude:alive")
    reg.put(alive)
    reg.put(make_peer(id="claude:dead", pid=dead_pid()))
    reg.put(make_peer(id="codex:reused", runtime="codex", proc_start="Thu Jan  1 00:00:00 1970"))
    (reg.dir / "broken.json").write_text("{not json")
    assert [p.id for p in reg.live()] == ["claude:alive"]
    # Reading deletes nothing; the locked prune step removes the records.
    assert len(list(reg.dir.glob("*.json"))) == 4
    Mesh(reg, Ledger(), {}).prune()
    # A record that does not parse stays: it is not a session that ended.
    assert sorted(f.name for f in reg.dir.glob("*.json")) == ["broken.json", "claude_alive.json"]


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


def test_claude_session_record_is_read_as_utf8() -> None:
    # Claude Code writes UTF-8. Under a Latin-1 locale, Python's default reads "é" as "Ã©".
    locales = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
    # macOS lists en_US.ISO8859-1, Linux en_US.iso88591.
    latin1 = [n for n in locales if n.lower().replace("-", "") == "en_us.iso88591"]
    if not latin1:
        pytest.skip("no en_US Latin-1 locale is installed")
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    record = {"sessionId": "S", "name": "café", "messagingSocketPath": "/tmp/café.sock"}
    (sessions / "41.json").write_bytes(json.dumps(record, ensure_ascii=False).encode())
    probe = "from peermesh import registry; print(ascii(registry.claude_session(41)))"
    env = {**os.environ, "LC_ALL": latin1[0], "PYTHONUTF8": "0"}
    out = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == ascii(record)


def rename(pid: int, name: str, source: str | None = "user") -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    record = {"name": name} if source is None else {"name": name, "nameSource": source}
    (sessions / f"{pid}.json").write_text(json.dumps(record))


def test_claude_user_name_reads_only_user_renames() -> None:
    rename(21, "alpha-infra")
    rename(22, "alpha-09", source="derived")
    rename(23, " Docs pass / beta ")
    assert claude_user_name(21) == "alpha-infra"
    assert claude_user_name(22) is None
    assert claude_user_name(23) == "Docs-pass-beta"
    assert claude_user_name(24) is None


def test_claude_user_name_reads_a_name_given_at_start_and_a_peer_rename() -> None:
    # The Claude desktop app records a name given when the session starts with no source.
    rename(31, "alpha-arch", source=None)
    rename(32, "alpha-review", source="peer")
    assert claude_user_name(31) == "alpha-arch"
    assert claude_user_name(32) == "alpha-review"
    for pid, source in enumerate(("auto", "hook", "collision", "derived"), start=33):
        rename(pid, "not-a-user-name", source=source)
        assert claude_user_name(pid) is None, source


def test_user_rename_replaces_name_and_keeps_given_name(make_peer: Callable[..., Peer]) -> None:
    reg = Registry()
    peer = make_peer()
    reg.put(peer)
    rename(peer.pid, "alpha-infra")
    [live] = reg.live()
    assert (live.name, live.auto_name) == ("alpha-infra", "claude-a-aa")
    got = reg.get(peer.id)
    assert got is not None and got.name == "alpha-infra"
    for ref in ("alpha-infra", "claude-a-aa", peer.id):
        found = reg.resolve(ref)
        assert found is not None and found.id == peer.id
    reg.put(got)
    assert json.loads(reg._file(peer.id).read_text())["name"] == "claude-a-aa"
    # Callers see the name that the user gave; the record keeps the name that peermesh gave.
    assert (got.to_json()["name"], got.to_json()["auto_name"]) == ("alpha-infra", "claude-a-aa")


def test_a_name_that_two_sessions_want_names_neither(make_peer: Callable[..., Peer]) -> None:
    # The two peers share one process, so both want the name in its session record.
    reg = Registry()
    reg.put(make_peer(id="claude:one", name="claude-a-01"))
    reg.put(make_peer(id="claude:two", name="claude-a-02"))
    rename(make_peer().pid, "alpha-infra")
    assert {p.id: p.name for p in reg.live()} == {
        "claude:one": "claude-a-01",
        "claude:two": "claude-a-02",
    }
    assert reg.resolve("alpha-infra") is None
    assert {p.id for p in reg.claimants("alpha-infra")} == {"claude:one", "claude:two"}


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
    Mesh(reg, Ledger(), {}).prune()
    assert reg.get("codex:closed") is None


def test_record_from_newer_version_loads(make_peer: Callable[..., Peer]) -> None:
    data = make_peer().to_json() | {"field_from_the_future": 1}
    assert Peer.from_json(data).id == "claude:s1"


def _other_process(make_peer: Callable[..., Peer], **fields: Any) -> Peer:
    # A second live process, so that the two peers have Claude session records of their own.
    pid = os.getppid()
    return make_peer(pid=pid, proc_start=proc_start(pid) or "", **fields)


def _name_record(pid: int, name: str, since: int) -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({"name": name, "nameSince": since}))


def test_a_new_name_does_not_take_the_given_name_of_a_renamed_peer(
    make_peer: Callable[..., Peer],
) -> None:
    # The ids claude:0001 and claude:0012 have the same first two hex digits of their sha1.
    reg = Registry()
    first = make_peer(id="claude:0001", name="", git_root="/r/demo")
    first.name = reg.unique_name(first)
    reg.put(first)
    rename(first.pid, "review", source=None)
    second = _other_process(make_peer, id="claude:0012", name="", git_root="/r/demo")
    second.name = reg.unique_name(second)
    assert second.name != first.name
    reg.put(second)
    for peer in (first, second):
        found = reg.resolve(peer.name)
        assert found is not None and found.id == peer.id


def test_a_listed_name_never_reaches_another_live_session(
    make_peer: Callable[..., Peer],
) -> None:
    reg = Registry()
    first = make_peer(id="claude:z", name="claude-demo-11")
    reg.put(first)
    _name_record(first.pid, "review", since=1000)
    found = reg.resolve("review")
    assert found is not None and found.id == "claude:z"
    # Another session wants the same name: the name stops working, and no address moves.
    second = _other_process(make_peer, id="claude:a", name="claude-demo-22")
    reg.put(second)
    _name_record(second.pid, "review", since=2000)
    assert reg.resolve("review") is None
    for peer in (first, second):
        found = reg.resolve(peer.name)
        assert found is not None and found.id == peer.id


def test_no_address_moves_when_a_session_that_wants_the_same_name_ends(
    make_peer: Callable[..., Peer],
) -> None:
    sleepers = [subprocess.Popen(["sleep", "30"]) for _ in range(2)]
    try:
        reg = Registry()
        a, b = (
            make_peer(
                id=f"claude:{key}",
                name=f"claude-x-{key}{key}",
                pid=proc.pid,
                proc_start=proc_start(proc.pid) or "",
            )
            for key, proc in zip("ab", sleepers, strict=True)
        )
        c = make_peer(id="claude:c", name="claude-x-cc")
        for peer in (a, b, c):
            reg.put(peer)
        _name_record(a.pid, "review", since=1000)
        _name_record(b.pid, "review", since=2000)
        _name_record(c.pid, "review-46f4", since=3000)
        listed = {p.id: p.name for p in reg.live()}
        assert listed == {
            "claude:a": "claude-x-aa",
            "claude:b": "claude-x-bb",
            "claude:c": "review-46f4",
        }
        sleepers[0].kill()
        sleepers[0].wait()
        for peer_id in ("claude:b", "claude:c"):
            found = reg.resolve(listed[peer_id])
            assert found is not None and found.id == peer_id
        # The one session that still wants the name now has it.
        found = reg.resolve("review")
        assert found is not None and found.id == "claude:b"
    finally:
        for proc in sleepers:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the kernel start time")
def test_process_began_is_the_start_of_the_process_on_the_wall_clock() -> None:
    before = time.time()
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from peermesh.registry import process_began; print(process_began())",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    began = float(out)
    # A tick of the Linux clock is 10 ms.
    assert before - 0.02 <= began <= time.time()
