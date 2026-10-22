"""Session hooks for both runtimes: register, track busy and idle, notice peer changes."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from peermesh import paths, text
from peermesh.envelope import Ending, render
from peermesh.errors import NoInboxError, UnreadableRecord
from peermesh.identity import detect, find_host
from peermesh.inbox import Waiting
from peermesh.install import HOOK_EVENTS, hooks_installed
from peermesh.registry import (
    Owner,
    Peer,
    Runtime,
    current_branch,
    is_alive,
    owner,
    proc_start,
    repo_info,
    started_after,
)
from peermesh.service import SHORT_IDS, Mesh, accepts_short_ids
from peermesh.views import printable

EVENTS = ("session-start", "prompt", "post-tool", "stop", "session-end")
_HOOK_NAMES = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "post-tool": "PostToolUse",
}
# The hook event of the runtime for each peermesh hook name.
_EVENTS = {name: event for event, name in HOOK_EVENTS.items()} | {"post-tool": "PostToolUse"}
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
    """Prune the records of exited processes, then clear the state files of sessions that have
    no registry record."""
    mesh.prune()
    live = {p.stem for p in mesh.registry.dir.glob("*.json")}
    for state in (paths.home() / "state").glob("*.*"):
        if state.stem not in live:
            state.unlink(missing_ok=True)


def _note(line: str) -> None:
    """Record why a hook did nothing, where `peers doctor` and a person can find it."""
    try:
        paths.ensure_private_dir(paths.home())
        with (paths.home() / "hook-errors.log").open("a") as fh:
            fh.write(f"{time.ctime()} {line}\n")
    except OSError:
        pass


def _post_tool(
    mesh: Mesh, peer_id: str, env: Mapping[str, str], emit: Emit, me: Callable[[], Owner | None]
) -> dict[str, Any] | None:
    if not mesh.inbox.waiting(peer_id):
        return None
    try:
        seen = mesh.registry.read(peer_id)
    except UnreadableRecord:
        return None
    if seen is None:
        return None

    def deliver(peer: Peer) -> dict[str, Any] | None:
        items = mesh.take_waiting(peer)
        if not items:
            return None
        if peer.status != "busy":
            # A native wake starts a turn without a prompt hook; a tool call proves the turn.
            peer.status, peer.last_seen = "busy", mesh.clock()
        out = _context("post-tool", _render(peer, items)) | _notice(items, peer.id, env)
        # Emit before the record, so a crash leaves the messages claimed for a retry and
        # never marks a message injected that the runtime did not get.
        emit(out)
        mesh.record_injected(items, "post-tool")
        return out

    # A process that does not own the session leaves its messages to the owner.
    return mesh.claim(peer_id, seen, me(), deliver)


def _render(peer: Peer, items: list[Waiting], ending: Ending | None = None) -> str:
    return render(
        [i.message for i in items],
        peer.runtime,
        short_ids=accepts_short_ids(peer),
        ending=ending,
        tools=peer.has_tools,
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


def _stop(
    mesh: Mesh,
    peer_id: str,
    seen: Peer,
    caller: Owner | None,
    change: Callable[[Peer], Peer],
    payload: Mapping[str, Any],
    emit: Emit,
) -> dict[str, Any] | None:
    """Decide what the end of a turn does with waiting messages.

    Information waits for the next prompt or tool call. Actionable messages continue the turn
    once, with the instruction to restate the final report. A turn that a Stop hook already
    continued is not continued again; its actionable messages start a new turn instead.
    """

    def finish(peer: Peer) -> tuple[dict[str, Any] | None, list[Waiting], Peer]:
        change(peer)
        # The status and the claim are one step under the lock, so a sender sees either a busy
        # peer whose inbox a hook still reads, or an idle peer. A continued turn stays busy.
        items = mesh.take_waiting(peer)
        actionable = [i for i in items if i.actionable]
        block = bool(actionable) and not payload.get("stop_hook_active")
        peer.status, peer.last_seen = ("busy" if block else "idle"), mesh.clock()
        if block:
            out = _continue_turn(peer.runtime, _render(peer, items, "stop"))
            emit(out)
            mesh.record_injected(items, "stop")
            return out, [], peer
        mesh.inbox.release([i for i in items if not i.actionable])
        mesh.hand_to_native(actionable)
        return None, actionable, peer

    result = mesh.claim(peer_id, seen, caller, finish)
    if result is None:
        return None
    out, actionable, peer = result
    if actionable:
        mesh.wake(peer, actionable)
    return out


def _context(event: str, body: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": _HOOK_NAMES[event], "additionalContext": body}}


def _host(runtime: Runtime, session_id: str, host_pid: int | None) -> int | None:
    """The process that fired the hook, or None when no ancestor names the session."""
    if host_pid is not None:
        return host_pid
    found = find_host(runtime, session_id)
    return found[0] if found else None


def _caller(runtime: Runtime, session_id: str, host_pid: int | None) -> Owner | None:
    """The pid and the start time of the process that fired the hook, or None when no ancestor
    names the session. Both come from subprocesses, so hooks read them before the lock."""
    pid = _host(runtime, session_id, host_pid)
    return None if pid is None else (pid, proc_start(pid) or "")


def _needs_registration(peer: Peer | None, caller: Owner | None) -> bool:
    """True when the hook's process must register: no live record, a paused one, or a record
    of an older process, which can still be ending after the session ran again. adopt decides
    whether the registration takes the record."""
    if peer is None or peer.status == "paused" or not is_alive(peer):
        return True
    return (
        caller is not None and caller != owner(peer) and started_after(caller[1], peer.proc_start)
    )


def handle(
    mesh: Mesh,
    event: str,
    runtime: Runtime,
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    host_pid: int | None = None,
    inbox: bool = False,
    plugin: bool = False,
    emit: Emit = lambda out: None,
) -> dict[str, Any] | None:
    """Run one hook event and return its output; `emit` writes the output to the runtime.

    The hook calls `emit` itself for output that carries peer messages, under the ledger lock
    and before it records the messages as injected. `inbox` is True when the post-tool hook of
    this runtime is installed, so senders can leave messages for it. `plugin` is True for a
    hook of the Codex plugin, whose session has the peermesh MCP tools.
    """
    if plugin and hooks_installed(runtime, _EVENTS.get(event)):
        # A hook of `peers install` serves this event, so the plugin hook does nothing.
        return None
    # The payload names the session that fired the hook; an env value can be inherited from a
    # parent session, so it is only a fallback.
    session_id = str(payload.get("session_id") or "")
    if runtime == "codex" and not session_id:
        session_id = env.get("CODEX_THREAD_ID", "")
    if not session_id:
        return None
    peer_id = f"{runtime}:{session_id}"
    if event == "session-end":
        # Both run subprocesses, so they come before the lock.
        ending = _caller(runtime, session_id, host_pid)
        if ending is None:
            _note(f"{runtime} session-end: no ancestor process names {peer_id}; pruning ends it")
        try:
            seen = mesh.registry.read(peer_id)
        except UnreadableRecord as exc:
            _note(f"{runtime} session-end: {exc}")
            return None
        if seen is None:
            # No record: the inbox that is left, if any, has no owner. A resume can register
            # meanwhile, and a record that cannot be read is not absent, so the check and the
            # expiry hold the lock and need confirmed absence.
            with mesh.ledger.locked():
                if mesh.registry.absent(peer_id):
                    mesh.expire_inbox(peer_id, locked=True)
            _seen_file(peer_id).unlink(missing_ok=True)
            paths.state_file(peer_id, SHORT_IDS).unlink(missing_ok=True)
            return None
        if ending != owner(seen):
            # Another process owns the record, or this process cannot tell who it is: the end is
            # not this record's. Pruning retires a record whose process exited.
            return None
        # The Claude app ends the process of an idle session and keeps the session to run it
        # again: a pause keeps the record and its waiting messages. /clear ends the session id.
        mesh.retire(peer_id, seen, keep_if_kept=payload.get("reason") != "clear")
        if mesh.registry.read(peer_id) is None:
            _seen_file(peer_id).unlink(missing_ok=True)
            paths.state_file(peer_id, SHORT_IDS).unlink(missing_ok=True)
        return None
    caller: list[Owner | None] = []

    def me() -> Owner | None:
        if not caller:
            caller.append(_caller(runtime, session_id, host_pid))
            if caller[0] is None:
                _note(f"{runtime} {event}: no ancestor process names {peer_id}; nothing changed")
        return caller[0]

    if event == "post-tool":
        return _post_tool(mesh, peer_id, env, emit, me)
    if runtime == "codex" and "transcript_path" in payload and not payload["transcript_path"]:
        # Codex gives a null path for an ephemeral thread, such as a one-time review. It has
        # no rollout file, so `codex queue` cannot reach it and nothing shows when it closes.
        return None
    cwd = str(payload.get("cwd") or os.getcwd())
    seen = mesh.registry.get(peer_id)
    # Each SessionStart begins a registration, with a new generation, also in the same process:
    # a Codex thread can open again in the app-server that hosted it, with the same rollout.
    registers = event == "session-start" or _needs_registration(seen, me())
    if registers:
        dead = seen is not None and not is_alive(seen)
        try:
            seen = mesh.adopt(detect(runtime, session_id, cwd, env, host_pid), seen, seen_dead=dead)
        except NoInboxError:
            # Claude Code can run SessionStart before it writes the inbox socket path. The
            # next prompt registers the session; the rules apply from the start.
            if event == "session-start":
                out = _context(event, text.unregistered_text(runtime == "claude" or plugin))
                emit(out)
                return out
            raise
    # A record that needs no registration exists, and registration returns one.
    assert seen is not None
    # The new values come first, from git and the payload; claim applies them to the stored
    # record under the lock, and only while it is still this registration of this process.
    probe = Peer.from_json(seen.to_record())
    if not registers and event in ("session-start", "prompt"):
        _refresh(probe, cwd)
    transcript = str(payload.get("transcript_path") or "")
    if runtime == "codex" and transcript and Path(transcript).exists():
        probe.transcript = transcript
    if event in ("session-start", "prompt"):
        flags = {"inbox": inbox, "mcp_tools": plugin}
        others = [c for c in probe.capabilities if c not in flags]
        probe.capabilities = [*others, *(c for c, on in flags.items() if on)]
    now = mesh.clock()

    def change(record: Peer) -> Peer:
        for name in ("cwd", "git_root", "worktree", "branch", "repo_key", "transcript"):
            setattr(record, name, getattr(probe, name))
        record.capabilities = probe.capabilities
        if event != "stop":
            record.status, record.last_seen = ("busy" if event == "prompt" else "idle"), now
        return record

    if event == "stop":
        return _stop(mesh, peer_id, seen, me(), change, payload, emit)
    updated = mesh.claim(peer_id, seen, me(), change)
    if updated is None:
        # Another registration owns the session, or this process cannot tell who it is: it
        # changes nothing and leaves the waiting messages to the owner.
        return None
    peer = mesh.registry.get(peer_id) or updated
    peers = mesh.list_peers(peer, "repo")
    if event == "session-start":
        _changed(peer.id, peers)
        _remove_stale_state(mesh)
        out = _context(event, text.session_start_text(peer, peers))
        emit(out)
        return out
    parts = [text.change_text(peer, peers)] if _changed(peer.id, peers) else []

    def inject(current: Peer) -> dict[str, Any] | None:
        items = mesh.take_waiting(current)
        if items:
            parts.append(_render(current, items))
        if not parts:
            return None
        out = _context(event, "\n\n".join(parts))
        if items:
            out |= _notice(items, current.id, env)
        emit(out)
        mesh.record_injected(items, "prompt")
        return out

    return mesh.claim(peer_id, updated, me(), inject)
