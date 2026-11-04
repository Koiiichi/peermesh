from __future__ import annotations

import json
import os
from pathlib import Path

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


def _status_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    status = tmp_path / "status"
    status.write_text(text)
    monkeypatch.setattr(identity, "PROC_STATUS", status)


def test_visible_pid_is_the_number_that_proc_shows_in_a_pid_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _status_file(tmp_path, monkeypatch, "Name:\tpeers\nNSpid:\t4242\t3\nVmSize:\t1 kB\n")
    assert identity.visible_pid() == 4242


@pytest.mark.parametrize("text", ["Name:\tpeers\n", "NSpid:\n", "NSpid:\tnot-a-number\n"])
def test_visible_pid_falls_back_to_getpid_without_a_clear_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    _status_file(tmp_path, monkeypatch, text)
    assert identity.visible_pid() == os.getpid()


def test_visible_pid_is_getpid_on_a_platform_without_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity, "PROC_STATUS", tmp_path / "absent")
    assert identity.visible_pid() == os.getpid()


def test_the_walk_finds_the_host_from_inside_a_pid_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sandbox numbers this process 2, and /proc shows host numbers, where 2 is another
    # process. Only the first NSpid value leads to the real parent chain.
    real = os.getpid()
    _status_file(tmp_path, monkeypatch, f"NSpid:\t{real}\t2\n")
    monkeypatch.setattr(identity.os, "getpid", lambda: 2)
    monkeypatch.setattr(identity, "_is_codex_command", lambda command: "python" in command)
    assert identity.find_host("codex", "T1") == (real, 0)
