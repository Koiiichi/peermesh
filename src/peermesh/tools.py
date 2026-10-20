"""The work of each peermesh MCP tool, which the MCP server runs in a new process for each call.

A Claude Code or Codex session keeps its MCP server across an upgrade, and neither client starts
an interactive session's server again after it exits. The server runs this module for each call,
in isolated mode and from the folder that it loaded peermesh from, so each call runs the
installed code.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any, cast

from peermesh import text
from peermesh.errors import PeerError
from peermesh.registry import Peer, Scope
from peermesh.service import Mesh, announce_short_ids


def run(
    tool: str,
    arguments: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    codex_thread: bool = False,
    cwd: str | None = None,
) -> Any:
    """The result of `tool` as the session that `env` names, as JSON data.

    `codex_thread` is True when the request metadata named the Codex thread of the call, and
    `cwd` is its folder from the metadata, or None. Raises PeerError with a text for the agent
    when peermesh refuses the call.
    """
    if tool == "peers_list":
        scope = arguments.get("scope", "repo")
        if scope not in ("repo", "worktree", "all"):
            raise PeerError("The scope is not valid. Use 'repo', 'worktree' or 'all'.")
        mesh, me = _context(env, codex_thread, cwd)
        return [p.to_json() for p in mesh.list_peers(me, cast(Scope, scope))]
    if tool == "peers_send":
        mesh, me = _context(env, codex_thread, cwd)
        sent = mesh.send(
            me,
            arguments["to"],
            arguments["body"],
            arguments.get("kind", "info"),
            arguments.get("urgency", "normal"),
        )
        return [r.to_json() for r in sent]
    if tool == "peers_reply":
        mesh, me = _context(env, codex_thread, cwd)
        done = bool(arguments.get("done", False))
        return mesh.reply(me, arguments["msg_id"], arguments["body"], done=done).to_json()
    if tool == "peers_track":
        mesh, _ = _context(env, codex_thread, cwd)
        return mesh.track(arguments["msg_id"])
    if tool == "peers_status":
        mesh, _ = _context(env, codex_thread, cwd)
        return mesh.status(arguments["target"])
    raise ValueError(f"peermesh has no tool {tool!r}.")


def _context(env: Mapping[str, str], codex_thread: bool, cwd: str | None) -> tuple[Mesh, Peer]:
    mesh = Mesh.default()
    # With no folder for a Codex thread, the folder of the server would be used to register
    # the thread, and it can be an unrelated repository.
    unknown = codex_thread and cwd is None
    me = mesh.whoami(env, cwd, unregistered=text.UNKNOWN_FOLDER if unknown else None)
    # The call runs the installed code, so it states what the installed version can do.
    announce_short_ids(me)
    return mesh, me


def main() -> None:
    """Read one call as JSON on stdin and write {"result": ...} or {"error": text} on stdout."""
    call = json.loads(sys.stdin.read())
    try:
        result = run(
            call["tool"],
            call["arguments"],
            os.environ,
            codex_thread=bool(call.get("codex_thread")),
            cwd=call.get("cwd"),
        )
    except PeerError as exc:
        reply: dict[str, Any] = {"error": str(exc)}
    else:
        reply = {"result": result}
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
