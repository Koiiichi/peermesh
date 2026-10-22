from __future__ import annotations

import json
import multiprocessing
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from peermesh import hooks, install, paths, registry
from peermesh.envelope import ENDINGS, INFO_NOTE, Message
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry, is_alive, proc_start
from peermesh.service import Mesh, accepts_short_ids, announce_short_ids
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


def _hook(
    world: World, event: str, key: str, env: dict[str, str] | None = None, **payload: Any
) -> tuple[Any, list[Any]]:
    peer = world.peers[key]
    emitted: list[Any] = []
    out = hooks.handle(
        world.mesh,
        event,
        peer.runtime,
        {"session_id": peer.id.split(":", 1)[1], "cwd": peer.cwd, **payload},
        env=env or {},
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
    [item] = world.mesh.inbox.waiting("codex:X")
    assert item.id == res.msg_id and "> Renamed total() to sum()." in item.record["rendered"]
    assert _outcomes(world, str(res.msg_id)) == ["pending"]


def test_idle_target_information_waits_and_request_wakes(world: World) -> None:
    [info] = world.mesh.send(world.peers["a"], "claude-b", "CI is green again.")
    assert info.status == "pending" and world.claude.calls == []
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Please rebase.", kind="request")
    assert req.status == "delivered"
    _, rendered, _ = world.claude.calls[0]
    assert ENDINGS["after_completion"] in rendered and INFO_NOTE not in rendered


def test_target_without_post_tool_hook_uses_native_path(world: World) -> None:
    world.put("b", capabilities=[], status="busy")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "FYI: schema v2.")
    assert res.status == "delivered" and len(world.claude.calls) == 1
    assert INFO_NOTE in world.claude.calls[0][1]


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
    assert [context.index(f"msg {i[:8]}") for i in ids] == sorted(
        context.index(f"msg {i[:8]}") for i in ids
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
    assert (
        out is not None
        and f"msg {res.msg_id[:8]}" in out["hookSpecificOutput"]["additionalContext"]
    )


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
    assert sum(t.count(" · msg ") for t in texts) == 5
    assert all(sum(f"msg {i[:8]}" in t for t in texts) == 1 for i in ids)


def test_stop_keeps_information_for_the_next_prompt(world: World) -> None:
    world.put("b", status="busy")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "FYI: docs moved.")
    assert _hook(world, "stop", "b") == (None, [])
    assert world.mesh.registry.get("claude:B").status == "idle"  # type: ignore[union-attr]
    out, _ = _hook(world, "prompt", "b")
    assert f"msg {res.msg_id[:8]}" in out["hookSpecificOutput"]["additionalContext"]
    assert _outcomes(world, str(res.msg_id))[-1] == "injected"


def test_stop_continues_once_for_actionable_messages(world: World) -> None:
    world.put("b", status="busy")
    [info] = world.mesh.send(world.peers["a"], "claude-b", "FYI: docs moved.")
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Run the tests.", kind="request")
    out, emitted = _hook(world, "stop", "b")
    assert emitted == [out] and "decision" not in out
    context = out["hookSpecificOutput"]
    assert context["hookEventName"] == "Stop" and ENDINGS["stop"] in context["additionalContext"]
    assert world.mesh.registry.get("claude:B").status == "busy"  # type: ignore[union-attr]
    [late] = world.mesh.send(world.peers["a"], "claude-b", "One more question.", kind="request")
    assert late.status == "pending"
    assert f"msg {info.msg_id[:8]}" in context["additionalContext"]
    assert f"msg {req.msg_id[:8]}" in context["additionalContext"]
    assert world.claude.calls == []


