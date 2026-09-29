from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from peermesh.registry import Peer, proc_start

SESSION_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "PEERMESH_CODEX",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PEERMESH_HOME", str(tmp_path / "pm"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def make_peer() -> Callable[..., Peer]:
    def factory(**overrides: Any) -> Peer:
        pid = os.getpid()
        fields: dict[str, Any] = {
            "id": "claude:s1",
            "name": "claude-a-aa",
            "runtime": "claude",
            "pid": pid,
            "proc_start": proc_start(pid) or "",
            "cwd": "/r/a",
            "git_root": "/r/a",
            "worktree": "/r/a",
            "branch": "main",
            "repo_key": "github.com/o/a",
            "status": "idle",
            "last_seen": 0.0,
            "endpoint": "/tmp/none.sock",
        }
        fields.update(overrides)
        return Peer(**fields)

    return factory
