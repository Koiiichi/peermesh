from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from peermesh import paths
from peermesh.envelope import Message
from peermesh.errors import PeerError
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry
from peermesh.service import Mesh
from peermesh.transports import Outcome


@dataclass
class FakeTransport:
    outcome: Outcome = field(default_factory=lambda: Outcome("delivered"))
    calls: list[tuple[str, str, Message]] = field(default_factory=list)

    def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome:
        self.calls.append((target.id, rendered, msg))
        return self.outcome


@dataclass
class World:
    mesh: Mesh
    claude: FakeTransport
    codex: FakeTransport
    now: list[float]
    peers: dict[str, Peer]


@pytest.fixture
def world(make_peer: Callable[..., Peer]) -> World:
    now = [1000.0]
    claude, codex = FakeTransport(), FakeTransport()
    mesh = Mesh(Registry(), Ledger(), {"claude": claude, "codex": codex}, clock=lambda: now[0])
    peers = {
        "ca": make_peer(id="claude:A", name="claude-a", repo_key="k", worktree="/w1"),
        "cb": make_peer(id="claude:B", name="claude-b", repo_key="k", worktree="/w2"),
        "xa": make_peer(
            id="codex:A",
            name="codex-a",
            runtime="codex",
            endpoint="A",
            repo_key="k",
            worktree="/w1",
        ),
        "xb": make_peer(
            id="codex:B",
            name="codex-b",
            runtime="codex",
            endpoint="B",
            repo_key="k",
            worktree="/w3",
        ),
        "xc": make_peer(
            id="codex:C",
            name="codex-c",
            runtime="codex",
            endpoint="C",
            repo_key="other",
            worktree="/o",
        ),
    }
    for p in peers.values():
        mesh.registry.put(p)
    return World(mesh, claude, codex, now, peers)


def test_list_across_runtimes(world: World) -> None:
    me = world.peers["ca"]
    assert [p.name for p in world.mesh.list_peers(me)] == ["codex-a", "claude-b", "codex-b"]
    assert len(world.mesh.list_peers(me, "all")) == 4


def test_claude_to_codex_routes_to_codex_transport(world: World) -> None:
    [res] = world.mesh.send(
        world.peers["ca"], "codex-a", "Renamed total() to sum_prices() in cart.py @ 1a2b"
    )
    assert res.status == "delivered" and res.to == "codex-a"
    target, rendered, msg = world.codex.calls[0]
    assert target == "codex:A" and "from claude-a (claude)" in rendered
    assert "peers reply" in rendered


def test_codex_to_claude_routes_to_claude_transport(world: World) -> None:
    [res] = world.mesh.send(
        world.peers["xa"], "claude-a", "Implementation done at 9f8e.", kind="review_request"
    )
    assert res.status == "delivered"
    assert world.claude.calls[0][0] == "claude:A"
    assert "peers_reply(" in world.claude.calls[0][1]


def test_claude_to_claude_and_codex_to_codex(world: World) -> None:
    world.mesh.send(world.peers["ca"], "claude-b", "Decision: use SQLite.")
    world.mesh.send(world.peers["xa"], "codex-b", "Blocker: tests need Docker.")
    assert [c[0] for c in world.claude.calls] == ["claude:B"]
    assert [c[0] for c in world.codex.calls] == ["codex:B"]


def test_unknown_target_lists_live_names(world: World) -> None:
    [res] = world.mesh.send(world.peers["ca"], "codex-z", "hello there")
    assert res.status == "refused"
    assert "codex-a" in res.note and "claude-b" in res.note and "claude-a," not in res.note


def test_self_target_refused(world: World) -> None:
    [res] = world.mesh.send(world.peers["ca"], "claude-a", "hello there")
    assert res.status == "refused" and "this session" in res.note


