"""Real model turns. Run only with PEERMESH_LIVE=1 and the user's approval."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from peermesh import install
from peermesh.registry import Peer, Registry, proc_start
from peermesh.service import Mesh

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


def test_busy_codex_turn_gets_message_at_next_tool_call(tmp_path: Path) -> None:
    """A message sent during a real Codex turn is injected by the post-tool hook of this checkout.

    The Codex home is temporary, so only the hooks of this checkout run; auth.json is a symlink
    to the real one, not a copy.
    """
    real_auth = Path.home() / ".codex" / "auth.json"
    if shutil.which("codex") is None or not real_auth.exists():
        pytest.skip("codex or its auth.json is missing")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").symlink_to(real_auth)
    peers = str(Path(sys.executable).with_name("peers"))
    hooks = install.merge_hooks({}, peers, "codex")
    (codex_home / "hooks.json").write_text(json.dumps(hooks))
    pid = os.getpid()
    sender = Peer(
        id="claude:live-sender",
        name="claude-live-sender",
        runtime="claude",
        pid=pid,
        proc_start=proc_start(pid) or "",
        cwd=str(tmp_path),
        git_root=None,
        worktree=None,
        branch=None,
        repo_key=None,
        status="busy",
        last_seen=0.0,
        endpoint="/tmp/peermesh-live-sender-none.sock",
    )
    Registry().put(sender)
    results: list[str] = []

    def send_when_busy() -> None:
        for _ in range(1200):
            busy = [p for p in Registry().live() if p.runtime == "codex" and p.status == "busy"]
            if busy:
                [res] = Mesh.default().send(sender, busy[0].id, "live test: post-tool delivery")
                results.append(str(res.msg_id))
                return
            time.sleep(0.25)

    thread = threading.Thread(target=send_when_busy, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CODEX_"))}
    env |= {"CODEX_HOME": str(codex_home)}
    env.pop("CLAUDE_CONFIG_DIR", None)
    prompt = (
        "Run `sleep 8` as one shell command, then `echo done` as a second one. Then reply with "
        "every peermesh msg id that you saw, and nothing else."
    )
    out = subprocess.run(
        ["codex", "exec", "--dangerously-bypass-hook-trust", "--skip-git-repo-check"]
        + ["-C", str(tmp_path), prompt],
        check=False,
        timeout=300,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    thread.join(timeout=10)
    assert results, "the Codex session never registered as busy"
    tracked = Mesh.default().track(results[0])
    assert [h["outcome"] for h in tracked["history"]] == ["pending", "injected"]
    assert tracked["history"][1]["via"] == "post-tool"
    assert results[0] in out.stdout
