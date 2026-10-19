from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any, cast

import pytest

import peermesh
from peermesh import mcp_server, paths, text
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry, proc_start
from peermesh.service import Mesh, accepts_short_ids
from peermesh.transports import Outcome


class Fake:
    def deliver(self, target: Peer, rendered: str, msg: object) -> Outcome:
        return Outcome("queued", "next turn")


@pytest.fixture
def ctx(make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch) -> Mesh:
    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    me = make_peer(id="claude:ME", name="claude-me")
    mesh.registry.put(me)
    mesh.registry.put(make_peer(id="codex:X", name="codex-x", runtime="codex", endpoint="X"))
    monkeypatch.setattr(mcp_server, "_context", lambda ctx=None: (mesh, me))
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
    with pytest.raises(ValueError, match="scope"):
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
    mcp_server._context()
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
        with pytest.raises(ValueError, match="not an ancestor"):
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
    with pytest.raises(ValueError, match="does not give its folder"):
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
    (sessions / f"{os.getppid()}.json").write_text(json.dumps({"name": "rotom-arch"}))
    monkeypatch.setattr(mcp_server, "_context", lambda ctx=None: (mesh, me))
    [row] = [p for p in mcp_server.peers_list("all") if p["id"] == "claude:OTHER"]
    assert (row["name"], row["auto_name"]) == ("rotom-arch", "claude-other")


def _call(name: str, arguments: dict[str, Any]) -> Any:
    from mcp.client import Client

    async def run() -> Any:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_a_tool_result_says_when_a_newer_peermesh_is_installed(
    ctx: Mesh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_installed_version", lambda: "9.9.9")
    result = _call("peers_list", {"scope": "all"})
    notice = text.STALE_SERVER.format(running=peermesh.__version__, installed="9.9.9")
    assert result.content[-1].text == notice
    # The structured result, which the Claude Code mod reads, keeps its shape.
    assert [p["name"] for p in result.structured_content["result"]] == ["codex-x"]


def test_a_tool_result_has_no_note_while_the_installed_peermesh_is_the_same(
    ctx: Mesh, monkeypatch: pytest.MonkeyPatch
) -> None:
    for installed in (peermesh.__version__, None):
        monkeypatch.setattr(mcp_server, "_installed_version", lambda v=installed: v)
        result = _call("peers_list", {"scope": "all"})
        assert all("reconnect" not in getattr(c, "text", "") for c in result.content)


def test_the_installed_version_comes_from_the_imported_package_file() -> None:
    assert mcp_server._installed_version() == peermesh.__version__
