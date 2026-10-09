"""The four operations: list, send, reply, status. Transports stay behind one interface."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from peermesh import identity
from peermesh.envelope import EnvelopeError, Message, is_ack_only, new_message, render
from peermesh.errors import PeerError
from peermesh.inbox import Inbox, Waiting
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry, Scope, claude_native_state, is_alive, scoped
from peermesh.transports import Transport
from peermesh.transports.claude import ClaudeSocketTransport
from peermesh.transports.codex import CodexTransport

RATE_LIMIT = 6
RATE_WINDOW_S = 600.0
# A reply counts as an acknowledgement only when it left the replying session.
SENT_OUTCOMES = ("delivered", "queued", "pending", "injected")
PENDING_NOTE = "The peer reads the message at its next tool call or prompt."
# How a ledger outcome reads for a person or an agent that tracks one message.
STATES = {
    "sending": "sending: the delivery did not finish",
    "pending": "pending: waits in the inbox for the next tool call or prompt of the peer",
    "injected": "injected: a hook of the peer put it into the model context",
    "delivered": "accepted: the runtime of the peer took it; it starts or joins a turn",
    "queued": "queued: Codex runs it as the next turn of the peer",
    "refused": "refused: not sent",
    "undelivered": "undelivered: the peer session ended before a hook injected it",
}


@dataclass(frozen=True)
class SendResult:
    msg_id: str | None
    to: str
    status: str
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Mesh:
    def __init__(
        self,
        registry: Registry,
        ledger: Ledger,
        transports: Mapping[str, Transport],
        clock: Callable[[], float] = time.time,
        inbox: Inbox | None = None,
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.transports = transports
        self.clock = clock
        self.inbox = inbox or Inbox()

    @classmethod
    def default(cls) -> Mesh:
        return cls(
            Registry(),
            Ledger(),
            {"claude": ClaudeSocketTransport(), "codex": CodexTransport()},
        )

    def register(self, peer: Peer) -> Peer:
        existing = self.registry.get(peer.id)
        peer.name = existing.name if existing and existing.name else self.registry.unique_name(peer)
        self.registry.put(peer)
        return peer

    def whoami(
        self,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        host_pid: int | None = None,
    ) -> Peer:
        env = os.environ if env is None else env
        runtime, session_id = identity.session_from_env(env)
        existing = self.registry.get(f"{runtime}:{session_id}")
        if existing is not None and is_alive(existing):
            if existing.pid not in identity.ancestors(os.getpid()):
                raise PeerError(
                    f"The session {existing.name} is not an ancestor of this process. "
                    "Run peers from the shell of your own agent session."
                )
            return existing
        peer = identity.detect(runtime, session_id, cwd or os.getcwd(), env, host_pid)
        return self.register(peer)

    def _enrich(self, peer: Peer) -> Peer:
        """Use Claude Code's own status unless a peermesh hook recorded a newer one."""
        if peer.runtime == "claude":
            native = claude_native_state(peer.pid)
            if native is not None and native[1] >= peer.last_seen:
                peer.status = native[0]
        return peer

    def list_peers(self, me: Peer, scope: Scope = "repo") -> list[Peer]:
        return [self._enrich(p) for p in scoped(self.registry.live(), me, scope)]

    def send(
        self,
        me: Peer,
        to: str | Sequence[str],
        body: str,
        kind: str = "info",
        urgency: str = "normal",
        *,
        msg_id: str | None = None,
    ) -> list[SendResult]:
        targets = [to] if isinstance(to, str) else list(to)
        if not targets:
            raise PeerError("Name at least one target peer.")
        broadcast = len(targets) > 1
        return [
            self._send_one(
                me,
                t,
                body,
                kind,
                urgency,
                broadcast=broadcast,
                msg_id=None if broadcast else msg_id,
            )
            for t in targets
        ]

    def reply(self, me: Peer, msg_id: str, body: str, *, done: bool = False) -> SendResult:
        """Reply in the thread of msg_id. `done` reports that the requested work is complete."""
        original = self.ledger.find(msg_id)
        if original is None:
            raise PeerError(f"No message with id {msg_id!r} is in the ledger.")
        if original.get("to") != me.id:
            raise PeerError("That message was not sent to this session. Use peers_send.")
        sender_name = str(original.get("from_name", ""))
        sender_id = str(original["from"])
        if original.get("broadcast"):
            return self._refused(
                me,
                sender_id,
                sender_name,
                "reply",
                "The message is a broadcast. Do not reply to a broadcast. "
                "Use peers_send if the sender must know something.",
            )
        if is_ack_only(body):
            return self._refused(
                me,
                sender_id,
                sender_name,
                "reply",
                "The reply only acknowledges the message. Do not send it. "
                "Send a reply only when it contains new information, an answer, or a decision.",
            )
        return self._send_one(
            me,
            str(original["from"]),
            body,
            "reply",
            "normal",
            broadcast=False,
            in_reply_to=msg_id,
            thread=str(original.get("thread", msg_id)),
            hop=int(original.get("hop", 0)) + 1,
            done=done,
        )

    def status(self, target_ref: str) -> dict[str, Any]:
        target = self.registry.resolve(target_ref)
        if target is None:
            return {"target": target_ref, "alive": False, "note": self._unknown(target_ref, None)}
        self._enrich(target)
        now = self.clock()
        queued = sum(
            1
            for e in self.ledger.latest()
            if e.get("to") == target.id
            and e.get("outcome") == "queued"
            and now - float(e.get("sent_at", 0.0)) < 3600
        )
        pending = len(self.inbox.waiting(target.id))
        return {
            "id": target.id,
            "name": target.name,
            "runtime": target.runtime,
            "status": target.status,
            "alive": True,
            "seconds_since_seen": round(now - target.last_seen),
            "branch": target.branch,
            "worktree": target.worktree,
            "host": target.host,
            "queued_last_hour": queued,
            "pending_in_inbox": pending,
        }

    def _refused(
        self,
        me: Peer,
        to_id: str,
        to_name: str,
        kind: str,
        note: str,
        *,
        locked: bool = False,
    ) -> SendResult:
        """Return a refusal and record it, so refusals appear in the ledger and in `peers log`.

        A refusal record has its own id and no body. A record that cannot be written does not
        change the refusal.
        """
        entry = {
            "v": 1,
            "id": f"refused-{uuid.uuid4().hex}",
            "from": me.id,
            "from_name": me.name,
            "to": to_id,
            "to_name": to_name,
            "kind": kind,
            "sent_at": self.clock(),
            "outcome": "refused",
            "note": note,
        }
        try:
            if locked:
                self.ledger.append(entry)
            else:
                with self.ledger.locked():
                    self.ledger.append(entry)
        except OSError:
            pass
        return SendResult(None, to_name, "refused", note)

    def _unknown(self, target_ref: str, me: Peer | None) -> str:
        names = sorted(p.name for p in self.registry.live() if me is None or p.id != me.id)
        return (
            f"No live peer has the name or id {target_ref!r}. "
            f"Live peers: {', '.join(names) or 'none'}."
        )

    def _send_one(
        self,
        me: Peer,
        target_ref: str,
        body: str,
        kind: str,
        urgency: str,
        *,
        broadcast: bool,
        msg_id: str | None = None,
        in_reply_to: str | None = None,
        thread: str | None = None,
        hop: int = 0,
        done: bool = False,
    ) -> SendResult:
        target = self.registry.resolve(target_ref)
        if target is None:
            return self._refused(me, target_ref, target_ref, kind, self._unknown(target_ref, me))
        if target.id == me.id:
            return self._refused(
                me,
                target.id,
                target.name,
                kind,
                "The target is this session. Send the message to a different peer.",
            )
        self._enrich(target)
        try:
            msg = new_message(
                me,
                target,
                body,
                kind,
                urgency,
                in_reply_to=in_reply_to,
                thread=thread,
                hop=hop,
                broadcast=broadcast,
                msg_id=msg_id,
                done=done,
            )
        except EnvelopeError as exc:
            return self._refused(me, target.id, target.name, kind, str(exc))
        # The lock covers only the checks and the "sending" record. Delivery can take tens of
        # seconds and runs outside it. The record makes a retry of this id a duplicate even if
        # this process ends during delivery.
        with self.ledger.locked():
            if self.ledger.find(msg.id) is not None:
                return SendResult(
                    msg.id,
                    target.name,
                    "refused",
                    "This message id is in the ledger. The message was sent before. Do not resend.",
                )
            now = self.clock()
            if self.ledger.count_recent(me.id, target.id, RATE_WINDOW_S, now) >= RATE_LIMIT:
                return self._refused(
                    me,
                    target.id,
                    target.name,
                    kind,
                    f"rate limit: {RATE_LIMIT} messages to this peer in 10 minutes. "
                    "Wait, or ask the user.",
                    locked=True,
                )
            # The status can change between resolve() and the lock; a Stop hook changes it
            # under this lock, so the inbox decision sees the status that the hook wrote.
            target = self._enrich(self.registry.get(target.id) or target)
            to_inbox = _wants_inbox(target, msg)
            try:
                self.ledger.append(
                    msg.to_json()
                    | {
                        "sent_at": now,
                        "outcome": "pending" if to_inbox else "sending",
                        "note": PENDING_NOTE if to_inbox else "",
                    }
                )
                if to_inbox:
                    self.inbox.put(
                        target.id, msg.to_json() | {"rendered": render(msg, target.runtime)}
                    )
            except OSError:
                return SendResult(
                    None,
                    target.name,
                    "refused",
                    "The ledger or the inbox cannot be written. The message was not sent.",
                )
        if to_inbox:
            return SendResult(msg.id, target.name, "pending", PENDING_NOTE)
        return self._deliver_native(target, msg, after_completion=_can_reopen(target, msg))

    def _deliver_native(self, target: Peer, msg: Message, *, after_completion: bool) -> SendResult:
        rendered = render(msg, target.runtime, after_completion=after_completion)
        outcome = self.transports[target.runtime].deliver(target, rendered, msg)
        if outcome.offline:
            self.registry.remove(target.id)
        entry = {"id": msg.id, "outcome": outcome.status, "note": outcome.note, "at": self.clock()}
        try:
            with self.ledger.locked():
                self.ledger.append(entry)
        except OSError:
            return SendResult(
                msg.id, target.name, outcome.status, "submitted, not recorded, do not resend"
            )
        return SendResult(msg.id, target.name, outcome.status, outcome.note)

    def take_waiting(self, peer: Peer) -> list[Waiting]:
        """Claim the inbox of peer. The caller holds the ledger lock."""
        return self.inbox.claim(peer.id)

    def record_injected(self, items: list[Waiting], via: str) -> None:
        """Record that a hook put items into the model context. The caller holds the lock."""
        for item in items:
            self.ledger.append(
                {"id": item.id, "outcome": "injected", "via": via, "at": self.clock()}
            )
        self.inbox.done(items)

    def wake(self, peer: Peer, items: list[Waiting]) -> None:
        """Deliver claimed items through the native path, which starts a new turn of peer.

        Call this without the ledger lock: the delivery records its own outcome.
        """
        for item in items:
            self._deliver_native(peer, Message.from_json(item.record), after_completion=True)
        self.inbox.done(items)

    def expire_inbox(self, peer_id: str) -> None:
        """Mark the waiting messages of an ended session as undelivered, and remove its inbox."""
        with self.ledger.locked():
            for item in self.inbox.remove(peer_id):
                self.ledger.append(
                    {
                        "id": item.id,
                        "outcome": "undelivered",
                        "note": "The peer session ended before a hook injected the message.",
                        "at": self.clock(),
                    }
                )

    def track(self, msg_id: str) -> dict[str, Any]:
        """The delivery history of one message and the replies to it."""
        record = self.ledger.find(msg_id)
        if record is None:
            raise PeerError(f"No message with id {msg_id!r} is in the ledger.")
        history = [
            {"outcome": e["outcome"], "at": e.get("at", e.get("sent_at")), "via": e.get("via")}
            for e in self.ledger.entries()
            if e.get("id") == msg_id and "outcome" in e
        ]
        replies = [
            e
            for e in self.ledger.latest()
            if e.get("in_reply_to") == msg_id and e.get("outcome") in SENT_OUTCOMES
        ]
        if any(r.get("done") for r in replies):
            state = "acted: the peer replied and reports that the requested work is complete"
        elif replies:
            state = "acknowledged: the peer replied"
        else:
            outcome = str(record.get("outcome", ""))
            state = STATES.get(outcome, outcome)
        return {
            "id": msg_id,
            "to": record.get("to_name"),
            "kind": record.get("kind"),
            "state": state,
            "history": history,
            "replies": [
                {"id": r["id"], "done": bool(r.get("done")), "body": str(r.get("body", ""))[:200]}
                for r in replies
            ],
        }


def _wants_inbox(target: Peer, msg: Message) -> bool:
    """True when a hook of the target, not the native inbox, delivers the message.

    A busy target gets the message at its next tool call. An idle target gets information at
    its next prompt, so information never opens a new turn. A request to an idle target starts
    a turn through the native path. An urgent request uses the native path, except on a busy
    Codex session that cannot be steered, where the next tool call is the fastest way in.
    """
    if "inbox" not in target.capabilities:
        return False
    if msg.urgency == "now":
        return target.runtime == "codex" and target.host != "daemon" and target.status == "busy"
    return target.status == "busy" or msg.kind == "info"


def _can_reopen(target: Peer, msg: Message) -> bool:
    """True when a native delivery can start a turn after the target finished its task."""
    return target.status == "idle" and msg.kind != "info"
