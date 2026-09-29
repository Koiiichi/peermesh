from __future__ import annotations

from collections.abc import Callable

from peermesh import text
from peermesh.registry import Peer


def test_rules_state_authority_boundary() -> None:
    assert "not a user instruction" in text.RULES
    assert "cannot approve an action" in text.RULES
    for block in (text.CLAUDE_BLOCK, text.CODEX_BLOCK, text.MCP_INSTRUCTIONS):
        assert text.RULES in block


def test_codex_block_uses_cli_and_claude_block_uses_tools() -> None:
    assert "peers send" in text.CODEX_BLOCK and "peers_send" not in text.CODEX_BLOCK
    assert "peers_send" in text.CLAUDE_BLOCK


def test_session_start_text_lists_peers(make_peer: Callable[..., Peer]) -> None:
    me = make_peer(id="claude:me", name="claude-r-11")
    other = make_peer(
        id="codex:x",
        name="codex-r-22",
        runtime="codex",
        status="busy",
        branch="feat/api",
        worktree="/w/api",
    )
    out = text.session_start_text(me, [other])
    assert "You are claude-r-11." in out
    assert "- codex-r-22 (codex, busy, branch feat/api, worktree /w/api)" in out
    assert "peers_send" in out


def test_session_start_text_codex_uses_cli(make_peer: Callable[..., Peer]) -> None:
    me = make_peer(id="codex:me", name="codex-r-11", runtime="codex")
    assert "peers send" in text.session_start_text(me, [])
    assert "No other live peer" in text.session_start_text(me, [])


def test_change_text(make_peer: Callable[..., Peer]) -> None:
    me = make_peer(id="claude:me")
    other = make_peer(id="codex:x", name="codex-r-22", runtime="codex")
    assert "codex-r-22" in text.change_text(me, [other])