def test_stop_after_a_continuation_wakes_instead_of_blocking(world: World) -> None:
    world.put("b", status="busy")
    [info] = world.mesh.send(world.peers["a"], "claude-b", "FYI: docs moved.")
    [req] = world.mesh.send(world.peers["a"], "claude-b", "Run the tests.", kind="request")
    assert _hook(world, "stop", "b", stop_hook_active=True) == (None, [])
    [(target, rendered, msg)] = world.claude.calls
    assert target == "claude:B" and msg.id == req.msg_id and ENDINGS["after_completion"] in rendered
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
    assert out["decision"] == "block" and ENDINGS["stop"] in out["reason"]


def test_hooks_write_no_notice_in_the_session_that_has_the_mod(world: World) -> None:
    world.put("b", status="busy")
    world.mesh.send(world.peers["a"], "claude-b", "Please rebase.", kind="request")
    out, _ = _hook(world, "post-tool", "b", env={hooks.MOD_ENV: "B"})
    assert "systemMessage" not in out and "Please rebase." in str(out)
    # A session that a mod session started inherits the variable. It has no mod of its own.
    world.mesh.send(world.peers["a"], "claude-b", "One more.", kind="request")
    out, _ = _hook(world, "post-tool", "b", env={hooks.MOD_ENV: "A"})
    assert out["systemMessage"] == "peermesh: ← claude-a · request  One more."
    world.mesh.send(world.peers["a"], "claude-b", "Run the tests.", kind="request")
    out, _ = _hook(world, "prompt", "b", env={hooks.MOD_ENV: "B"})
    assert "systemMessage" not in out and "Run the tests." in str(out)


def test_the_notice_is_one_bounded_line_and_the_agent_gets_each_message_in_full(
    world: World,
) -> None:
    world.put("b", status="busy")
    long = "First line.\n\n\x1b[2J" + "word " * 60 + "\nlast line"
    world.mesh.send(world.peers["a"], "claude-b", long, kind="request")
    world.mesh.send(world.peers["a"], "claude-b", "A second message.", kind="info")
    out, _ = _hook(world, "post-tool", "b")
    notice = out["systemMessage"]
    assert "\n" not in notice and "\x1b" not in notice
    assert notice.startswith("peermesh: ← claude-a · request  First line. \\x1b[2Jword")
    assert notice.endswith("… (+1 more)") and len(notice) < 180
    # The notice is for the user. The agent reads both messages, each one whole.
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "> last line" in context and "> A second message." in context


def test_hooks_tell_the_user_which_messages_the_agent_got(world: World) -> None:
    world.put("b", status="busy")
    world.mesh.send(world.peers["a"], "claude-b", "Please rebase.", kind="request")
    out, _ = _hook(world, "post-tool", "b")
    assert out["systemMessage"] == "peermesh: ← claude-a · request  Please rebase."
    world.mesh.send(world.peers["a"], "claude-b", "Run the tests.", kind="review_request")
    out, _ = _hook(world, "prompt", "b")
    assert out["systemMessage"] == "peermesh: ← claude-a · review request  Run the tests."
    # Both runtimes show the Stop body to the user, so a notice would repeat it.
    world.put("b", status="busy")
    world.mesh.send(world.peers["a"], "claude-b", "One more check.", kind="request")
    out, _ = _hook(world, "stop", "b")
    assert "systemMessage" not in out


def test_claude_receiver_gets_short_ids_only_after_its_server_supports_them(world: World) -> None:
    [first] = world.mesh.send(world.peers["a"], "claude-b", "Please rebase.", kind="request")
    assert f'peers_reply("{first.msg_id}", "<text>")' in world.claude.calls[0][1]
    announce_short_ids(world.peers["b"])
    [second] = world.mesh.send(world.peers["a"], "claude-b", "Please rebase again.", kind="request")
    assert f'peers_reply("{str(second.msg_id)[:8]}", "<text>")' in world.claude.calls[1][1]
    world.put("b", status="busy")
    [third] = world.mesh.send(world.peers["a"], "claude-b", "And once more.", kind="request")
    out, _ = _hook(world, "post-tool", "b")
    context = out["hookSpecificOutput"]["additionalContext"]
    assert f'peers_reply("{str(third.msg_id)[:8]}", "<text>")' in context
    assert accepts_short_ids(world.peers["x"]) and not accepts_short_ids(world.peers["a"])
    _hook(world, "session-end", "b")
    assert not accepts_short_ids(world.peers["b"])


