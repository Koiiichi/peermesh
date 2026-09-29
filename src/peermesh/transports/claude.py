"""Claude Code inbox socket. Documented: code.claude.com/docs/en/cross-session-messaging."""

from __future__ import annotations

import json
import socket

from peermesh.envelope import Message
from peermesh.registry import Peer
from peermesh.transports import Outcome

CLAUDE_HOLD_NOTE = "The receiver inbound policy can hold the message."


class ClaudeSocketTransport:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def deliver(self, target: Peer, rendered: str, msg: Message) -> Outcome:
        frame = {
            "type": "user",
            "message": {"role": "user", "content": rendered},
            "priority": "now" if msg.urgency == "now" else "next",
            "msg_id": msg.id,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(target.endpoint)
                sock.sendall((json.dumps(frame) + "\n").encode())
        except (FileNotFoundError, ConnectionRefusedError):
            return Outcome(
                "refused", "peer offline: the inbox socket does not accept connections.", True
            )
        except OSError as exc:
            return Outcome("refused", f"The socket write failed: {exc}. Do not resend.")
        return Outcome("delivered", CLAUDE_HOLD_NOTE)
