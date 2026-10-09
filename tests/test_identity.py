from __future__ import annotations

import json
import os

import pytest

from peermesh import identity, paths
from peermesh.errors import PeerError


def test_ancestors_starts_with_self() -> None:
    chain = identity.ancestors(os.getpid())
    assert chain[0] == os.getpid()
    assert os.getppid() in chain


def test_find_host_claude_by_sessions_file() -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getppid()}.json").write_text(json.dumps({"sessionId": "S1"}))
    assert identity.find_host("claude", "S1") == (os.getppid(), 1)


def test_session_from_env_single() -> None:
    assert identity.session_from_env({"CODEX_THREAD_ID": "T"}) == ("codex", "T")
    assert identity.session_from_env({"CLAUDE_CODE_SESSION_ID": "C"}) == ("claude", "C")
    with pytest.raises(PeerError, match="not inside"):
        identity.session_from_env({})


def test_nested_runtime_prefers_nearest_host(monkeypatch: pytest.MonkeyPatch) -> None:
    depths = {"claude": (10, 4), "codex": (20, 1)}
    monkeypatch.setattr(identity, "find_host", lambda rt, sid, start=None: depths[rt])
    env = {"CLAUDE_CODE_SESSION_ID": "C", "CODEX_THREAD_ID": "T"}
    assert identity.session_from_env(env) == ("codex", "T")
    depths["claude"] = (10, 0)
    assert identity.session_from_env(env) == ("claude", "C")


def test_detect_claude_requires_socket() -> None:
    with pytest.raises(PeerError, match="inbox socket"):
        identity.detect("claude", "C", os.getcwd(), {}, host_pid=os.getpid())


def test_detect_codex() -> None:
    peer = identity.detect("codex", "T", os.getcwd(), {}, host_pid=os.getpid())
    assert peer.id == "codex:T" and peer.endpoint == "T" and peer.pid == os.getpid()
    assert peer.status == "idle" and peer.host == "local"


def test_claude_session_follows_native_record_after_clear() -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getppid()}.json").write_text(json.dumps({"sessionId": "NEW"}))
    assert identity.session_from_env({"CLAUDE_CODE_SESSION_ID": "OLD"}) == ("claude", "NEW")


def test_detect_claude_prefers_own_session_record() -> None:
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"messagingSocketPath": "/tmp/own.sock"})
    )
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/inherited.sock"}
    peer = identity.detect("claude", "C", os.getcwd(), env, host_pid=os.getpid())
    assert peer.endpoint == "/tmp/own.sock"


def test_codex_app_server_without_daemon_is_not_steerable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "command_of", lambda pid: "/x/codex app-server --flag")
    peer = identity.detect("codex", "T", os.getcwd(), {}, host_pid=os.getpid())
    assert peer.host == "app-server" and "urgent_interrupt" not in peer.capabilities
