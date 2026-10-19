"""`peers-mcp`: the peermesh tools for Claude Code and the Codex plugin (stdio MCP server)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent

import peermesh
from peermesh import text
from peermesh.errors import PeerError
from peermesh.registry import Peer, Scope
from peermesh.service import Mesh, announce_short_ids


class _Server(MCPServer):
    """The MCP server, with a note in each tool result while a newer peermesh is installed.

    A Claude Code or Codex session keeps this process until it restarts the server, so the
    process can run older code than the hooks and the `peers` command of the same session.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            result = await super().call_tool(name, arguments, context)
        except ToolError as exc:
            notice = _stale_notice()
            if notice is None:
                raise
            # The library shows the agent the text of the error. The same type keeps a crash
            # (UnexpectedToolError) logged with its traceback, and older code can cause one.
            raise type(exc)(f"{exc} {notice}") from exc
        notice = _stale_notice()
        if notice is not None and isinstance(result, CallToolResult):
            # Claude Code gives the agent only the structured result when one exists, and
            # other clients give it the text.
            result.content.append(TextContent(type="text", text=notice))
            if isinstance(result.structured_content, dict):
                result.structured_content = {**result.structured_content, "notice": notice}
        return result


def _stale_notice() -> str | None:
    installed = _installed_version()
    if installed in (None, peermesh.__version__):
        return None
    return text.STALE_SERVER.format(running=peermesh.__version__, installed=installed)


def _installed_version() -> str | None:
    """The version in the package file that this process imported, as that file is now.

    An upgrade or a `git pull` of an editable install replaces the file; None when it cannot be
    read.
    """
    try:
        source = Path(peermesh.__file__).read_text()
    except OSError:
        return None
    match = re.search(r'^__version__ = "([^"]+)"', source, re.M)
    return match.group(1) if match else None


mcp = _Server("peermesh", instructions=text.MCP_INSTRUCTIONS)


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
        # A relative path would be read against the folder of the server.
        return env, cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
    return env, None


def _context(ctx: Context | None = None) -> tuple[Mesh, Peer]:
    mesh = Mesh.default()
    env, cwd = _session(ctx)
    # With no folder for a Codex thread, the folder of the server would be used to register
    # the thread, and it can be an unrelated repository.
    unknown = env is not os.environ and cwd is None
    try:
        me = mesh.whoami(env, cwd, unregistered=text.UNKNOWN_FOLDER if unknown else None)
    except PeerError as exc:
        raise ToolError(str(exc)) from exc
    # This process stays for the whole session, so it states what its version can do.
    announce_short_ids(me)
    return mesh, me


@mcp.tool(description=text.TOOL_LIST)
def peers_list(scope: str = "repo", ctx: Context | None = None) -> list[dict[str, Any]]:
    if scope not in ("repo", "worktree", "all"):
        raise ToolError("The scope is not valid. Use 'repo', 'worktree' or 'all'.")
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
        raise ToolError(str(exc)) from exc


@mcp.tool(description=text.TOOL_REPLY)
def peers_reply(
    msg_id: str, body: str, done: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    mesh, me = _context(ctx)
    try:
        return mesh.reply(me, msg_id, body, done=done).to_json()
    except PeerError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(description=text.TOOL_TRACK)
def peers_track(msg_id: str, ctx: Context | None = None) -> dict[str, Any]:
    mesh, _ = _context(ctx)
    try:
        return mesh.track(msg_id)
    except PeerError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(description=text.TOOL_STATUS)
def peers_status(target: str, ctx: Context | None = None) -> dict[str, Any]:
    mesh, _ = _context(ctx)
    return mesh.status(target)


def main() -> None:
    mcp.run()
