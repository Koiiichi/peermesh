"""Find which session this process belongs to, and build its registry record."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from peermesh import paths
from peermesh.errors import PeerError
from peermesh.registry import Peer, Runtime, proc_start, repo_info


def _ps(field: str, pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def command_of(pid: int) -> str:
    return _ps("command", pid)


def ancestors(start: int) -> list[int]:
    chain: list[int] = []
    pid = start
    while pid > 1 and pid not in chain and len(chain) < 64:
        chain.append(pid)
        parent = _ps("ppid", pid)
        pid = int(parent) if parent.isdigit() else 0
    return chain


def _is_codex_command(command: str) -> bool:
    return any(Path(tok).name.startswith("codex") for tok in command.split()[:2])


def _is_claude_command(command: str) -> bool:
    return any(Path(tok).name.startswith("claude") for tok in command.split()[:2])


def find_host(
    runtime: Runtime, session_id: str, start: int | None = None
) -> tuple[int, int] | None:
    """Return (pid, depth) of the nearest ancestor that hosts the session."""
    chain = ancestors(start or os.getpid())
    if runtime == "claude":
        for depth, pid in enumerate(chain):
            try:
                data = json.loads((paths.claude_dir() / "sessions" / f"{pid}.json").read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("sessionId") == session_id:
                return pid, depth
        for depth, pid in enumerate(chain):
            if _is_claude_command(command_of(pid)):
                return pid, depth
        return None
    for depth, pid in enumerate(chain):
        if _is_codex_command(command_of(pid)):
            return pid, depth
    return None


def session_from_env(env: Mapping[str, str], start: int | None = None) -> tuple[Runtime, str]:
    candidates: list[tuple[Runtime, str]] = []
    if env.get("CLAUDE_CODE_SESSION_ID"):
        candidates.append(("claude", env["CLAUDE_CODE_SESSION_ID"]))
    codex_id = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if codex_id:
        candidates.append(("codex", codex_id))
    if not candidates:
        raise PeerError(
            "This process is not inside a Claude Code or Codex session. "
            "Run this command from the shell of an agent session."
        )
    if len(candidates) == 1:
        return candidates[0]

    def depth(candidate: tuple[Runtime, str]) -> int:
        found = find_host(candidate[0], candidate[1], start)
        return found[1] if found else 10_000

    return min(candidates, key=depth)


def detect(
    runtime: Runtime,
    session_id: str,
    cwd: str,
    env: Mapping[str, str],
    host_pid: int | None = None,
) -> Peer:
    if host_pid is None:
        found = find_host(runtime, session_id)
        if found is None:
            raise PeerError(f"peermesh cannot find the {runtime} process of this session.")
        host_pid = found[0]
    started = proc_start(host_pid)
    if started is None:
        raise PeerError(f"The host process {host_pid} does not run.")
    host = "local"
    capabilities: list[str] = []
    if runtime == "claude":
        endpoint = env.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
        if not endpoint:
            try:
                native = json.loads(
                    (paths.claude_dir() / "sessions" / f"{host_pid}.json").read_text()
                )
                endpoint = str(native.get("messagingSocketPath", ""))
            except (OSError, ValueError, AttributeError):
                endpoint = ""
        if not endpoint:
            raise PeerError(
                "This Claude Code session has no inbox socket. "
                "Use Claude Code 2.1.224 or later on macOS or Linux."
            )
        capabilities.append("urgent_interrupt")
    else:
        endpoint = session_id
        if "app-server" in command_of(host_pid):
            host = "daemon"
            capabilities.append("urgent_interrupt")
    info = repo_info(cwd)
    return Peer(
        id=f"{runtime}:{session_id}",
        name="",
        runtime=runtime,
        pid=host_pid,
        proc_start=started,
        cwd=cwd,
        git_root=info.git_root,
        worktree=info.worktree,
        branch=info.branch,
        repo_key=info.repo_key,
        status="idle",
        last_seen=time.time(),
        endpoint=endpoint,
        host=host,
        capabilities=capabilities,
    )
