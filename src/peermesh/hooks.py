"""Session hooks for both runtimes: register, track busy and idle, notice peer changes."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from peermesh import paths, text
from peermesh.envelope import Ending, render
from peermesh.errors import NoInboxError
from peermesh.identity import detect
from peermesh.inbox import Waiting
from peermesh.registry import Peer, Runtime, current_branch, is_alive, repo_info
from peermesh.service import SHORT_IDS, Mesh, accepts_short_ids
from peermesh.views import printable

EVENTS = ("session-start", "prompt", "post-tool", "stop", "session-end")
_HOOK_NAMES = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "post-tool": "PostToolUse",
}
Emit = Callable[[dict[str, Any]], None]
# The peermesh mod for Claude Code sets this variable to the id of its session. The mod draws
# each message that a hook gives the agent, so the hook does not write a notice line too.
MOD_ENV = "PEERMESH_MOD"


def _seen_file(peer_id: str) -> Path:
    return paths.state_file(peer_id, "seen")


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


def _refresh(peer: Peer, cwd: str) -> None:
    """Bring the branch and location up to date; a session can change both after it starts."""
    if cwd != peer.cwd:
        info = repo_info(cwd)
        peer.cwd = cwd
        peer.git_root, peer.worktree = info.git_root, info.worktree
        peer.branch, peer.repo_key = info.branch, info.repo_key
    elif peer.worktree:
        peer.branch = current_branch(peer.worktree)


def _remove_stale_state(mesh: Mesh) -> None:
    """Clear the state files and inboxes of sessions that have no registry record."""
    live = {p.stem for p in mesh.registry.dir.glob("*.json")}
    for state in (paths.home() / "state").glob("*.*"):
        if state.stem not in live:
            state.unlink(missing_ok=True)
    for name in mesh.inbox.peer_ids():
        if name not in live:
            mesh.expire_inbox(name)


def _post_tool(
    mesh: Mesh, peer_id: str, env: Mapping[str, str], emit: Emit
) -> dict[str, Any] | None:
    peer = mesh.registry.get(peer_id)
    if peer is None:
        return None
    with mesh.ledger.locked():
        items = mesh.take_waiting(peer)
        if not items:
            return None
        if peer.status != "busy":
            # A native wake starts a turn without a prompt hook; a tool call proves the turn.
            peer.status, peer.last_seen = "busy", mesh.clock()
            mesh.registry.put(peer)
        out = _context("post-tool", _render(peer, items)) | _notice(items, peer.id, env)
        # Emit before the record, so a crash leaves the messages claimed for a retry and
        # never marks a message injected that the runtime did not get.
        emit(out)
        mesh.record_injected(items, "post-tool")
    return out


def _render(peer: Peer, items: list[Waiting], ending: Ending | None = None) -> str:
    return render(
        [i.message for i in items],
        peer.runtime,
        short_ids=accepts_short_ids(peer),
        ending=ending,
    )


# The notice line shows this many characters of the text of the first message.
NOTICE_CHARS = 120


def _notice(items: list[Waiting], peer_id: str, env: Mapping[str, str]) -> dict[str, str]:
    """The hook output that shows the user one line about the messages that the agent got.

    The line names the sender and the kind of the first message (or "done" on a reply that
    reports completed work), the start of its text on one line, and how many more messages
    came: "peermesh: ← claude-a · request  Please rebase. (+1 more)". The model context holds
    each message in full.
    """
    # A child process inherits the variable, so only the session that it names has the mod.
    if peer_id == f"claude:{env.get(MOD_ENV)}" or not items:
        return {}
    first = items[0].message
    word = ("done" if first.done else "") if first.kind == "reply" else first.kind.replace("_", " ")
    # The text can hold newlines and terminal control sequences. The notice is one plain line.
    text = printable(" ".join(first.body.split()))
    if len(text) > NOTICE_CHARS:
        text = text[: NOTICE_CHARS - 1] + "…"
    sender = printable(" ".join(first.from_name.split()))
    line = f"peermesh: ← {sender}{f' · {word}' if word else ''}  {text}"
    if len(items) > 1:
        line += f" (+{len(items) - 1} more)"
    return {"systemMessage": line}


def _continue_turn(runtime: Runtime, body: str) -> dict[str, Any]:
    """Stop-hook output that keeps the turn going with body as context for the model.

    Claude Code shows the reason of a "block" decision to the user as a hook error, and takes
    additionalContext as feedback that continues the turn. Codex has no additionalContext for
    Stop; a "block" decision is its way to continue a turn. Both runtimes show the body to the
    user, so this output carries no separate notice.
    """
    if runtime == "claude":
        return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": body}}
    return {"decision": "block", "reason": body}


def _stop(mesh: Mesh, peer: Peer, payload: Mapping[str, Any], emit: Emit) -> dict[str, Any] | None:
    """Decide what the end of a turn does with waiting messages.

    Information waits for the next prompt or tool call. Actionable messages continue the turn
    once, with the instruction to restate the final report. A turn that a Stop hook already
    continued is not continued again; its actionable messages start a new turn instead.
    """
    with mesh.ledger.locked():
        # The status and the claim are one step under the lock, so a sender sees either a busy
        # peer whose inbox a hook still reads, or an idle peer. A continued turn stays busy.
        items = mesh.take_waiting(peer)
        actionable = [i for i in items if i.actionable]
        block = bool(actionable) and not payload.get("stop_hook_active")
        peer.status, peer.last_seen = ("busy" if block else "idle"), mesh.clock()
        mesh.registry.put(peer)
        if block:
            out = _continue_turn(peer.runtime, _render(peer, items, "stop"))
            emit(out)
            mesh.record_injected(items, "stop")
            return out
        mesh.inbox.release([i for i in items if not i.actionable])
        mesh.hand_to_native(actionable)
    if actionable:
        mesh.wake(peer, actionable)
    return None


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
    inbox: bool = False,
    emit: Emit = lambda out: None,
) -> dict[str, Any] | None:
    """Run one hook event and return its output; `emit` writes the output to the runtime.

    The hook calls `emit` itself for output that carries peer messages, under the ledger lock
    and before it records the messages as injected. `inbox` is True when the post-tool hook of
    this runtime is installed, so senders can leave messages for it.
    """
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
        paths.state_file(peer_id, SHORT_IDS).unlink(missing_ok=True)
        mesh.expire_inbox(peer_id)
        return None
    if event == "post-tool":
        return _post_tool(mesh, peer_id, env, emit)
    if runtime == "codex" and "transcript_path" in payload and not payload["transcript_path"]:
        # Codex gives a null path for an ephemeral thread, such as a one-time review. It has
        # no rollout file, so `codex queue` cannot reach it and nothing shows when it closes.
        return None
    cwd = str(payload.get("cwd") or os.getcwd())
    peer = mesh.registry.get(peer_id)
    if peer is None or not is_alive(peer):
        try:
            peer = mesh.register(detect(runtime, session_id, cwd, env, host_pid))
        except NoInboxError:
            # Claude Code can run SessionStart before it writes the inbox socket path. The
            # next prompt registers the session; the rules apply from the start.
            if event == "session-start":
                out = _context(event, text.unregistered_text(runtime))
                emit(out)
                return out
            raise
    elif event in ("session-start", "prompt"):
        _refresh(peer, cwd)
    transcript = str(payload.get("transcript_path") or "")
    if runtime == "codex" and transcript and Path(transcript).exists():
        peer.transcript = transcript
    if event in ("session-start", "prompt"):
        others = [c for c in peer.capabilities if c != "inbox"]
        peer.capabilities = [*others, "inbox"] if inbox else others
    if event == "stop":
        return _stop(mesh, peer, payload, emit)
    peer.status = "busy" if event == "prompt" else "idle"
    peer.last_seen = mesh.clock()
    mesh.registry.put(peer)
    peers = mesh.list_peers(peer, "repo")
    if event == "session-start":
        _changed(peer.id, peers)
        _remove_stale_state(mesh)
        out = _context(event, text.session_start_text(peer, peers))
        emit(out)
        return out
    parts = [text.change_text(peer, peers)] if _changed(peer.id, peers) else []
    with mesh.ledger.locked():
        items = mesh.take_waiting(peer)
        if items:
            parts.append(_render(peer, items))
        if not parts:
            return None
        out = _context(event, "\n\n".join(parts))
        if items:
            out |= _notice(items, peer.id, env)
        emit(out)
        mesh.record_injected(items, "prompt")
    return out