def test_reply_to_a_broadcast_goes_to_the_sender_only(world: World) -> None:
    results = world.mesh.send(
        world.peers["ca"], ["codex-a", "codex-b", "claude-b"], "main is green"
    )
    assert [r.status for r in results] == ["delivered"] * 3
    assert all(c[2].broadcast for c in world.codex.calls + world.claude.calls)
    msg_id = world.codex.calls[0][2].id
    world.claude.calls.clear()
    res = world.mesh.reply(world.peers["xa"], msg_id, "Rebased on it, conflicts in api.py")
    assert res.status == "delivered" and res.to == "claude-a"
    [(target, _, reply)] = world.claude.calls
    assert target == "claude:A" and reply.hop == 1 and not reply.broadcast


def test_send_to_a_recent_sender_continues_its_thread(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "Which port does the API use?")
    world.now[0] += 60
    [answer] = world.mesh.send(world.peers["xa"], "claude-a", "Port 8080, see config.py.")
    reply = world.claude.calls[-1][2]
    assert reply.thread == first.msg_id and reply.hop == 1
    world.now[0] += 1000
    world.mesh.send(world.peers["xa"], "claude-a", "New topic: the CI cache is full.")
    fresh = world.claude.calls[-1][2]
    assert fresh.hop == 0 and fresh.thread == fresh.id
    assert answer.status == "delivered"


def test_ping_pong_through_send_stops_at_the_hop_limit(world: World) -> None:
    a, b = world.peers["ca"], world.peers["xa"]
    statuses = []
    for i in range(12):
        world.now[0] += 10
        sender, target = (a, "codex-a") if i % 2 == 0 else (b, "claude-a")
        [res] = world.mesh.send(sender, target, f"point {i}")
        statuses.append(res.status)
        if res.status == "refused":
            assert "loop limit" in res.note
            break
    assert statuses[-1] == "refused" and len(statuses) == 9


def test_rate_limit_counts_new_threads_only(world: World) -> None:
    a, b = world.peers["ca"], world.peers["xa"]
    sent = [world.mesh.send(a, "codex-a", f"topic {i}")[0] for i in range(6)]
    assert all(r.status == "delivered" for r in sent)
    assert world.mesh.send(a, "codex-a", "topic 6")[0].status == "refused"
    answer = world.mesh.reply(b, str(sent[0].msg_id), "On topic 0: the build fails on arm64.")
    follow_up = world.mesh.reply(a, str(answer.msg_id), "Use the x86 runner until 2c3d lands.")
    assert follow_up.status == "delivered", follow_up.note


def test_reply_routing_threads_and_hops(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "Please review cart.py", kind="request")
    assert first.msg_id is not None
    renamed = world.peers["ca"]
    renamed.name = "claude-renamed"
    world.mesh.registry.put(renamed)
    res = world.mesh.reply(world.peers["xa"], first.msg_id, "One bug: rounding in line 12.")
    assert res.status == "delivered" and res.to == "claude-renamed"
    reply_msg = world.claude.calls[0][2]
    assert reply_msg.in_reply_to == first.msg_id and reply_msg.thread == first.msg_id
    assert reply_msg.hop == 1 and reply_msg.kind == "reply"


def test_reply_and_track_accept_the_short_id_of_the_frame(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "Please review cart.py", kind="request")
    assert first.msg_id is not None
    res = world.mesh.reply(world.peers["xa"], first.msg_id[:8], "One bug: rounding in line 12.")
    assert res.status == "delivered"
    assert world.claude.calls[0][2].in_reply_to == first.msg_id
    assert world.mesh.track(first.msg_id[:8])["id"] == first.msg_id
    with pytest.raises(PeerError, match="No message"):
        world.mesh.reply(world.peers["xa"], first.msg_id[:4], "Too short to resolve.")


def test_short_id_that_matches_two_messages_is_refused(world: World) -> None:
    for msg_id in ("abcdef01" + "0" * 24, "abcdef01" + "1" * 24):
        world.mesh.send(world.peers["ca"], "codex-a", f"Question {msg_id[-1]}?", msg_id=msg_id)
    with pytest.raises(PeerError, match="more than one message"):
        world.mesh.reply(world.peers["xa"], "abcdef01", "The answer is 4.")


