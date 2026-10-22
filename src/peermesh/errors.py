"""Errors whose text an agent reads and acts on."""


class PeerError(Exception):
    """The operation cannot continue. The text tells the agent the next step."""


class NoSessionError(PeerError):
    """No Claude Code or Codex session runs this process."""


class NoInboxError(PeerError):
    """The Claude Code session has no inbox socket yet, so it cannot receive messages."""


class ProcessCheckError(PeerError):
    """`ps` gave no trusted answer about which processes run: that is not a sign that a
    process exited."""


class UnreadableRecord(PeerError):
    """A registry record exists but cannot be read or parsed: that is not absence."""
