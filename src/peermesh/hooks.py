"""Session hooks for both runtimes: register, track busy and idle, notice peer changes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from peermesh import paths, text
from peermesh.identity import detect
from peermesh.registry import Peer, Runtime, is_alive
from peermesh.service import Mesh

EVENTS = ("session-start", "prompt", "stop", "session-end")
_HOOK_NAMES = {"session-start": "SessionStart", "prompt": "UserPromptSubmit"}


def _seen_file(peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", peer_id)
    return paths.home() / "state" / f"{safe}.seen"


def _changed(peer_id: str, peers: list[Peer]) -> bool:
    """Record the peer id set; True when it differs from the recorded set."""
    current = sorted(p.id for p in peers)
    try:
        previous = json.loads(_seen_file(peer_id).read_text())
    except (OSError, ValueError):
        previous = None
    if previous == current:
        return False
    paths.ensure_private_dir(_seen_file(peer_id).parent)
    _seen_file(peer_id).write_text(json.dumps(current))
    return previous is not None or bool(current)


def _context(event: str, body: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": _HOOK_NAMES[event], "additionalContext": body}}


def handle(
    mesh: Mesh,
    event: str,
    runtime: Runtime,
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    host_pid: int | None = None,
) -> dict[str, Any] | None:
    # The payload names the session that fired the hook; an env value can be inherited from a
    # parent session, so it is only a fallback.
    session_id = str(payload.get("session_id") or "")
    if runtime == "codex" and not session_id:
        session_id = env.get("CODEX_THREAD_ID", "")
    if not session_id:
        return None
    peer_id = f"{runtime}:{session_id}"
    if event == "session-end":
        mesh.registry.remove(peer_id)
        _seen_file(peer_id).unlink(missing_ok=True)
        return None
    peer = mesh.registry.get(peer_id)
    if peer is None or not is_alive(peer):
        cwd = str(payload.get("cwd") or os.getcwd())
        peer = mesh.register(detect(runtime, session_id, cwd, env, host_pid))
    peer.status = "busy" if event == "prompt" else "idle"
    peer.last_seen = mesh.clock()
    mesh.registry.put(peer)
    if event == "stop":
        return None
    peers = mesh.list_peers(peer, "repo")
    if event == "session-start":
        _changed(peer.id, peers)
        return _context(event, text.session_start_text(peer, peers))
    if _changed(peer.id, peers):
        return _context(event, text.change_text(peer, peers))
    return None