def test_reply_to_message_for_someone_else(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "x marks the spot")
    assert first.msg_id is not None
    with pytest.raises(PeerError, match="not sent to this session"):
        world.mesh.reply(world.peers["xb"], first.msg_id, "a real answer")


def test_ack_only_reply_refused(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "FYI schema v2 is live")
    assert first.msg_id is not None
    assert world.mesh.reply(world.peers["xa"], first.msg_id, "Thanks!").status == "refused"


def test_hop_limit_stops_ping_pong(world: World) -> None:
    a, b = world.peers["ca"], world.peers["xa"]
    [res] = world.mesh.send(a, "codex-a", "question 0")
    last = res.msg_id
    senders = [b, a]
    statuses = []
    for i in range(1, 10):
        world.now[0] += 20
        assert last is not None
        r = world.mesh.reply(senders[i % 2 == 0], last, f"answer {i}")
        statuses.append(r.status)
        if r.status == "refused":
            assert "loop limit" in r.note
            break
        last = r.msg_id
    assert statuses[-1] == "refused" and len(statuses) == 8


def test_rate_limit(world: World) -> None:
    for i in range(6):
        assert world.mesh.send(world.peers["ca"], "codex-a", f"update {i}")[0].status == "delivered"
    blocked = world.mesh.send(world.peers["ca"], "codex-a", "update 6")[0]
    assert blocked.status == "refused" and "rate limit" in blocked.note
    world.now[0] += 601
    assert world.mesh.send(world.peers["ca"], "codex-a", "update 7")[0].status == "delivered"


def test_duplicate_id_refused(world: World) -> None:
    world.mesh.send(world.peers["ca"], "codex-a", "one", msg_id="fixed")
    [again] = world.mesh.send(world.peers["ca"], "codex-a", "one", msg_id="fixed")
    assert again.status == "refused" and "Do not resend" in again.note
    assert len(world.codex.calls) == 1


def test_busy_target_reports_queued(world: World) -> None:
    world.codex.outcome = Outcome("queued", "next turn")
    assert world.mesh.send(world.peers["ca"], "codex-a", "when free: rebase")[0].status == "queued"


def test_offline_target_record_removed(world: World) -> None:
    world.claude.outcome = Outcome("refused", "peer offline", offline=True)
    world.mesh.send(world.peers["xa"], "claude-b", "are you there")
    assert world.mesh.registry.get("claude:B") is None


def test_ledger_write_failure_is_reported(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(entry: object) -> None:
        raise OSError("disk full")

    original = world.mesh.ledger.append
    calls: list[object] = []

    def fail_second(entry: dict[str, object]) -> None:
        calls.append(entry)
        if len(calls) == 2:
            boom(entry)
        original(entry)

    monkeypatch.setattr(world.mesh.ledger, "append", fail_second)
    [res] = world.mesh.send(world.peers["ca"], "codex-a", "hello there")
    assert res.status == "delivered" and res.note == "submitted, not recorded, do not resend"


def test_status_live_and_gone(world: World) -> None:
    world.codex.outcome = Outcome("queued")
    world.mesh.send(world.peers["ca"], "codex-a", "later")
    st = world.mesh.status("codex-a")
    assert st["alive"] is True and st["queued_last_hour"] == 1 and st["runtime"] == "codex"
    assert world.mesh.status("nobody")["alive"] is False


def test_register_keeps_existing_name(world: World, make_peer: Callable[..., Peer]) -> None:
    again = make_peer(id="codex:A", name="", runtime="codex", endpoint="A")
    assert world.mesh.register(again).name == "codex-a"
    fresh = make_peer(id="codex:NEW", name="", runtime="codex", endpoint="N", git_root="/r/demo")
    assert world.mesh.register(fresh).name.startswith("codex-demo-")


def test_user_rename_names_the_sender_and_keeps_the_record(
    world: World, make_peer: Callable[..., Peer]
) -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{world.peers['ca'].pid}.json").write_text(
        json.dumps({"name": "rotom-infra", "nameSource": "user"})
    )
    me = world.mesh.register(make_peer(id="claude:A", name="", repo_key="k", worktree="/w1"))
    assert me.name == "rotom-infra"
    world.mesh.send(me, "codex-a", "the rename is live")
    _, rendered, msg = world.codex.calls[-1]
    assert msg.from_name == "rotom-infra" and "from rotom-infra (claude)" in rendered
    assert world.mesh.registry.resolve("claude-a") is not None
    stored = world.mesh.registry._file("claude:A").read_text()
    assert json.loads(stored)["name"] == "claude-a"


