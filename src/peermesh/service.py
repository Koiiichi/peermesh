"""The four operations: list, send, reply, status. Transports stay behind one interface."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeVar

from peermesh import identity, paths, text
from peermesh.envelope import (
    EnvelopeError,
    Message,
    is_ack_only,
    new_message,
    render,
)
from peermesh.errors import PeerError
from peermesh.inbox import Inbox, Waiting
from peermesh.ledger import Ledger
from peermesh.registry import (
    Owner,
    Peer,
    Registry,
    Scope,
    app_keeps,
    claude_native_state,
    codex_native_state,
    is_alive,
    owner,
    proc_starts,
    scoped,
    started_after,
)
from peermesh.transports import Transport
from peermesh.transports.claude import ClaudeSocketTransport
from peermesh.transports.codex import CodexTransport

T = TypeVar("T")

RATE_LIMIT = 6
RATE_WINDOW_S = 600.0
# A send to a peer that messaged this session within this window continues that thread, so a
# reply sent with peers_send counts toward the hop limit like a reply sent with peers_reply.
CAUSAL_WINDOW_S = 900.0
# A hop counts only when a message answers its parent within this time. Agents in a loop answer
# in seconds; an answer that took longer followed work, so the count starts again.
LOOP_GAP_S = 120.0
THREAD_LIMIT = 8
# Message ids are uuid4 hex. A shorter reference must have MIN_PREFIX characters, so a typo of
# one or two characters cannot match a message by chance.
FULL_ID = 32
MIN_PREFIX = 6
# A reply counts as an acknowledgement only when it left the replying session.
SENT_OUTCOMES = ("delivered", "queued", "pending", "injected")
# Agents routed refused messages through Claude Code's own session messaging, which has no limits.
NO_DETOUR = "Do not send the message through a different channel."
# The kind of state file that a session writes when its peermesh resolves a short message id.
SHORT_IDS = "short-ids"
PENDING_NOTE = "The peer reads the message at its next tool call or prompt."
PAUSED_NOTE = (
    "The Claude app paused this idle session to free memory. The message waits, and the "
    "session reads it at its next prompt after the app runs it again. peermesh cannot run it; "
    "the user can, by opening the session."
)
# How a ledger outcome reads for a person or an agent that tracks one message.
STATES = {
    "sending": "sending: the delivery did not finish",
    "pending": "pending: waits in the inbox for the next tool call or prompt of the peer",
    "injected": "injected: a hook of the peer put it into the model context",
    "delivered": (
        "accepted: the runtime of the peer took it. It starts a turn or joins the current turn"
    ),
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
        return self.adopt(peer, self.registry.get(peer.id))

    # Registry invariant. A record names the process that owns its session (pid and ps start
    # time) and its registration (`generation`). adopt, claim and retire are the only writers
    # of records: each re-reads the record under the ledger lock and commits only when the
    # record is still the registration and the owner that the writer saw. A process that
    # cannot tell who it is owns nothing. Work that runs a subprocess (ps, git, detect) comes
    # before the lock.

    def adopt(self, peer: Peer, seen: Peer | None = None, *, seen_dead: bool = False) -> Peer:
        """Register peer, the session of the calling process, which read its record as `seen`.

        The record gets a new generation. It replaces the stored record when that is gone,
        when peer's process already owns it, when peer's process started after its owner's,
        or when it is still the registration in `seen` and that owner exited (`seen_dead`). An
        older process never takes a session back. Otherwise the stored record stays, and the
        live view of the stored record is returned.
        """
        given = (seen.auto_name or seen.name) if seen else None
        peer.name = given or self.registry.unique_name(peer)
        peer.generation = uuid.uuid4().hex
        with self.ledger.locked():
            current = self.registry.read(peer.id)
            if (
                current is None
                or owner(current) == owner(peer)
                or started_after(peer.proc_start, current.proc_start)
                or (seen_dead and seen is not None and _same(current, seen))
            ):
                self.registry.put(peer)
        # Outside the lock: the live view, with the user's name, runs ps.
        return self.registry.get(peer.id) or peer

    def claim(
        self, peer_id: str, seen: Peer, caller: Owner | None, change: Callable[[Peer], T]
    ) -> T | None:
        """Run change on the stored record and store it, under the lock, when the record is
        still the registration in `seen` and `caller` owns it. None otherwise, and for an
        unknown caller. change runs under the lock: it sets fields and claims messages only."""
        if caller is None:
            return None
        with self.ledger.locked():
            current = self.registry.read(peer_id)
            if current is None or not _same(current, seen) or owner(current) != caller:
                return None
            result = change(current)
            self.registry.put(current)
            return result

    def retire(self, peer_id: str, seen: Peer, *, keep_if_kept: bool = True) -> None:
        """End the registration in `seen`, under the lock: pause it while the Claude app keeps
        its session (and `keep_if_kept`), else remove the record and expire its inbox in the
        same step. A newer registration, of any process, stays as it is."""
        with self.ledger.locked():
            current = self.registry.read(peer_id)
            if current is None or not _same(current, seen):
                return
            if keep_if_kept and app_keeps(current):
                current.status = "paused"
                self.registry.put(current)
                return
            self.registry.remove(peer_id)
            self.expire_inbox(peer_id, locked=True)

    def prune(self) -> None:
        """Retire the registrations of exited processes, and expire the inboxes that no record
        has. Which processes run is read first, with ps; retire checks each one again."""
        records = self.registry.records()
        starts = proc_starts([peer.pid for peer in records])
        for peer in records:
            if peer.status == "paused":
                # A paused record ends when the app archives or deletes its session.
                if not app_keeps(peer):
                    self.retire(peer.id, peer, keep_if_kept=False)
            elif not is_alive(peer, starts):
                self.retire(peer.id, peer)
        with self.ledger.locked():
            for path in self.registry.dir.glob("*.json") if self.registry.dir.is_dir() else []:
                try:
                    json.loads(path.read_text())
                except ValueError:
                    path.unlink(missing_ok=True)  # a record that does not parse
                except OSError:
                    continue
            for peer_id in self.inbox.peer_ids():
                if self.registry.absent(peer_id):
                    self.expire_inbox(peer_id, locked=True)

    def whoami(
        self,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        host_pid: int | None = None,
        *,
        unregistered: str | None = None,
    ) -> Peer:
        """The live session of this process. A session with no live record is registered,
        unless `unregistered` is given: then whoami raises a PeerError with that text."""
        env = os.environ if env is None else env
        runtime, session_id = identity.session_from_env(env)
        existing = self.registry.get(f"{runtime}:{session_id}")
        if existing is not None and existing.status != "paused" and is_alive(existing):
            if existing.pid not in identity.ancestors(os.getpid()):
                raise PeerError(
                    f"The session {existing.name} is not an ancestor of this process. "
                    "Run peers from the shell of your own agent session."
                )
            return existing
        if unregistered is not None:
            raise PeerError(unregistered)
        peer = identity.detect(runtime, session_id, cwd or os.getcwd(), env, host_pid)
        dead = existing is not None and not is_alive(existing)
        adopted = self.adopt(peer, existing, seen_dead=dead)
        if owner(adopted) != owner(peer):
            raise PeerError(text.SUPERSEDED)
        return adopted

    def _enrich(self, peer: Peer) -> Peer:
        """Use the runtime's own status unless a peermesh hook recorded a newer one."""
        if peer.status == "paused":
            # Its process ended, so its pid can belong to another session now.
            return peer
        if peer.runtime == "claude":
            native = claude_native_state(peer.pid)
        else:
            native = codex_native_state(peer.transcript) if peer.transcript else None
        if native is not None and native[1] >= peer.last_seen:
            peer.status = native[0]
        return peer

    def all_peers(self) -> list[Peer]:
        """Every live peer, with the status that Claude Code reports when it is newer."""
        return [self._enrich(p) for p in self.registry.live()]

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
        msg_id = self.full_id(msg_id, to=me.id)
        original = self.ledger.find(msg_id)
        if original is None:
            raise PeerError(f"No message with id {msg_id!r} is in the ledger.")
        if original.get("to") != me.id:
            raise PeerError("That message was not sent to this session. Use peers_send.")
        sender_name = str(original.get("from_name", ""))
        sender_id = str(original["from"])
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
            hop=self._next_hop(original),
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

    def _next_hop(self, parent: dict[str, Any]) -> int:
        """The hop of a message that answers parent: one more if it is quick, else zero."""
        quick = self.clock() - float(parent.get("sent_at", 0.0)) < LOOP_GAP_S
        return int(parent.get("hop", 0)) + 1 if quick else 0

    def full_id(self, ref: str, *, to: str | None = None) -> str:
        """The message id that ref names. Frames show ids shortened to SHORT_ID characters.

        A prefix that matches no message comes back unchanged, so the caller reports the id as
        not found.
        """
        if not MIN_PREFIX <= len(ref) < FULL_ID:
            return ref
        matches = {
            str(e["id"]) for e in self.ledger.with_prefix(ref) if to is None or e.get("to") == to
        }
        if len(matches) > 1:
            raise PeerError(
                f"The id {ref!r} matches more than one message. Use more characters of the id."
            )
        return matches.pop() if matches else ref

    def _causal_parent(self, me: Peer, target: Peer) -> dict[str, Any] | None:
        """The newest message from target to me in CAUSAL_WINDOW_S, which a send answers."""
        now = self.clock()
        received = [
            e
            for e in self.ledger.latest()
            if e.get("from") == target.id
            and e.get("to") == me.id
            and e.get("outcome") in SENT_OUTCOMES
            and now - float(e.get("sent_at", 0.0)) < CAUSAL_WINDOW_S
        ]
        return max(received, key=lambda e: float(e.get("sent_at", 0.0)), default=None)

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
        contested = self.registry.claimants(target_ref)
        if contested:
            return (
                f"More than one live session has the name {target_ref!r}: "
                f"{', '.join(sorted(p.name for p in contested))}. "
                "Send the message to one of these names."
            )
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
        if in_reply_to is None:
            parent = self._causal_parent(me, target)
            if parent is not None:
                thread = str(parent.get("thread", parent["id"]))
                hop = self._next_hop(parent)
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
            # The rate limit applies to new threads. In a running thread, the hop limit stops
            # fast loops, and this cap stops one sender from flooding the thread.
            starts_thread = msg.thread == msg.id
            recent = self.ledger.count_recent(
                me.id, target.id, RATE_WINDOW_S, now, new_threads_only=True
            )
            in_thread = self.ledger.count_in_thread(me.id, msg.thread, RATE_WINDOW_S, now)
            if not starts_thread and in_thread >= THREAD_LIMIT:
                return self._refused(
                    me,
                    target.id,
                    target.name,
                    kind,
                    f"flood limit: you sent {THREAD_LIMIT} messages in this thread in 10 "
                    "minutes. Wait, or ask the user. " + NO_DETOUR,
                    locked=True,
                )
            if starts_thread and recent >= RATE_LIMIT:
                return self._refused(
                    me,
                    target.id,
                    target.name,
                    kind,
                    f"rate limit: {RATE_LIMIT} new threads to this peer in 10 minutes. "
                    "Reply in an open thread, wait, or ask the user. " + NO_DETOUR,
                    locked=True,
                )
            # The status can change between resolve() and the lock; a Stop hook changes it
            # under this lock, so the inbox decision sees the status that the hook wrote.
            # A raw read: the live view would run ps under the lock. The name stays the one that
            # resolve() gave.
            fresh = self.registry.read(target.id)
            if fresh is not None:
                if target.status == "paused" and _same(fresh, target):
                    # The live view saw this registration's process exit while the Claude app
                    # keeps the session; the stored status can still say idle.
                    fresh.status = "paused"
                fresh.name, fresh.auto_name = target.name, target.auto_name
                target = fresh
            target = self._enrich(target)
            to_inbox = _wants_inbox(target, msg)
            try:
                self.ledger.append(
                    msg.to_json()
                    | {
                        "sent_at": now,
                        "outcome": "pending" if to_inbox else "sending",
                        "note": _pending_note(target) if to_inbox else "",
                    }
                )
                if to_inbox:
                    # A hook renders the waiting messages itself. peermesh 0.2.1 and older
                    # read `rendered`, so the record keeps it for a session whose hooks run
                    # an older version after a rollback.
                    self.inbox.put(
                        target.id,
                        msg.to_json()
                        | {"rendered": render([msg], target.runtime, tools=target.has_tools)},
                    )
            except OSError:
                return SendResult(
                    None,
                    target.name,
                    "refused",
                    "The ledger or the inbox cannot be written. The message was not sent.",
                )
        if to_inbox:
            return SendResult(msg.id, target.name, "pending", _pending_note(target))
        return self._deliver_native(target, msg, after_completion=_can_reopen(target, msg))

    def _deliver_native(self, target: Peer, msg: Message, *, after_completion: bool) -> SendResult:
        rendered = render(
            [msg],
            target.runtime,
            short_ids=accepts_short_ids(target),
            ending="after_completion" if after_completion else None,
            tools=target.has_tools,
        )
        outcome = self.transports[target.runtime].deliver(target, rendered, msg)
        if outcome.offline:
            # The record ends only if it is still the registration that the send tried.
            self.retire(target.id, target)
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

    def hand_to_native(self, items: list[Waiting]) -> None:
        """Move claimed items from the inbox to the native path. The caller holds the lock.

        Each item is recorded as "sending" and leaves the inbox before any delivery starts, so
        a hook that stops during `wake` leaves an unfinished send, never a message to replay.
        """
        for item in items:
            self.ledger.append(
                {"id": item.id, "outcome": "sending", "note": "native wake", "at": self.clock()}
            )
        self.inbox.done(items)

    def wake(self, peer: Peer, items: list[Waiting]) -> None:
        """Deliver items that `hand_to_native` took, which starts a new turn of peer.

        Call this without the ledger lock: each delivery records its own outcome.
        """
        for item in items:
            self._deliver_native(peer, Message.from_json(item.record), after_completion=True)

    def expire_inbox(self, peer_id: str, *, locked: bool = False) -> None:
        """Mark the waiting messages of an ended session as undelivered, and remove its inbox.
        `locked` is True when the caller holds the ledger lock."""
        if not locked:
            with self.ledger.locked():
                self.expire_inbox(peer_id, locked=True)
            return
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
        msg_id = self.full_id(msg_id)
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


