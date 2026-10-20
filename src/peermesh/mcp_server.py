"""`peers-mcp`: the peermesh tools for Claude Code and the Codex plugin (stdio MCP server)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent

import peermesh
from peermesh import text
from peermesh.errors import PeerError


class _Server(MCPServer):
    """The MCP server, with a note in one tool result when a newer peermesh is installed.

    A Claude Code or Codex session keeps this process across an upgrade. Each call runs the
    installed code in a new process, but the list of tools is the one that this process loaded.
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


_noticed: set[str] = set()


def _stale_notice() -> str | None:
    """The note about a newer installed peermesh, the first time that this process sees it."""
    installed = _installed_version()
    if installed in (None, peermesh.__version__) or installed in _noticed:
        return None
    _noticed.add(installed)
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


class _Runner(Protocol):
    def __call__(
        self,
        tool: str,
        arguments: dict[str, Any],
        env: Mapping[str, str],
        *,
        codex_thread: bool,
        cwd: str | None,
    ) -> Any: ...


def _in_new_process(
    tool: str,
    arguments: dict[str, Any],
    env: Mapping[str, str],
    *,
    codex_thread: bool,
    cwd: str | None,
) -> Any:
    """Run the tool in a new process of the installed code, as the session that `env` names.

    The new process is a child of this one, so the session host is still its ancestor. It has
    no time limit: a send to several Codex sessions can wait 30 seconds for each of them.
    """
    call = json.dumps(
        {"tool": tool, "arguments": arguments, "codex_thread": codex_thread, "cwd": cwd}
    )
    # -I ignores PYTHONPATH and the folder of the session, which can hold a module "peermesh".
    # The package comes only from the folder that this process loaded it from, and an upgrade
    # replaces the files there.
    start = (
        f"import sys; sys.path.insert(0, {_package_root()!r}); import peermesh.tools as t; t.main()"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", start],
        input=call,
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
    )
    sys.stderr.write(done.stderr)
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = None
    if done.returncode != 0 or not isinstance(reply, dict):
        raise RuntimeError(f"The peermesh tool process ended with code {done.returncode}.")
    if "error" in reply:
        raise PeerError(str(reply["error"]))
    return reply.get("result")


def _package_root() -> str:
    """The folder that holds the peermesh package of this process."""
    return str(Path(peermesh.__file__).resolve().parent.parent)


_runner: _Runner = _in_new_process


def _call(tool: str, arguments: dict[str, Any], ctx: Context | None) -> Any:
    env, cwd = _session(ctx)
    try:
        return _runner(tool, arguments, env, codex_thread=env is not os.environ, cwd=cwd)
    except PeerError as exc:
        raise ToolError(str(exc)) from exc


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