def test_reservation_failure_refuses_without_delivery(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(entry: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(world.mesh.ledger, "append", boom)
    [res] = world.mesh.send(world.peers["ca"], "codex-a", "hello there")
    assert res.status == "refused" and "not sent" in res.note
    assert world.codex.calls == []


def test_slow_delivery_does_not_block_other_senders(world: World) -> None:
    import threading

    release = threading.Event()
    entered = threading.Event()
    slow = world.codex

    def blocking(target: Peer, rendered: str, msg: Message) -> Outcome:
        entered.set()
        release.wait(10)
        return Outcome("delivered")

    slow.deliver = blocking  # type: ignore[method-assign]
    first = threading.Thread(
        target=world.mesh.send, args=(world.peers["ca"], "codex-a", "slow one")
    )
    first.start()
    assert entered.wait(5)
    second = threading.Thread(
        target=world.mesh.send, args=(world.peers["xb"], "claude-b", "fast one")
    )
    second.start()
    second.join(timeout=3)
    blocked = second.is_alive()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not blocked
    assert world.claude.calls and world.claude.calls[0][0] == "claude:B"


def test_whoami_refuses_a_session_that_is_not_an_ancestor(world: World) -> None:
    import subprocess

    other = subprocess.Popen(["sleep", "30"])
    try:
        from peermesh.registry import proc_start

        record = world.peers["xa"]
        record.pid = other.pid
        record.proc_start = proc_start(other.pid) or ""
        world.mesh.registry.put(record)
        with pytest.raises(PeerError, match="not an ancestor"):
            world.mesh.whoami(env={"CODEX_THREAD_ID": "A"})
    finally:
        other.kill()
        other.wait()


def test_whoami_accepts_own_ancestor(world: World) -> None:
    assert world.mesh.whoami(env={"CODEX_THREAD_ID": "A"}).id == "codex:A"


def test_status_unknown_lists_live_names(world: World) -> None:
    st = world.mesh.status("nobody")
    assert st["alive"] is False
    assert "claude-a" in st["note"] and "codex-b" in st["note"]


def _slow(world: World, seconds: float) -> None:
    import time

    def deliver(target: Peer, rendered: str, msg: Message) -> Outcome:
        time.sleep(seconds)
        world.codex.calls.append((target.id, rendered, msg))
        return Outcome("delivered")

    world.codex.deliver = deliver  # type: ignore[method-assign]


def _parallel(n: int, fn: Callable[[int], object]) -> list[object]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(fn, range(n)))


def test_parallel_sends_with_one_id_deliver_once(world: World) -> None:
    _slow(world, 0.3)
    results = _parallel(
        5, lambda i: world.mesh.send(world.peers["ca"], "codex-a", "same", msg_id="dup")[0]
    )
    assert sorted(r.status for r in results) == ["delivered"] + ["refused"] * 4  # type: ignore[attr-defined]
    assert len(world.codex.calls) == 1


def test_parallel_burst_respects_rate_limit(world: World) -> None:
    _slow(world, 0.3)
    results = _parallel(12, lambda i: world.mesh.send(world.peers["ca"], "codex-a", f"m{i}")[0])
    assert sum(r.status == "delivered" for r in results) == 6  # type: ignore[attr-defined]


def test_reply_while_original_is_still_sending(world: World) -> None:
    from peermesh.envelope import new_message

    msg = new_message(world.peers["ca"], world.peers["xa"], "question", kind="request")
    with world.mesh.ledger.locked():
        world.mesh.ledger.append(
            msg.to_json() | {"sent_at": 1000.0, "outcome": "sending", "note": ""}
        )
    res = world.mesh.reply(world.peers["xa"], msg.id, "the answer is 42")
    assert res.status == "delivered" and world.claude.calls[0][2].hop == 1


def test_refusals_are_recorded_without_body(world: World) -> None:
    for i in range(6):
        world.mesh.send(world.peers["ca"], "codex-a", f"update {i}")
    world.mesh.send(world.peers["ca"], "codex-a", "update 6")
    world.mesh.send(world.peers["ca"], "codex-z", "to nobody")
    refused = [e for e in world.mesh.ledger.latest() if e["outcome"] == "refused"]
    assert [e["to_name"] for e in refused] == ["codex-a", "codex-z"]
    assert "rate limit" in refused[0]["note"] and "body" not in refused[0]
    assert all(e["id"].startswith("refused-") for e in refused)


def test_ack_only_refusal_is_recorded(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "FYI schema v2 is live")
    assert first.msg_id is not None
    world.mesh.reply(world.peers["xa"], first.msg_id, "ok")
    [refused] = [e for e in world.mesh.ledger.latest() if e["outcome"] == "refused"]
    assert refused["from"] == "codex:A" and refused["to"] == "claude:A"


def test_outcome_entry_does_not_repeat_the_body(world: World) -> None:
    [res] = world.mesh.send(world.peers["ca"], "codex-a", "the body text")
    lines = world.mesh.ledger.entries()
    assert [e["outcome"] for e in lines] == ["sending", "delivered"]
    assert "body" not in lines[1]
    found = world.mesh.ledger.find(str(res.msg_id))
    assert found is not None and found["body"] == "the body text"


def test_repeated_replies_to_one_message_are_capped(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "Please review cart.py", kind="request")
    statuses = [
        world.mesh.reply(world.peers["xa"], str(first.msg_id), f"finding {i}").status
        for i in range(12)
    ]
    assert statuses == ["delivered"] * 8 + ["refused"] * 4
    [refused] = [e for e in world.mesh.ledger.latest() if e["outcome"] == "refused"][:1]
    assert "messages in this thread" in refused["note"]


def test_slow_collaboration_never_reaches_the_loop_limit(world: World) -> None:
    """The gaps, in minutes, of a real 32-minute thread that the old hop limit refused."""
    a, b = world.peers["ca"], world.peers["xa"]
    [first] = world.mesh.send(a, "codex-a", "Please verify PR 61.", kind="request")
    last, senders = str(first.msg_id), [b, a]
    for i, gap in enumerate([0.2, 4.6, 4.6, 14.2, 2.6, 0.9, 5.4, 0.1, 3.0, 6.0, 1.0, 4.0]):
        world.now[0] += gap * 60
        res = world.mesh.reply(senders[i % 2], last, f"step {i}: findings and the next check")
        assert res.status == "delivered", res.note
        last = str(res.msg_id)
    world.now[0] += 300
    [handoff] = world.mesh.send(b, "claude-a", "Merged as fef7a0bc.", kind="handoff")
    assert handoff.status == "delivered", handoff.note


def test_one_sender_cannot_flood_a_thread(world: World) -> None:
    [first] = world.mesh.send(world.peers["ca"], "codex-a", "Please review cart.py", kind="request")
    notes = []
    for i in range(10):
        world.now[0] += 5
        res = world.mesh.reply(world.peers["xa"], str(first.msg_id), f"finding {i}")
        notes.append(res.note if res.status == "refused" else res.status)
    assert notes[:8] == ["delivered"] * 8 and "flood limit" in notes[8]
    world.now[0] += 600
    assert world.mesh.reply(world.peers["xa"], str(first.msg_id), "late finding").status == (
        "delivered"
    )
