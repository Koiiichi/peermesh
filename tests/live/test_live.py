"""Real model turns. Run only with PEERMESH_LIVE=1 and the user's approval."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from peermesh.registry import Peer, Registry, proc_start

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("PEERMESH_LIVE") != "1", reason="PEERMESH_LIVE is not 1"),
]


def _fake_claude_inbox() -> tuple[str, list[str], threading.Thread]:
    path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "in.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    got: list[str] = []

    def loop() -> None:
        server.settimeout(300)
        conn, _ = server.accept()
        with conn:
            got.append(conn.makefile().readline())
        server.close()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return path, got, thread


def test_codex_session_sends_to_claude_inbox(tmp_path: Path) -> None:
    """A real Codex exec session identifies itself and sends through the CLI."""
    if shutil.which("codex") is None or shutil.which("peers") is None:
        pytest.skip("codex or peers is not on PATH")
    sock, got, thread = _fake_claude_inbox()
    pid = os.getpid()
    Registry().put(
        Peer(
            id="claude:live-test",
            name="claude-live-test",
            runtime="claude",
            pid=pid,
            proc_start=proc_start(pid) or "",
            cwd=str(tmp_path),
            git_root=None,
            worktree=None,
            branch=None,
            repo_key=None,
            status="idle",
            last_seen=0.0,
            endpoint=sock,
        )
    )
    # The autouse fixture points CODEX_HOME and CLAUDE_CONFIG_DIR at temp dirs. The real Codex
    # session needs the real CODEX_HOME for auth and the installed allow rule; PEERMESH_HOME stays
    # temporary so the test registry is isolated.
    env = {**os.environ, "CODEX_HOME": str(Path.home() / ".codex")}
    env.pop("CLAUDE_CONFIG_DIR", None)
    prompt = (
        "Run this shell command exactly once: "
        "peers send claude-live-test --body 'live test: codex to claude' . Then stop."
    )
    subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", "-C", str(tmp_path), prompt],
        check=False,
        timeout=300,
        env=env,
    )
    thread.join(timeout=10)
    assert got, "no frame reached the fake Claude inbox"
    frame = json.loads(got[0])
    assert "(codex, id codex:" in frame["message"]["content"]
    assert "live test: codex to claude" in frame["message"]["content"]