def _rollout(path: Any, *events: tuple[str, str, str, str]) -> str:
    """Write a Codex rollout with (timestamp, type, turn_id, root_turn_id) turn events."""
    lines = [json.dumps({"timestamp": "2026-10-22T00:00:00.000Z", "type": "session_meta"})]
    for when, kind, turn, root in events:
        payload = {"type": kind, "turn_id": turn, "root_turn_id": root}
        lines.append(json.dumps({"timestamp": when, "type": "event_msg", "payload": payload}))
    path.write_text("\n".join(lines) + "\n")
    return str(path)


START = "2026-10-22T00:24:49.405Z"
END = "2026-10-22T00:32:57.143Z"
BETWEEN = datetime.fromisoformat("2026-10-22T00:30:00+00:00").timestamp()


def test_codex_turn_that_ended_without_a_stop_hook_reads_idle(world: World, tmp_path: Any) -> None:
    # Codex wrote the end of the turn, but no Stop hook ran, as after a turn ends with an error.
    rollout = _rollout(
        tmp_path / "r.jsonl", (START, "task_started", "R", "R"), (END, "task_complete", "R", "R")
    )
    world.put("x", status="busy", last_seen=BETWEEN - 300, transcript=rollout)
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Please review cart.py", kind="request")
    assert res.status == "delivered"
    assert [c[0] for c in world.codex.calls] == ["codex:X"]


def test_nested_codex_turn_does_not_end_the_running_turn(world: World, tmp_path: Any) -> None:
    rollout = _rollout(
        tmp_path / "r.jsonl",
        (START, "task_started", "R", "R"),
        ("2026-10-22T00:25:00.000Z", "task_started", "G", "R"),
        ("2026-10-22T00:25:03.000Z", "task_complete", "G", "R"),
    )
    world.put("x", status="busy", last_seen=BETWEEN - 600, transcript=rollout)
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Please review cart.py", kind="request")
    assert res.status == "pending"


def test_codex_hook_newer_than_the_rollout_wins(world: World, tmp_path: Any) -> None:
    rollout = _rollout(
        tmp_path / "r.jsonl", (START, "task_started", "R", "R"), (END, "task_complete", "R", "R")
    )
    world.put("x", status="busy", last_seen=BETWEEN + 3600, transcript=rollout)
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Please review cart.py", kind="request")
    assert res.status == "pending"


def test_codex_native_state_reads_only_the_tail_of_a_large_rollout(tmp_path: Any) -> None:
    path = tmp_path / "r.jsonl"
    _rollout(path, (START, "task_started", "R", "R"), (END, "task_complete", "R", "R"))
    big = json.dumps({"type": "response_item", "payload": {"text": "x" * (2 << 20)}})
    path.write_text(path.read_text() + big + "\n")
    # The last top-level turn event is outside the tail, so the rollout says nothing.
    assert registry.codex_native_state(str(path)) is None
    with path.open("a") as fh:
        payload = {"type": "task_started", "turn_id": "S", "root_turn_id": "S"}
        fh.write(
            json.dumps(
                {"timestamp": "2026-10-22T01:00:00Z", "type": "event_msg", "payload": payload}
            )
            + "\n"
        )
    state = registry.codex_native_state(str(path))
    assert state is not None and state[0] == "busy"
    assert registry.codex_native_state(str(tmp_path / "missing.jsonl")) is None


