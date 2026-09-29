"""Errors whose text an agent reads and acts on."""


class PeerError(Exception):
    """The operation cannot continue. The text tells the agent the next step."""
