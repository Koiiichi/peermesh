"""`peers-mcp`: the peermesh tools for Claude Code and the Codex plugin (stdio MCP server)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, cast

from mcp.server.mcpserver import Context, MCPServer

from peermesh import text
from peermesh.errors import PeerError
from peermesh.registry import Peer, Scope
from peermesh.service import Mesh, announce_short_ids

mcp = MCPServer("peermesh", instructions=text.MCP_INSTRUCTIONS)


def _session(ctx: Context | None) -> tuple[Mapping[str, str], str | None]:
    """The environment and the folder that name the session of one tool call.

    Codex starts the server with no thread id in its environment, and one Codex process can
    host many threads. Codex sends the thread of each call in the request metadata.
    """
    meta = ctx.request_context.meta if ctx is not None else None
    turn = meta.get("x-codex-turn-metadata") if isinstance(meta, Mapping) else None
    if not isinstance(turn, Mapping):
        return os.environ, None
    thread = turn.get("thread_id")
    if not isinstance(thread, str) or not thread:
        return os.environ, None
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID")
    }
    env["CODEX_THREAD_ID"] = thread
    # The folder matters only when no hook registered the thread before this call. The
    # metadata does not say which of several workspaces is the folder of the session.
    workspaces = turn.get("workspaces")
    if isinstance(workspaces, Mapping) and len(workspaces) == 1:
        [cwd] = workspaces
        return env, cwd if isinstance(cwd, str) else None
    return env, None


def _context(ctx: Context | None = None) -> tuple[Mesh, Peer]:
    mesh = Mesh.default()
    env, cwd = _session(ctx)
    try:
        me = mesh.whoami(env, cwd)
    except PeerError as exc:
        raise ValueError(str(exc)) from exc
    # This process stays for the whole session, so it states what its version can do.
    announce_short_ids(me)
    return mesh, me


@mcp.tool(description=text.TOOL_LIST)
def peers_list(scope: str = "repo", ctx: Context | None = None) -> list[dict[str, Any]]:
    if scope not in ("repo", "worktree", "all"):
        raise ValueError("The scope is not valid. Use 'repo', 'worktree' or 'all'.")
    mesh, me = _context(ctx)
    return [p.to_json() for p in mesh.list_peers(me, cast(Scope, scope))]


@mcp.tool(description=text.TOOL_SEND)
def peers_send(
    to: list[str],
    body: str,
    kind: str = "info",
    urgency: str = "normal",
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    mesh, me = _context(ctx)
    try:
        return [r.to_json() for r in mesh.send(me, to, body, kind, urgency)]
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_REPLY)
def peers_reply(
    msg_id: str, body: str, done: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    mesh, me = _context(ctx)
    try:
        return mesh.reply(me, msg_id, body, done=done).to_json()
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_TRACK)
def peers_track(msg_id: str, ctx: Context | None = None) -> dict[str, Any]:
    mesh, _ = _context(ctx)
    try:
        return mesh.track(msg_id)
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_STATUS)
def peers_status(target: str, ctx: Context | None = None) -> dict[str, Any]:
    mesh, _ = _context(ctx)
    return mesh.status(target)


def main() -> None:
    mcp.run()