def test_a_turn_event_counts_by_its_type_not_by_text_in_it(tmp_path: Any) -> None:
    # A completion carries the agent's last message, which can name another turn event.
    path = tmp_path / "r.jsonl"
    _rollout(path, (START, "task_started", "R", "R"))
    payload = {
        "type": "task_complete",
        "turn_id": "R",
        "root_turn_id": "R",
        # As JSON, the value is "task_started" in quotes, as an event type is.
        "last_agent_message": "task_started",
    }
    with path.open("a") as fh:
        fh.write(json.dumps({"timestamp": END, "type": "event_msg", "payload": payload}) + "\n")
    state = registry.codex_native_state(str(path))
    assert state is not None and state[0] == "idle"


def _app_session(host: str, title: str, *, archived: bool = False, cli_session: str = "B") -> None:
    """Write the Claude app's record of its session `host`, which runs Claude session B."""
    folder = paths.claude_app_dir() / "claude-code-sessions" / "account" / "org"
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "sessionId": host,
        "cliSessionId": cli_session,
        "title": title,
        "isArchived": archived,
    }
    (folder / f"{host}.json").write_text(json.dumps(record))


def _process_ended(world: World, peer_id: str) -> None:
    record = world.mesh.registry.get(peer_id)
    assert record is not None
    record.proc_start = "a process that ended"
    world.mesh.registry.put(record)


def test_a_session_that_the_claude_app_paused_stays_reachable(world: World) -> None:
    world.put("b", host_session="local_b", status="idle")
    _app_session("local_b", "worker-b")
    _hook(world, "session-end", "b", reason="other")
    _process_ended(world, "claude:B")
    [paused] = [p for p in world.mesh.all_peers() if p.id == "claude:B"]
    assert paused.status == "paused" and paused.name == "worker-b"
    [res] = world.mesh.send(world.peers["a"], "worker-b", "Run the tests.", kind="request")
    assert res.status == "pending" and "paused" in res.note
    assert world.claude.calls == []
    # The app runs the session again in a new process; its next prompt reads the message.
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": "B", "messagingSocketPath": "/tmp/b.sock"})
    )
    _hook(world, "session-start", "b", source="resume")
    resumed = world.mesh.registry.get("claude:B")
    assert resumed is not None and resumed.status == "idle" and is_alive(resumed)
    _hook(world, "prompt", "b")
    assert _outcomes(world, str(res.msg_id)) == ["pending", "injected"]


def test_a_session_that_the_claude_app_archived_ends(world: World) -> None:
    world.put("b", host_session="local_b", status="busy")
    _app_session("local_b", "worker-b", archived=True)
    [res] = world.mesh.send(world.peers["a"], "claude-b", "too late", kind="request")
    _hook(world, "session-end", "b", reason="other")
    assert world.mesh.registry.get("claude:B") is None
    assert _outcomes(world, str(res.msg_id)) == ["pending", "undelivered"]


def test_clear_ends_the_old_session_of_a_claude_app_session(world: World) -> None:
    world.put("b", host_session="local_b")
    _app_session("local_b", "worker-b")
    _hook(world, "session-end", "b", reason="clear")
    assert world.mesh.registry.get("claude:B") is None


def test_a_paused_session_leaves_when_the_app_archives_it(world: World) -> None:
    world.put("b", host_session="local_b")
    _app_session("local_b", "worker-b")
    _hook(world, "session-end", "b", reason="other")
    _process_ended(world, "claude:B")
    assert "claude:B" in {p.id for p in world.mesh.registry.live()}
    _app_session("local_b", "worker-b", archived=True)
    assert "claude:B" not in {p.id for p in world.mesh.registry.live()}


def test_a_child_claude_process_of_an_app_session_is_not_kept(world: World) -> None:
    # A child Claude process inherits the hostSessionId of its parent, whose session the app
    # record names; /clear maps the app session to a new session id the same way.
    world.put("b", host_session="local_b")
    _app_session("local_b", "orchestrator", cli_session="PARENT")
    _hook(world, "session-end", "b", reason="other")
    assert world.mesh.registry.get("claude:B") is None


