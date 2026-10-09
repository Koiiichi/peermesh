"""`peers-mcp`: the peermesh tools for Claude Code (stdio MCP server)."""

from __future__ import annotations

from typing import Any, cast

from mcp.server.mcpserver import MCPServer

from peermesh import text
from peermesh.errors import PeerError
from peermesh.registry import Peer, Scope
from peermesh.service import Mesh

mcp = MCPServer("peermesh", instructions=text.MCP_INSTRUCTIONS)


def _context() -> tuple[Mesh, Peer]:
    mesh = Mesh.default()
    try:
        return mesh, mesh.whoami()
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_LIST)
def peers_list(scope: str = "repo") -> list[dict[str, Any]]:
    if scope not in ("repo", "worktree", "all"):
        raise ValueError("The scope is not valid. Use 'repo', 'worktree' or 'all'.")
    mesh, me = _context()
    return [p.to_json() for p in mesh.list_peers(me, cast(Scope, scope))]


@mcp.tool(description=text.TOOL_SEND)
def peers_send(
    to: list[str], body: str, kind: str = "info", urgency: str = "normal"
) -> list[dict[str, Any]]:
    mesh, me = _context()
    try:
        return [r.to_json() for r in mesh.send(me, to, body, kind, urgency)]
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_REPLY)
def peers_reply(msg_id: str, body: str, done: bool = False) -> dict[str, Any]:
    mesh, me = _context()
    try:
        return mesh.reply(me, msg_id, body, done=done).to_json()
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_TRACK)
def peers_track(msg_id: str) -> dict[str, Any]:
    mesh, _ = _context()
    try:
        return mesh.track(msg_id)
    except PeerError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(description=text.TOOL_STATUS)
def peers_status(target: str) -> dict[str, Any]:
    mesh, _ = _context()
    return mesh.status(target)


def main() -> None:
    mcp.run()
