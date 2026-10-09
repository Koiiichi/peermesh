from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from peermesh import hooks, install, paths, text
from peermesh.envelope import AFTER_COMPLETION_LINE, INFO_LINE, Message
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry
from peermesh.service import Mesh
from peermesh.transports import Outcome


@dataclass
class FakeTransport:
    calls: list[tuple[str, str, Message]] = field(default_factory=list)

    def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome:
        self.calls.append((target.id, rendered, msg))
        return Outcome("delivered")


@dataclass
class World:
    mesh: Mesh
    claude: FakeTransport
    codex: FakeTransport
    peers: dict[str, Peer]

    def put(self, key: str, **changes: Any) -> Peer:
        peer = self.peers[key]
        for name, value in changes.items():
            setattr(peer, name, value)
        self.mesh.registry.put(peer)
        return peer


@pytest.fixture
def world(make_peer: Callable[..., Peer]) -> World:
    claude, codex = FakeTransport(), FakeTransport()
    mesh = Mesh(Registry(), Ledger(), {"claude": claude, "codex": codex})
    common = {"repo_key": "k", "capabilities": ["inbox"], "last_seen": 0.0}
    peers = {
        "a": make_peer(id="claude:A", name="claude-a", **common),
        "b": make_peer(id="claude:B", name="claude-b", **common),
        "x": make_peer(id="codex:X", name="codex-x", runtime="codex", endpoint="X", **common),
    }
    for peer in peers.values():
        mesh.registry.put(peer)
    return World(mesh, claude, codex, peers)


def _hook(world: World, event: str, key: str, **payload: Any) -> tuple[Any, list[Any]]:
    peer = world.peers[key]
    emitted: list[Any] = []
    out = hooks.handle(
        world.mesh,
        event,
        peer.runtime,
        {"session_id": peer.id.split(":", 1)[1], "cwd": peer.cwd, **payload},
        env={},
        host_pid=os.getpid(),
        emit=emitted.append,
    )
    return out, emitted


def _outcomes(world: World, msg_id: str) -> list[str]:
    return [e["outcome"] for e in world.mesh.ledger.entries() if e.get("id") == msg_id]


def test_busy_target_gets_pending_message_and_no_native_delivery(world: World) -> None:
    world.put("x", status="busy")
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Renamed total() to sum().")
    assert res.status == "pending" and world.codex.calls == []
    assert [w.id for w in world.mesh.inbox.waiting("codex:X")] == [res.msg_id]
    assert _outcomes(world, str(res.msg_id)) == ["pending"]


def test_idle_target_information_waits_and_request_wakes(world: World) -> None:
    [info] = world.mesh.send(world.peers["a"], "claude-b", "CI is green again.")
    assert info.status == "pending" and world.claude.calls == []
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Please rebase.", kind="request")
    assert req.status == "delivered"
    _, rendered, _ = world.claude.calls[0]
    assert AFTER_COMPLETION_LINE in rendered and INFO_LINE not in rendered


def test_target_without_post_tool_hook_uses_native_path(world: World) -> None:
    world.put("b", capabilities=[], status="busy")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "FYI: schema v2.")
    assert res.status == "delivered" and len(world.claude.calls) == 1
    assert INFO_LINE in world.claude.calls[0][1]


def test_urgent_request_routes(world: World) -> None:
    world.put("b", status="busy")
    world.put("x", status="busy")
    [claude] = world.mesh.send(
        world.peers["a"], "claude-b", "Stop: wrong branch.", "request", "now"
    )
    [codex] = world.mesh.send(world.peers["a"], "codex-x", "Stop: wrong branch.", "request", "now")
    assert claude.status == "delivered" and codex.status == "pending"
    world.put("x", host="daemon")
    [steer] = world.mesh.send(world.peers["b"], "codex-x", "Stop now.", "request", "now")
    assert steer.status == "delivered" and len(world.codex.calls) == 1


def test_post_tool_injects_in_send_order_exactly_once(world: World) -> None:
    world.put("x", status="busy")
    ids = [
        world.mesh.send(world.peers[s], "codex-x", f"note {i} from {s}")[0].msg_id
        for i, s in enumerate(["a", "b", "a"])
    ]
    out, emitted = _hook(world, "post-tool", "x")
    assert emitted == [out]
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert [context.index(f"msg={i}") for i in ids] == sorted(
        context.index(f"msg={i}") for i in ids
    )
    assert all(_outcomes(world, str(i)) == ["pending", "injected"] for i in ids)
    assert _hook(world, "post-tool", "x") == (None, [])


