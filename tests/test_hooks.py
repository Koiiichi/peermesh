from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

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
        [sys.executable, "-m", "peermesh.cli", "hook", "session-start", "--runtime", "claude"],
        input='{"session_id": "X"}',
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert (paths.home() / "hook-errors.log").exists()
