"""Delivery to one runtime's native inbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from peermesh.envelope import Message
from peermesh.registry import Peer

OutcomeStatus = Literal["delivered", "queued", "refused"]


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    note: str = ""
    offline: bool = False


class Transport(Protocol):
    def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome: ...