def test_an_unreadable_app_record_keeps_a_paused_session(world: World) -> None:
    # A sandboxed process can be unable to read the app's folder; that is not a sign that the
    # session ended.
    world.put("b", host_session="local_b")
    _app_session("local_b", "worker-b")
    _hook(world, "session-end", "b", reason="other")
    _process_ended(world, "claude:B")
    sessions = paths.claude_app_dir() / "claude-code-sessions"
    for folder in (sessions, sessions / "account", sessions / "account" / "org"):
        folder.chmod(0)
        try:
            assert "claude:B" in {p.id for p in world.mesh.registry.live()}, folder
        finally:
            folder.chmod(0o700)
    (sessions / "account" / "org" / "local_b.json").write_text("{not json")
    assert "claude:B" in {p.id for p in world.mesh.registry.live()}


def test_a_resume_registers_again_while_the_old_process_still_runs(world: World) -> None:
    world.put("b", host_session="local_b", endpoint="/tmp/old.sock")
    _app_session("local_b", "worker-b")
    _hook(world, "session-end", "b", reason="other")
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": "B", "messagingSocketPath": "/tmp/new.sock"})
    )
    # The old process (this test's pid) has its recorded start time: it has not exited yet.
    _hook(world, "session-start", "b", source="resume")
    resumed = world.mesh.registry.get("claude:B")
    assert resumed is not None and resumed.endpoint == "/tmp/new.sock"
    assert resumed.status == "idle"


def _owned_by_another_process(world: World, peer_id: str) -> None:
    """Make the record belong to a running process other than the one that ends in the test."""
    record = world.mesh.registry.get(peer_id)
    assert record is not None
    record.pid = os.getppid()
    record.proc_start = proc_start(os.getppid()) or ""
    world.mesh.registry.put(record)


def test_the_end_of_an_old_process_leaves_the_resumed_session_alone(world: World) -> None:
    # The app ran the session again in a new process, which registered, before the old one ended.
    world.put("b", host_session="local_b", status="idle")
    _app_session("local_b", "worker-b")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "Note for later.", kind="info")
    _owned_by_another_process(world, "claude:B")
    for reason in ("other", "clear"):
        _hook(world, "session-end", "b", reason=reason)
        record = world.mesh.registry.get("claude:B")
        assert record is not None and record.status == "idle" and record.pid == os.getppid()
    assert world.mesh.inbox.waiting("claude:B") != []
    assert _outcomes(world, str(res.msg_id)) == ["pending"]


def test_a_paused_session_leaves_when_archived_while_its_old_process_runs(world: World) -> None:
    world.put("b", host_session="local_b")
    _app_session("local_b", "worker-b")
    _hook(world, "session-end", "b", reason="other")
    # The old process (this test's pid) has not exited yet.
    _app_session("local_b", "worker-b", archived=True)
    assert "claude:B" not in {p.id for p in world.mesh.registry.live()}


def test_an_unknown_ending_process_leaves_an_app_session_alone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old process's hook can run after its parent exited, so no ancestor names the session.
    world.put("b", host_session="local_b", status="idle")
    _app_session("local_b", "worker-b")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "Note for later.", kind="info")
    monkeypatch.setattr(hooks, "find_host", lambda *a, **k: None)
    for reason in ("other", "clear"):
        hooks.handle(
            world.mesh, "session-end", "claude", {"session_id": "B", "reason": reason}, env={}
        )
        record = world.mesh.registry.get("claude:B")
        assert record is not None and record.status == "idle"
    assert _outcomes(world, str(res.msg_id)) == ["pending"]


