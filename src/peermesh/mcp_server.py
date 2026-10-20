"""`peers-mcp`: the peermesh tools for Claude Code and the Codex plugin (stdio MCP server)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent

import peermesh
from peermesh import text, tools
from peermesh.errors import PeerError


class _Server(MCPServer):
    """The MCP server. Each call runs the installed peermesh in a new process.

    A Claude Code or Codex session keeps this process across an upgrade, and neither client starts
    an interactive session's server again after it exits. So the work of each call runs in a new
    process of the installed code, and only the list of tools is the one that this process loaded.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            result = await super().call_tool(name, arguments, context)
        except ToolError as exc:
            notes = _notes.pop(id(context), [])
            if not notes:
                raise
            # The library shows the agent the text of the error. The same type keeps a crash
            # (UnexpectedToolError) logged with its traceback.
            raise type(exc)(" ".join([str(exc), *notes])) from exc
        notes = _notes.pop(id(context), [])
        if notes and isinstance(result, CallToolResult):
            notice = " ".join(notes)
            # Claude Code gives the agent only the structured result when one exists, and
            # other clients give it the text.
            result.content.append(TextContent(type="text", text=notice))
            if isinstance(result.structured_content, dict):
                result.structured_content = {**result.structured_content, "notice": notice}
        return result


# Notes for the agent, by the context object of the call that the server is answering.
_notes: dict[int, list[str]] = {}
# The newer versions that a note already named, so that each one is named one time.
_noticed: set[str] = set()

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


class _Unavailable(Exception):
    """The installed peermesh cannot run. The text names the cause for the agent."""


class _Runner(Protocol):
    def __call__(
        self,
        tool: str,
        arguments: dict[str, Any],
        env: Mapping[str, str],
        *,
        codex_thread: bool,
        cwd: str | None,
    ) -> dict[str, Any]: ...


def _command() -> list[str]:
    # -I ignores PYTHONPATH and the folder of the session, which can hold a module "peermesh",
    # but it keeps the site-packages of the environment, which name the installed peermesh.
    return [sys.executable, "-I", "-m", "peermesh.tools"]


def _in_new_process(
    tool: str,
    arguments: dict[str, Any],
    env: Mapping[str, str],
    *,
    codex_thread: bool,
    cwd: str | None,
) -> dict[str, Any]:
    """The reply of the tool from a new process of the installed code, as the session in `env`.

    The new process is a child of this one, so the session host is still its ancestor. It has
    no time limit: a send to several Codex sessions can wait 30 seconds for each of them.
    """
    call = json.dumps(
        {"tool": tool, "arguments": arguments, "codex_thread": codex_thread, "cwd": cwd}
    )
    try:
        done = subprocess.run(
            _command(), input=call, capture_output=True, text=True, env=dict(env), check=False
        )
    except OSError as exc:
        raise _Unavailable(f"its Python interpreter cannot start: {exc.strerror}") from exc
    sys.stderr.write(done.stderr)
    if done.returncode != 0 and re.search(r"No module named 'peermesh(\.tools)?'", done.stderr):
        raise _Unavailable("the environment does not hold peermesh")
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = None
    if done.returncode != 0 or not isinstance(reply, dict):
        raise RuntimeError(f"The peermesh tool process ended with code {done.returncode}.")
    return reply


def _in_this_process(
    tool: str,
    arguments: dict[str, Any],
    env: Mapping[str, str],
    *,
    codex_thread: bool,
    cwd: str | None,
) -> dict[str, Any]:
    """The reply of the tool from the code that this process loaded."""
    try:
        result = tools.run(tool, arguments, env, codex_thread=codex_thread, cwd=cwd)
    except PeerError as exc:
        return {"error": str(exc), "version": peermesh.__version__}
    return {"result": result, "version": peermesh.__version__}


_runner: _Runner = _in_new_process


def _call(tool: str, arguments: dict[str, Any], ctx: Context | None) -> Any:
    env, cwd = _session(ctx)
    codex_thread = env is not os.environ
    try:
        reply = _runner(tool, arguments, env, codex_thread=codex_thread, cwd=cwd)
    except _Unavailable as exc:
        # An older server that still runs is better than none.
        _note(ctx, text.TOOLS_UNAVAILABLE.format(reason=exc))
        reply = _in_this_process(tool, arguments, env, codex_thread=codex_thread, cwd=cwd)
    version = reply.get("version")
    if isinstance(version, str) and version != peermesh.__version__ and version not in _noticed:
        _noticed.add(version)
        _note(ctx, text.STALE_SERVER.format(running=peermesh.__version__, installed=version))
    if "error" in reply:
        raise ToolError(str(reply["error"]))
    return reply.get("result")


def _note(ctx: Context | None, note: str) -> None:
    if ctx is not None:
        _notes.setdefault(id(ctx), []).append(note)


@mcp.tool(description=text.TOOL_LIST)
def peers_list(scope: str = "repo", ctx: Context | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = _call("peers_list", {"scope": scope}, ctx)
    return result


@mcp.tool(description=text.TOOL_SEND)
def peers_send(
    to: list[str],
    body: str,
    kind: str = "info",
    urgency: str = "normal",
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    arguments = {"to": to, "body": body, "kind": kind, "urgency": urgency}
    result: list[dict[str, Any]] = _call("peers_send", arguments, ctx)
    return result


@mcp.tool(description=text.TOOL_REPLY)
def peers_reply(
    msg_id: str, body: str, done: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = _call(
        "peers_reply", {"msg_id": msg_id, "body": body, "done": done}, ctx
    )
    return result


@mcp.tool(description=text.TOOL_TRACK)
def peers_track(msg_id: str, ctx: Context | None = None) -> dict[str, Any]:
    result: dict[str, Any] = _call("peers_track", {"msg_id": msg_id}, ctx)
    return result


@mcp.tool(description=text.TOOL_STATUS)
def peers_status(target: str, ctx: Context | None = None) -> dict[str, Any]:
    result: dict[str, Any] = _call("peers_status", {"target": target}, ctx)
    return result


def main() -> None:
    mcp.run()