def accepts_short_ids(receiver: Peer) -> bool:
    """True when the receiver can reply with the short id of a message.

    A Codex agent starts `peers` for each reply, so it runs the installed version. A Claude
    Code session keeps one MCP server process from its start, which can be an older version
    that knows only full ids. A server that resolves short ids writes the state file.
    """
    return receiver.runtime == "codex" or paths.state_file(receiver.id, SHORT_IDS).exists()


def announce_short_ids(peer: Peer) -> None:
    """Record that the process of peer resolves short ids.

    The fact has its own file: a write of the registry record from a long-lived process can
    replace a status that a hook wrote a moment before.
    """
    marker = paths.state_file(peer.id, SHORT_IDS)
    if not marker.exists():
        paths.ensure_private_dir(marker.parent)
        marker.touch(mode=0o600)


def _wants_inbox(target: Peer, msg: Message) -> bool:
    """True when a hook of the target, not the native inbox, delivers the message.

    A busy target gets the message at its next tool call. An idle target gets information at
    its next prompt, so information never opens a new turn. A request to an idle target starts
    a turn through the native path. An urgent request uses the native path, except on a busy
    Codex session that cannot be steered, where the next tool call is the fastest way in.
    """
    if target.status == "paused":
        return True
    if "inbox" not in target.capabilities:
        return False
    if msg.urgency == "now":
        return target.runtime == "codex" and target.host != "daemon" and target.status == "busy"
    return target.status == "busy" or msg.kind == "info"


def _same(record: Peer, seen: Peer) -> bool:
    """True when record is the registration that was read as seen: its generation and owner."""
    return record.generation == seen.generation and owner(record) == owner(seen)


def _pending_note(target: Peer) -> str:
    return PAUSED_NOTE if target.status == "paused" else PENDING_NOTE


def _can_reopen(target: Peer, msg: Message) -> bool:
    """True when a native delivery can start a turn after the target finished its task."""
    return target.status == "idle" and msg.kind != "info"