def test_failed_emit_leaves_messages_for_a_retry(world: World) -> None:
    world.put("x", status="busy")
    [res] = world.mesh.send(world.peers["a"], "codex-x", "keep me")

    def broken(out: Any) -> None:
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        hooks.handle(
            world.mesh,
            "post-tool",
            "codex",
            {"session_id": "X"},
            env={},
            host_pid=os.getpid(),
            emit=broken,
        )
    assert _outcomes(world, str(res.msg_id)) == ["pending"]
    out, _ = _hook(world, "post-tool", "x")
    assert out is not None and f"msg={res.msg_id}" in out["hookSpecificOutput"]["additionalContext"]


def _concurrent_hook(home: str, result: Any) -> None:
    os.environ["PEERMESH_HOME"] = home
    mesh = Mesh(Registry(), Ledger(), {})
    emitted: list[Any] = []
    hooks.handle(
        mesh, "post-tool", "codex", {"session_id": "X"}, env={}, host_pid=1, emit=emitted.append
    )
    result.put([e["hookSpecificOutput"]["additionalContext"] for e in emitted])


def test_parallel_post_tool_hooks_inject_each_message_once(world: World) -> None:
    world.put("x", status="busy")
    ids = [world.mesh.send(world.peers["a"], "codex-x", f"m{i}")[0].msg_id for i in range(5)]
    ctx = multiprocessing.get_context("spawn")
    result = ctx.Queue()
    procs = [
        ctx.Process(target=_concurrent_hook, args=(os.environ["PEERMESH_HOME"], result))
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    texts = [t for _ in procs for t in result.get(timeout=10)]
    assert sum(t.count("msg=") for t in texts) == 5
    assert all(sum(f"msg={i}" in t for t in texts) == 1 for i in ids)


def test_stop_keeps_information_for_the_next_prompt(world: World) -> None:
    world.put("b", status="busy")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "FYI: docs moved.")
    assert _hook(world, "stop", "b") == (None, [])
    assert world.mesh.registry.get("claude:B").status == "idle"  # type: ignore[union-attr]
    out, _ = _hook(world, "prompt", "b")
    assert f"msg={res.msg_id}" in out["hookSpecificOutput"]["additionalContext"]
    assert _outcomes(world, str(res.msg_id))[-1] == "injected"


def test_stop_continues_once_for_actionable_messages(world: World) -> None:
    world.put("b", status="busy")
    [info] = world.mesh.send(world.peers["a"], "claude-b", "FYI: docs moved.")
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Run the tests.", kind="request")
    out, emitted = _hook(world, "stop", "b")
    assert emitted == [out] and "decision" not in out
    context = out["hookSpecificOutput"]
    assert context["hookEventName"] == "Stop" and text.STOP_NOTE in context["additionalContext"]
    assert world.mesh.registry.get("claude:B").status == "busy"  # type: ignore[union-attr]
    [late] = world.mesh.send(world.peers["a"], "claude-b", "One more question.", kind="request")
    assert late.status == "pending"
    assert f"msg={info.msg_id}" in context["additionalContext"]
    assert f"msg={req.msg_id}" in context["additionalContext"]
    assert world.claude.calls == []


def test_stop_after_a_continuation_wakes_instead_of_blocking(world: World) -> None:
    world.put("b", status="busy")
    [info] = world.mesh.send(world.peers["a"], "claude-b", "FYI: docs moved.")
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Run the tests.", kind="request")
    assert _hook(world, "stop", "b", stop_hook_active=True) == (None, [])
    [(target, rendered, msg)] = world.claude.calls
    assert target == "claude:B" and msg.id == req.msg_id and AFTER_COMPLETION_LINE in rendered
    assert [w.id for w in world.mesh.inbox.waiting("claude:B")] == [info.msg_id]


def test_request_after_stop_is_not_left_in_the_inbox(world: World) -> None:
    world.put("b", status="busy")
    _hook(world, "stop", "b")
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Review 1a2b.", kind="review_request")
    assert req.status == "delivered" and world.mesh.inbox.waiting("claude:B") == []


def test_session_end_marks_waiting_messages_undelivered(world: World) -> None:
    world.put("x", status="busy")
    [res] = world.mesh.send(world.peers["a"], "codex-x", "too late")
    _hook(world, "session-end", "x")
    assert _outcomes(world, str(res.msg_id)) == ["pending", "undelivered"]
    assert not world.mesh.inbox.dir("codex:X").exists()