def test_a_resume_before_the_old_process_ends_takes_the_record(world: World) -> None:
    # The old process still runs and its record is not paused yet when the new one starts.
    world.put("b", host_session="local_b", status="idle", endpoint="/tmp/old.sock")
    _app_session("local_b", "worker-b")
    _owned_by_another_process(world, "claude:B")
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": "B", "messagingSocketPath": "/tmp/new.sock"})
    )
    _hook(world, "session-start", "b", source="resume")
    record = world.mesh.registry.get("claude:B")
    assert record is not None and record.pid == os.getpid() and record.endpoint == "/tmp/new.sock"
    # The old process ends now; the record is the new process's.
    hooks.handle(
        world.mesh,
        "session-end",
        "claude",
        {"session_id": "B", "reason": "other"},
        env={},
        host_pid=os.getppid(),
    )
    record = world.mesh.registry.get("claude:B")
    assert record is not None and record.status == "idle" and record.pid == os.getpid()


def _end_or_resume(event: str, host_pid: int, reason: str, gate: Any) -> None:
    """One hook of a separate process: the old process's end, or the new process's resume."""
    mesh = Mesh(Registry(), Ledger(), {})
    payload = {"session_id": "B", "cwd": os.getcwd(), "reason": reason, "source": "resume"}
    gate.wait(timeout=30)
    hooks.handle(mesh, event, "claude", payload, env={}, host_pid=host_pid)


