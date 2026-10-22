from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import peermesh
from peermesh import mcp_server, paths, text, tools
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry, proc_start
from peermesh.service import Mesh, accepts_short_ids
from peermesh.transports import Outcome


class Fake:
    def deliver(self, target: Peer, rendered: str, msg: object) -> Outcome:
        return Outcome("queued", "next turn")


@pytest.fixture(autouse=True)
def ran(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    # The server runs each call in a new process; the tests run it in this one, so that a
    # patch of peermesh code applies to the call. A test sets the version that the call ran.
    version = {"version": peermesh.__version__}

    def run_here(
        tool: str, arguments: dict[str, Any], env: Any, *, codex_thread: bool, cwd: str | None
    ) -> dict[str, Any]:
        reply = mcp_server._in_this_process(
            tool, arguments, env, codex_thread=codex_thread, cwd=cwd
        )
        return reply | version

    monkeypatch.setattr(mcp_server, "_runner", run_here)
    monkeypatch.setattr(mcp_server, "_noticed", set())
    return version


@pytest.fixture
def ctx(make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch) -> Mesh:
    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    me = make_peer(id="claude:ME", name="claude-me")
    mesh.registry.put(me)
    mesh.registry.put(make_peer(id="codex:X", name="codex-x", runtime="codex", endpoint="X"))
    monkeypatch.setattr(tools, "_context", lambda env, codex_thread, cwd: (mesh, me))
    return mesh


def test_tools_registered_with_descriptions() -> None:
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools) == {"peers_list", "peers_send", "peers_reply", "peers_status", "peers_track"}
    assert tools["peers_send"].description == text.TOOL_SEND


def test_list_send_reply_status(ctx: Mesh) -> None:
    assert [p["name"] for p in mcp_server.peers_list()] == ["codex-x"]
    [res] = mcp_server.peers_send(["codex-x"], "Schema v2 merged at 1a2b", kind="info")
    assert res["status"] == "queued"
    assert mcp_server.peers_status("codex-x")["queued_last_hour"] == 1


def test_bad_scope_is_a_clear_error(ctx: Mesh) -> None:
    with pytest.raises(ToolError, match="scope"):
        mcp_server.peers_list("galaxy")


def test_the_server_states_that_it_resolves_short_ids(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    me = make_peer(id="claude:ME", name="claude-me")
    mesh.registry.put(me)
    monkeypatch.setattr(Mesh, "default", classmethod(lambda cls: mesh))
    monkeypatch.setattr(Mesh, "whoami", lambda self, *a, **k: mesh.registry.get("claude:ME"))
    before = mesh.registry._file("claude:ME").read_text()
    assert not accepts_short_ids(me)
    tools._context(os.environ, False, None)
    assert accepts_short_ids(me)
    # The registry record is not written: a stale copy could replace a status from a hook.
    assert mesh.registry._file("claude:ME").read_text() == before


class _Request:
    def __init__(self, meta: dict[str, object] | None) -> None:
        self.meta = meta


class _Ctx:
    def __init__(self, meta: dict[str, object] | None) -> None:
        self.request_context = _Request(meta)


def test_a_codex_call_names_its_thread_in_the_request_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "OUTER")
    turn = {"thread_id": "T1", "workspaces": {"/repo": {"has_changes": False}}}
    env, cwd = mcp_server._session(cast(Any, _Ctx({"x-codex-turn-metadata": turn})))
    assert env["CODEX_THREAD_ID"] == "T1" and "CLAUDE_CODE_SESSION_ID" not in env
    assert cwd == "/repo"


def test_several_workspaces_or_a_relative_one_give_no_folder() -> None:
    for workspaces in ({"/a": {}, "/b": {}}, {"": {}}, {".": {}}, {"repo": {}}):
        turn = {"thread_id": "T1", "workspaces": workspaces}
        env, cwd = mcp_server._session(cast(Any, _Ctx({"x-codex-turn-metadata": turn})))
        assert env["CODEX_THREAD_ID"] == "T1" and cwd is None, workspaces


def test_a_call_without_codex_metadata_uses_the_environment() -> None:
    assert mcp_server._session(cast(Any, _Ctx({"progressToken": 1}))) == (os.environ, None)
    assert mcp_server._session(None) == (os.environ, None)