def test_track_reports_each_state(world: World) -> None:
    world.put("x", status="busy")
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Please review cart.py", kind="request")
    msg_id = str(res.msg_id)
    assert world.mesh.track(msg_id)["state"].startswith("pending")
    _hook(world, "post-tool", "x")
    assert world.mesh.track(msg_id)["state"].startswith("injected")
    world.mesh.reply(world.peers["x"], msg_id, "Found one bug in line 12.")
    assert world.mesh.track(msg_id)["state"].startswith("acknowledged")
    world.mesh.reply(world.peers["x"], msg_id, "Fixed the bug in 3c4d.", done=True)
    tracked = world.mesh.track(msg_id)
    assert tracked["state"].startswith("acted") and [r["done"] for r in tracked["replies"]] == [
        False,
        True,
    ]
    assert [h["outcome"] for h in tracked["history"]] == ["pending", "injected"]


def test_hooks_advertise_the_inbox_only_when_installed_with_it() -> None:
    mesh = Mesh(Registry(), Ledger(), {})
    payload = {"session_id": "T1", "cwd": os.getcwd()}
    hooks.handle(mesh, "session-start", "codex", payload, env={}, host_pid=os.getpid())
    assert "inbox" not in mesh.registry.get("codex:T1").capabilities  # type: ignore[union-attr]
    hooks.handle(mesh, "prompt", "codex", payload, env={}, host_pid=os.getpid(), inbox=True)
    assert "inbox" in mesh.registry.get("codex:T1").capabilities  # type: ignore[union-attr]


def test_installed_commands_declare_the_inbox() -> None:
    config = install.merge_hooks({}, "/bin/peers", "codex")
    commands = {e: g[0]["hooks"][0]["command"] for e, g in config["hooks"].items()}
    assert "--inbox" in commands["SessionStart"] and "--inbox" in commands["UserPromptSubmit"]
    assert "--inbox" not in commands["Stop"]
    assert install.post_tool_installed("codex") is False


def test_hook_status_newer_than_claude_native_status_wins(world: World) -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    pid = world.peers["b"].pid
    (sessions / f"{pid}.json").write_text(json.dumps({"status": "busy", "statusUpdatedAt": 5000}))
    world.put("b", status="idle", last_seen=6.0)
    [res] = world.mesh.send(world.peers["a"], "claude-b", "Please rebase.", kind="request")
    assert res.status == "delivered"
    world.put("b", status="idle", last_seen=4.0)
    [res] = world.mesh.send(world.peers["a"], "claude-b", "Please rebase again.", kind="request")
    assert res.status == "pending"


def test_refused_reply_does_not_count_as_acknowledged(world: World) -> None:
    world.put("x", status="busy")
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Please review cart.py", kind="request")
    world.claude.calls.clear()
    world.mesh.transports = {"claude": RefusingTransport(), "codex": world.codex}
    world.mesh.reply(world.peers["x"], str(res.msg_id), "Reviewed: one bug.", done=True)
    assert world.mesh.track(str(res.msg_id))["state"].startswith("pending")


class RefusingTransport:
    def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome:
        return Outcome("refused", "socket write failed")


def test_interrupted_wake_never_replays_a_delivered_message(world: World) -> None:
    world.put("b", status="busy")
    ids = [
        world.mesh.send(world.peers["a"], "claude-b", f"request {i}", kind="request")[0].msg_id
        for i in range(2)
    ]
    calls: list[str] = []

    class StopsOnSecond:
        def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome:
            if calls:
                raise KeyboardInterrupt
            calls.append(msg.id)
            return Outcome("delivered")

    world.mesh.transports = {"claude": StopsOnSecond(), "codex": world.codex}
    with pytest.raises(KeyboardInterrupt):
        _hook(world, "stop", "b", stop_hook_active=True)
    assert calls == [ids[0]]
    assert world.mesh.inbox.waiting("claude:B") == []
    assert _outcomes(world, str(ids[0]))[-2:] == ["sending", "delivered"]
    assert _outcomes(world, str(ids[1]))[-1] == "sending"
    world.put("b", status="busy")
    assert _hook(world, "post-tool", "b") == (None, [])


def test_stop_hook_outlasts_the_codex_delivery_timeout() -> None:
    config = install.merge_hooks({}, "/bin/peers", "codex")
    [stop] = [g for g in config["hooks"]["Stop"] if "# peermesh" in g["hooks"][0]["command"]]
    assert stop["hooks"][0]["timeout"] > 30


def test_codex_stop_continues_the_turn_with_a_block_decision(world: World) -> None:
    world.put("x", status="busy")
    world.mesh.send(world.peers["a"], "codex-x", "Run the tests.", kind="request")
    out, _ = _hook(world, "stop", "x")
    assert out["decision"] == "block" and text.STOP_NOTE in out["reason"]
