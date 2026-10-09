from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from peermesh import hooks, paths
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry
from peermesh.service import Mesh


def _mesh() -> Mesh:
    return Mesh(Registry(), Ledger(), {})


def test_session_start_registers_and_returns_context(make_peer: Callable[..., Peer]) -> None:
    mesh = _mesh()
    mesh.registry.put(
        make_peer(id="claude:other", name="claude-other", repo_key=None, cwd=os.getcwd())
    )
    out = hooks.handle(
        mesh,
        "session-start",
        "codex",
        {"session_id": "T1", "cwd": os.getcwd()},
        env={},
        host_pid=os.getpid(),
    )
    assert out is not None
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert "claude-other" in ctx["additionalContext"]
    peer = mesh.registry.get("codex:T1")
    assert peer is not None and peer.status == "idle"


def test_codex_hook_prefers_payload_over_inherited_env() -> None:
    mesh = _mesh()
    hooks.handle(
        mesh,
        "session-start",
        "codex",
        {"session_id": "CHILD"},
        env={"CODEX_THREAD_ID": "PARENT"},
        host_pid=os.getpid(),
    )
    assert mesh.registry.get("codex:CHILD") is not None
    assert mesh.registry.get("codex:PARENT") is None


def test_codex_hook_falls_back_to_env() -> None:
    mesh = _mesh()
    hooks.handle(
        mesh, "session-start", "codex", {}, env={"CODEX_THREAD_ID": "THR"}, host_pid=os.getpid()
    )
    assert mesh.registry.get("codex:THR") is not None


def test_prompt_sets_busy_and_notices_changes_once(make_peer: Callable[..., Peer]) -> None:
    mesh = _mesh()
    payload = {"session_id": "C1", "cwd": os.getcwd()}
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock"}
    hooks.handle(mesh, "session-start", "claude", payload, env=env, host_pid=os.getpid())
    assert hooks.handle(mesh, "prompt", "claude", payload, env=env, host_pid=os.getpid()) is None
    peer = mesh.registry.get("claude:C1")
    assert peer is not None and peer.status == "busy"
    mesh.registry.put(
        make_peer(
            id="codex:new",
            name="codex-new",
            runtime="codex",
            repo_key=peer.repo_key,
            cwd=peer.cwd,
        )
    )
    out = hooks.handle(mesh, "prompt", "claude", payload, env=env, host_pid=os.getpid())
    assert out is not None and "codex-new" in out["hookSpecificOutput"]["additionalContext"]
    assert hooks.handle(mesh, "prompt", "claude", payload, env=env, host_pid=os.getpid()) is None


def test_stop_sets_idle_and_end_removes() -> None:
    mesh = _mesh()
    payload = {"session_id": "T2"}
    hooks.handle(mesh, "prompt", "codex", payload, env={}, host_pid=os.getpid())
    hooks.handle(mesh, "stop", "codex", payload, env={}, host_pid=os.getpid())
    peer = mesh.registry.get("codex:T2")
    assert peer is not None and peer.status == "idle"
    hooks.handle(mesh, "session-end", "codex", payload, env={}, host_pid=os.getpid())
    assert mesh.registry.get("codex:T2") is None


def test_hook_cli_never_fails() -> None:
    env = {**os.environ, "PEERMESH_HOME": "/dev/null/not-a-dir"}
    for stdin in ("{not json", '{"session_id": "X"}', ""):
        result = subprocess.run(
            [sys.executable, "-m", "peermesh.cli", "hook", "session-start", "--runtime", "codex"],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout == ""


def test_hook_errors_are_logged() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "peermesh.cli", "hook", "session-start", "--runtime", "codex"],
        input='{"session_id": "X"}',
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert (paths.home() / "hook-errors.log").exists()


def _git_repo(path: Path) -> Path:
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return path


def test_prompt_refreshes_branch_and_location(tmp_path: Path) -> None:
    mesh = _mesh()
    first, second = _git_repo(tmp_path / "one"), _git_repo(tmp_path / "two")
    payload = {"session_id": "T5", "cwd": str(first)}
    hooks.handle(mesh, "session-start", "codex", payload, env={}, host_pid=os.getpid())
    subprocess.run(["git", "-C", str(first), "checkout", "-q", "-b", "feature"], check=True)
    hooks.handle(mesh, "prompt", "codex", payload, env={}, host_pid=os.getpid())
    peer = mesh.registry.get("codex:T5")
    assert peer is not None and peer.branch == "feature"
    payload["cwd"] = str(second)
    hooks.handle(mesh, "prompt", "codex", payload, env={}, host_pid=os.getpid())
    peer = mesh.registry.get("codex:T5")
    assert peer is not None and peer.branch == "main"
    assert peer.worktree == str(second.resolve()) and peer.cwd == str(second)


def test_codex_hook_records_rollout(tmp_path: Path) -> None:
    mesh = _mesh()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("")
    payload = {"session_id": "T6", "cwd": os.getcwd(), "transcript_path": str(rollout)}
    hooks.handle(mesh, "session-start", "codex", payload, env={}, host_pid=os.getpid())
    peer = mesh.registry.get("codex:T6")
    assert peer is not None and peer.transcript == str(rollout)
    rollout.unlink()
    assert mesh.registry.live() == []


def test_session_start_removes_seen_files_of_ended_sessions() -> None:
    mesh = _mesh()
    stale = paths.home() / "state" / "codex_gone.seen"
    paths.ensure_private_dir(stale.parent)
    stale.write_text("[]")
    payload = {"session_id": "T7", "cwd": os.getcwd()}
    hooks.handle(mesh, "session-start", "codex", payload, env={}, host_pid=os.getpid())
    assert not stale.exists()
    assert (paths.home() / "state" / "codex_T7.seen").exists()


def test_session_start_without_inbox_socket_still_gives_rules() -> None:
    out = hooks.handle(
        _mesh(), "session-start", "claude", {"session_id": "C9"}, env={}, host_pid=os.getpid()
    )
    assert out is not None
    text_out = out["hookSpecificOutput"]["additionalContext"]
    assert "not registered yet" in text_out and "peers_send" in text_out