def test_no_tool_takes_an_identity_argument() -> None:
    # The model writes the arguments. The session comes from the runtime only.
    tools = {
        t.name: set(t.input_schema["properties"]) for t in asyncio.run(mcp_server.mcp.list_tools())
    }
    assert tools == {
        "peers_list": {"scope"},
        "peers_send": {"to", "body", "kind", "urgency"},
        "peers_reply": {"msg_id", "body", "done"},
        "peers_status": {"target"},
        "peers_track": {"msg_id"},
    }


def test_metadata_that_names_a_thread_of_another_process_is_refused(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A process that starts its own server can write any metadata. It can act only as a
    # thread whose host process is one of its ancestors.
    import subprocess

    from peermesh.registry import proc_start

    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    monkeypatch.setattr(Mesh, "default", classmethod(lambda cls: mesh))
    other = subprocess.Popen(["sleep", "30"])
    try:
        mesh.registry.put(
            make_peer(
                id="codex:T2",
                name="codex-t2",
                runtime="codex",
                endpoint="T2",
                pid=other.pid,
                proc_start=proc_start(other.pid) or "",
            )
        )
        ctx = _Ctx({"x-codex-turn-metadata": {"thread_id": "T2"}})
        with pytest.raises(ToolError, match="not an ancestor"):
            mcp_server.peers_send(["anyone"], "spoof", ctx=cast(Any, ctx))
    finally:
        other.kill()
        other.wait()


def test_a_codex_thread_with_an_unknown_folder_is_not_registered_by_a_call(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The folder of the server can be a repository that is not the folder of the thread.
    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    monkeypatch.setattr(Mesh, "default", classmethod(lambda cls: mesh))
    turn = {"thread_id": "T9", "workspaces": {"/a": {}, "/b": {}}}
    with pytest.raises(ToolError, match="does not give its folder"):
        mcp_server.peers_list(ctx=cast(Any, _Ctx({"x-codex-turn-metadata": turn})))
    assert mesh.registry.get("codex:T9") is None
    # A thread that a hook registered does not need the folder.
    mesh.registry.put(make_peer(id="codex:T9", name="codex-t9", runtime="codex", endpoint="T9"))
    assert mcp_server.peers_list(ctx=cast(Any, _Ctx({"x-codex-turn-metadata": turn}))) == []


def test_peers_list_shows_the_name_that_the_user_gave(
    make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch
) -> None:
    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    me = make_peer(id="claude:ME", name="claude-me")
    other = make_peer(
        id="claude:OTHER",
        name="claude-other",
        pid=os.getppid(),
        proc_start=proc_start(os.getppid()) or "",
    )
    for peer in (me, other):
        mesh.registry.put(peer)
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getppid()}.json").write_text(json.dumps({"name": "alpha-arch"}))
    monkeypatch.setattr(tools, "_context", lambda env, codex_thread, cwd: (mesh, me))
    [row] = [p for p in mcp_server.peers_list("all") if p["id"] == "claude:OTHER"]
    assert (row["name"], row["auto_name"]) == ("alpha-arch", "claude-other")


def _call(name: str, arguments: dict[str, Any]) -> Any:
    from mcp.client import Client

    async def run() -> Any:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_the_first_result_from_a_newer_peermesh_says_that_the_tool_list_is_old(
    ctx: Mesh, ran: dict[str, str]
) -> None:
    ran["version"] = "9.9.9"
    result = _call("peers_list", {"scope": "all"})
    notice = text.STALE_SERVER.format(running=peermesh.__version__, installed="9.9.9")
    assert result.content[-1].text == notice
    # Claude Code gives the agent only the structured result. The Claude Code mod reads its
    # rows from "result", which keeps its shape.
    assert result.structured_content["notice"] == notice
    assert [p["name"] for p in result.structured_content["result"]] == ["codex-x"]
    # The note comes one time for each newer version.
    assert "notice" not in _call("peers_list", {"scope": "all"}).structured_content
    ran["version"] = "9.9.10"
    assert "9.9.10" in _call("peers_list", {"scope": "all"}).structured_content["notice"]


def test_a_failed_call_from_a_newer_peermesh_carries_the_note(
    ctx: Mesh, ran: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran["version"] = "9.9.9"
    notice = text.STALE_SERVER.format(running=peermesh.__version__, installed="9.9.9")
    for name, arguments in (("peers_list", {"scope": "galaxy"}), ("peers_status", {})):
        monkeypatch.setattr(mcp_server, "_noticed", set())
        result = _call(name, arguments)
        assert result.is_error, name
        # A missing argument fails before the tool runs, so it has no version and no note.
        assert result.content[0].text.endswith(notice) == (name == "peers_list"), name


def test_a_crash_keeps_the_generic_text(ctx: Mesh, monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(msg_id: str) -> dict[str, Any]:
        raise PermissionError("the ledger cannot be read")

    monkeypatch.setattr(ctx, "track", crash)
    crashed = _call("peers_track", {"msg_id": "a" * 32})
    assert crashed.is_error
    assert crashed.content[0].text.startswith("Error executing tool peers_track")
    assert "ledger" not in crashed.content[0].text


def test_a_result_from_the_same_peermesh_has_no_note(ctx: Mesh) -> None:
    result = _call("peers_list", {"scope": "all"})
    assert all("peermesh: This MCP server" not in getattr(c, "text", "") for c in result.content)
    assert "notice" not in result.structured_content


def test_an_error_reaches_the_agent_with_its_text(ctx: Mesh) -> None:
    # The MCP library shows only "Error executing tool" for an exception that is not a ToolError.
    result = _call("peers_list", {"scope": "galaxy"})
    assert result.is_error
    assert result.content[0].text == (
        "Error executing tool peers_list: The scope is not valid. Use 'repo', 'worktree' or 'all'."
    )


def _fake_install(root: Path, tools_py: str) -> list[str]:
    """A command that runs a peermesh from `root`, which stands in for a new installation."""
    package = root / "peermesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text(tools_py)
    start = (
        f"import sys, runpy; sys.path.insert(0, {str(root)!r}); "
        "runpy.run_module('peermesh.tools', run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", start]


def _new_process(arguments: dict[str, Any] | None = None, **env: str) -> dict[str, Any]:
    return mcp_server._in_new_process(
        "peers_list", arguments or {}, {**os.environ, **env}, codex_thread=False, cwd=None
    )


def test_a_call_in_a_new_process_gives_the_refusal_text_and_the_version() -> None:
    reply = _new_process({"scope": "galaxy"})
    assert reply == {
        "error": "The scope is not valid. Use 'repo', 'worktree' or 'all'.",
        "version": peermesh.__version__,
    }


def test_a_call_in_a_new_process_runs_the_installation_of_the_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = 'import sys\nsys.stdout.write(\'{"result": "new code", "version": "9.9.9"}\')\n'
    monkeypatch.setattr(mcp_server, "_command", lambda: _fake_install(tmp_path, body))
    assert _new_process() == {"result": "new code", "version": "9.9.9"}


def test_a_call_in_a_new_process_ignores_a_peermesh_module_in_the_folder_or_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A module that a session can write must not run in place of peermesh, as any session.
    for name in ("folder", "elsewhere"):
        package = tmp_path / name / "peermesh"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"raise SystemExit('{name} ran')\n")
    monkeypatch.chdir(tmp_path / "folder")
    elsewhere = str(tmp_path / "elsewhere")
    for pythonpath in (".", "", elsewhere, f":{elsewhere}"):
        reply = _new_process({"scope": "galaxy"}, PYTHONPATH=pythonpath)
        assert reply["version"] == peermesh.__version__, pythonpath


def test_a_crash_in_the_new_process_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_server, "_command", lambda: _fake_install(tmp_path, "raise SystemExit(3)\n")
    )
    with pytest.raises(RuntimeError, match="ended with code 3"):
        _new_process()


def test_a_codex_thread_reaches_the_new_process(tmp_path: Path) -> None:
    # The new process refuses to register a Codex thread whose folder the call does not give.
    env = {**os.environ, "CODEX_THREAD_ID": "T-new", "PEERMESH_HOME": str(tmp_path / "pm")}
    reply = mcp_server._in_new_process("peers_list", {}, env, codex_thread=True, cwd=None)
    assert "does not give its folder" in reply["error"]


def test_a_call_runs_here_with_a_note_when_the_installation_cannot_run(
    ctx: Mesh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_runner", mcp_server._in_new_process)
    for command, reason in (
        (["/nonexistent/python"], "its Python interpreter cannot start"),
        (
            [
                sys.executable,
                "-I",
                "-c",
                "raise ModuleNotFoundError(\"No module named 'peermesh'\")",
            ],
            "the environment does not hold peermesh",
        ),
    ):
        monkeypatch.setattr(mcp_server, "_command", lambda c=command: c)
        result = _call("peers_list", {"scope": "all"})
        assert [p["name"] for p in result.structured_content["result"]] == ["codex-x"]
        assert reason in result.structured_content["notice"]