def test_concurrent_end_and_resume_leave_the_session_with_the_new_process(world: World) -> None:
    # Two processes run at once: the SessionEnd of the process that the app ends (pid `old`),
    # and the SessionStart of the process that runs the session again (pid `new`).
    old, new = os.getppid(), os.getpid()
    _app_session("local_b", "worker-b")
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{new}.json").write_text(
        json.dumps({"sessionId": "B", "messagingSocketPath": "/tmp/new.sock"})
    )
    ctx = multiprocessing.get_context("spawn")
    for round_ in range(20):
        world.put("b", host_session="local_b", status="idle", endpoint="/tmp/old.sock")
        _owned_by_another_process(world, "claude:B")
        [res] = world.mesh.send(world.peers["a"], "claude-b", f"Note {round_}.", kind="info")
        gate = ctx.Barrier(2)
        reason = "clear" if round_ % 2 else "other"
        procs = [
            ctx.Process(target=_end_or_resume, args=("session-end", old, reason, gate)),
            ctx.Process(target=_end_or_resume, args=("session-start", new, reason, gate)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0
        record = world.mesh.registry.get("claude:B")
        assert record is not None, round_
        assert (record.pid, record.endpoint, record.status) == (new, "/tmp/new.sock", "idle")
        outcomes = _outcomes(world, str(res.msg_id))
        if reason == "other":
            # A pause keeps the messages in either order.
            assert outcomes == ["pending"], round_
        elif outcomes != ["pending"]:
            # /clear ends the session id, so its messages end with it, but only when the end
            # ran before the resume registered: never after.
            [ended] = [e for e in world.mesh.ledger.entries() if e.get("id") == res.msg_id][1:]
            assert ended["outcome"] == "undelivered" and ended["at"] <= record.last_seen, round_


def _whoami_in_child(host_pid: int) -> None:
    Mesh(Registry(), Ledger(), {}).whoami(env={"CODEX_THREAD_ID": "X"}, host_pid=host_pid)


def test_whoami_registers_only_after_a_session_end_finished(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SessionEnd removes the record and expires its inbox as one step under the ledger lock. A
    # whoami that needs to register the session waits for that step to finish.
    world.put("x", status="busy")
    [res] = world.mesh.send(world.peers["a"], "codex-x", "Run the tests.", kind="request")
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_whoami_in_child, args=(os.getpid(),))
    blocked: list[bool] = []
    expire = Mesh.expire_inbox

    def expire_while_a_whoami_waits(self: Mesh, peer_id: str, *, locked: bool = False) -> None:
        if locked:
            child.start()
            child.join(timeout=1.0)  # The child registers here if registration ignores the lock.
            blocked.append(child.is_alive())
        expire(self, peer_id, locked=locked)

    monkeypatch.setattr(Mesh, "expire_inbox", expire_while_a_whoami_waits)
    _hook(world, "session-end", "x")
    child.join(timeout=60)
    assert blocked == [True] and child.exitcode == 0
    assert world.mesh.registry.get("codex:X") is not None
    assert _outcomes(world, str(res.msg_id)) == ["pending", "undelivered"]


def _takes_the_lock_while(world: World, slow_call: str, run: Callable[[], Any]) -> None:
    """Run `run` while `hooks.<slow_call>` waits, and check that the ledger lock is free then."""
    import threading

    entered, release = threading.Event(), threading.Event()
    real = getattr(hooks, slow_call)

    def slow(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(timeout=30)
        return real(*args, **kwargs)

    setattr(hooks, slow_call, slow)
    try:
        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(timeout=30)
        took: list[bool] = []
        other = threading.Thread(target=lambda: took.append(_lock_once(world)))
        other.start()
        other.join(timeout=2.0)
        free = took == [True]
        release.set()
        worker.join(timeout=30)
        other.join(timeout=30)
    finally:
        setattr(hooks, slow_call, real)
    assert free, f"the ledger lock was held during {slow_call}"


def _lock_once(world: World) -> bool:
    with world.mesh.ledger.locked():
        return True


def test_registration_detects_the_session_outside_the_ledger_lock(world: World) -> None:
    payload = {"session_id": "Y", "cwd": os.getcwd()}
    _takes_the_lock_while(
        world,
        "detect",
        lambda: hooks.handle(
            world.mesh, "session-start", "codex", payload, env={}, host_pid=os.getpid()
        ),
    )
    assert world.mesh.registry.get("codex:Y") is not None


def test_session_end_finds_its_process_outside_the_ledger_lock(world: World) -> None:
    world.put("b", host_session="local_b")
    _app_session("local_b", "worker-b")
    payload = {"session_id": "B", "reason": "other"}
    _takes_the_lock_while(
        world,
        "find_host",
        lambda: hooks.handle(world.mesh, "session-end", "claude", payload, env={}),
    )


def test_adopt_replaces_a_record_only_when_it_is_gone_paused_or_unchanged(world: World) -> None:
    def candidate() -> Peer:
        peer = Peer.from_json(world.peers["b"].to_record())
        peer.endpoint = "/tmp/new.sock"
        return peer

    world.mesh.registry.remove("claude:B")
    assert world.mesh.adopt(candidate(), None).endpoint == "/tmp/new.sock"  # gone
    seen = world.put("b", endpoint="/tmp/seen.sock")
    assert world.mesh.adopt(candidate(), seen).endpoint == "/tmp/new.sock"  # unchanged
    world.put("b", endpoint="/tmp/other.sock")
    _owned_by_another_process(world, "claude:B")
    # Another process registered the session after this one read it: its record stays.
    assert world.mesh.adopt(candidate(), seen).endpoint == "/tmp/other.sock"
    record = world.mesh.registry.get("claude:B")
    assert record is not None
    record.status = "paused"
    world.mesh.registry.put(record)
    assert world.mesh.adopt(candidate(), seen).endpoint == "/tmp/new.sock"  # paused


def test_an_unknown_ending_process_pauses_a_record_whose_process_is_gone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ancestor names the session, and the recorded process has already exited: this end is
    # that process's, so the app session is paused and keeps its messages.
    world.put("b", host_session="local_b", status="idle")
    _app_session("local_b", "worker-b")
    [res] = world.mesh.send(world.peers["a"], "claude-b", "Note for later.", kind="info")
    _process_ended(world, "claude:B")
    monkeypatch.setattr(hooks, "find_host", lambda *a, **k: None)
    hooks.handle(
        world.mesh, "session-end", "claude", {"session_id": "B", "reason": "other"}, env={}
    )
    record = world.mesh.registry.get("claude:B")
    assert record is not None and record.status == "paused"
    assert "claude:B" in {p.id for p in world.mesh.registry.live()}
    assert _outcomes(world, str(res.msg_id)) == ["pending"]
