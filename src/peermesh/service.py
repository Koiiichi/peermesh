"""The four operations: list, send, reply, status. Transports stay behind one interface."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from peermesh import identity
from peermesh.envelope import EnvelopeError, is_ack_only, new_message, render
from peermesh.errors import PeerError
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry, Scope, claude_native_status, is_alive, scoped
from peermesh.transports import Transport
from peermesh.transports.claude import ClaudeSocketTransport
from peermesh.transports.codex import CodexTransport

RATE_LIMIT = 6
RATE_WINDOW_S = 600.0


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
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.transports = transports
        self.clock = clock

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
        if peer.runtime == "claude":
            native = claude_native_status(peer.pid)
            if native is not None:
                peer.status = native
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

    def reply(self, me: Peer, msg_id: str, body: str) -> SendResult:
        original = self.ledger.find(msg_id)
        if original is None:
            raise PeerError(f"No message with id {msg_id!r} is in the ledger.")
        if original.get("to") != me.id:
            raise PeerError("That message was not sent to this session. Use peers_send.")
        sender_name = str(original.get("from_name", ""))
        if original.get("broadcast"):
            return SendResult(
                None,
                sender_name,
                "refused",
                "The message is a broadcast. Do not reply to a broadcast. "
                "Use peers_send if the sender must know something.",
            )
        if is_ack_only(body):
            return SendResult(
                None,
                sender_name,
                "refused",
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
        }

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
    ) -> SendResult:
        target = self.registry.resolve(target_ref)
        if target is None:
            return SendResult(None, target_ref, "refused", self._unknown(target_ref, me))
        if target.id == me.id:
            return SendResult(
                None,
                target.name,
                "refused",
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
            )
        except EnvelopeError as exc:
            return SendResult(None, target.name, "refused", str(exc))
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
                return SendResult(
                    None,
                    target.name,
                    "refused",
                    f"rate limit: {RATE_LIMIT} messages to this peer in 10 minutes. "
                    "Wait, or ask the user.",
                )
            try:
                self.ledger.append(
                    msg.to_json() | {"sent_at": now, "outcome": "sending", "note": ""}
                )
            except OSError:
                return SendResult(
                    None,
                    target.name,
                    "refused",
                    "The ledger cannot be written. The message was not sent.",
                )
        outcome = self.transports[target.runtime].deliver(target, render(msg, target.runtime), msg)
        if outcome.offline:
            self.registry.remove(target.id)
        entry = msg.to_json() | {"sent_at": now, "outcome": outcome.status, "note": outcome.note}
        try:
            with self.ledger.locked():
                self.ledger.append(entry)
        except OSError:
            return SendResult(
                msg.id, target.name, outcome.status, "submitted, not recorded, do not resend"
            )
        return SendResult(msg.id, target.name, outcome.status, outcome.note)
