from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

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
    assert target == "codex:A" and "(claude, id claude:A)" in rendered
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


def test_broadcast_marks_messages_and_blocks_reply(world: World) -> None:
    results = world.mesh.send(
        world.peers["ca"], ["codex-a", "codex-b", "claude-b"], "main is green"
    )
    assert [r.status for r in results] == ["delivered"] * 3
    assert all(c[2].broadcast for c in world.codex.calls + world.claude.calls)
    msg_id = world.codex.calls[0][2].id
    res = world.mesh.reply(world.peers["xa"], msg_id, "Rebased on it, conflicts in api.py")
    assert res.status == "refused" and "broadcast" in res.note


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
        world.now[0] += 1000
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

    monkeypatch.setattr(world.mesh.ledger, "append", boom)
    [res] = world.mesh.send(world.peers["ca"], "codex-a", "hello there")
    assert res.status == "delivered" and res.note == "submitted, not recorded, do not resend"


def test_status_live_and_gone(world: World) -> None:
    world.codex.outcome = Outcome("queued")
    world.mesh.send(world.peers["ca"], "codex-a", "later")
    st = world.mesh.status("codex-a")
    assert st["alive"] is True and st["queued_last_hour"] == 1 and st["runtime"] == "codex"
    assert world.mesh.status("nobody") == {"target": "nobody", "alive": False}


def test_register_keeps_existing_name(world: World, make_peer: Callable[..., Peer]) -> None:
    again = make_peer(id="codex:A", name="", runtime="codex", endpoint="A")
    assert world.mesh.register(again).name == "codex-a"
    fresh = make_peer(id="codex:NEW", name="", runtime="codex", endpoint="N", git_root="/r/demo")
    assert world.mesh.register(fresh).name.startswith("codex-demo-")
