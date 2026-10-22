"""Tripwires for the experimental and undocumented interfaces peermesh relies on."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from peermesh.install import MIN_CLAUDE, _version
from peermesh.registry import claude_app_session, codex_native_state

pytestmark = pytest.mark.contract
REAL_HOME = Path(os.path.expanduser("~"))


def _defs(schema_dir: Path) -> dict[str, Any]:
    defs: dict[str, Any] = {}
    for file in schema_dir.rglob("*.json"):
        data = json.loads(file.read_text())
        defs.update(data.get("definitions", {}))
        defs.update(data.get("$defs", {}))
        if "title" in data:
            defs.setdefault(data["title"], data)
    return defs


@pytest.fixture(scope="module")
def codex_schema(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    exe = shutil.which("codex")
    if exe is None:
        pytest.skip("codex is not installed")
    out = tmp_path_factory.mktemp("schema")
    subprocess.run(
        [exe, "app-server", "generate-json-schema", "--out", str(out), "--experimental"],
        check=True,
        capture_output=True,
    )
    return _defs(out)


def test_turn_steer_shape(codex_schema: dict[str, Any]) -> None:
    steer = codex_schema["TurnSteerParams"]
    assert set(steer["required"]) == {"threadId", "expectedTurnId", "input"}


def test_text_user_input_shape(codex_schema: dict[str, Any]) -> None:
    variants = codex_schema["UserInput"]["oneOf"]
    text = next(v for v in variants if v.get("title") == "TextUserInput")
    assert set(text["required"]) == {"text", "type"}


def test_thread_read_and_turn_status(codex_schema: dict[str, Any]) -> None:
    assert "threadId" in codex_schema["ThreadReadParams"]["required"]
    assert "inProgress" in codex_schema["TurnStatus"]["enum"]


def test_queue_add_exists(codex_schema: dict[str, Any]) -> None:
    assert any("QueueAdd" in name for name in codex_schema), "thread/queue/add params missing"


def test_codex_queue_cli_flags() -> None:
    exe = shutil.which("codex")
    if exe is None:
        pytest.skip("codex is not installed")
    result = subprocess.run([exe, "queue", "--help"], capture_output=True, text=True, check=True)
    assert "--thread" in result.stdout and "--message" in result.stdout


def test_claude_version_floor() -> None:
    exe = shutil.which("claude")
    if exe is None:
        pytest.skip("claude is not installed")
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True).stdout
    version = _version(out)
    assert version is not None and version >= MIN_CLAUDE


def _session_record(path: Path) -> dict[str, Any] | None:
    """The record in path, or None when its session ended while the test read it.

    A record that stays unreadable fails the test, because a new format must not pass as a
    session that ended.
    """
    for _ in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            time.sleep(0.1)  # the session can be writing the file
            continue
        assert isinstance(data, dict), f"{path} does not hold a JSON object"
        return data
    pytest.fail(f"{path} does not hold valid JSON")


def test_claude_session_record_fields() -> None:
    records = []
    for name in glob.glob(str(REAL_HOME / ".claude" / "sessions" / "*.json")):
        record = _session_record(Path(name))
        if record is not None:
            records.append(record)
    if not records:
        pytest.skip("no live Claude Code session")
    assert all({"pid", "sessionId", "status"} <= set(r) for r in records)
    # Some live sessions have no inbox socket, so one record with every field shows the format.
    assert any("messagingSocketPath" in r for r in records)


def test_codex_rollout_turn_events() -> None:
    rollouts = sorted(
        glob.glob(str(REAL_HOME / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")),
        key=os.path.getmtime,
    )
    if not rollouts:
        pytest.skip("no Codex rollout")
    # The newest rollout with a finished top-level turn shows the fields that peermesh reads.
    for rollout in reversed(rollouts[-20:]):
        state = codex_native_state(rollout)
        if state is not None:
            assert state[0] in ("busy", "idle") and state[1] > 0
            return
    pytest.fail("no recent Codex rollout has a top-level task_started or task_complete event")


def test_claude_app_session_record_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEERMESH_CLAUDE_APP", raising=False)
    hosts = []
    for name in glob.glob(str(REAL_HOME / ".claude" / "sessions" / "*.json")):
        try:
            data = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("hostSessionId"), str):
            hosts.append((data["hostSessionId"], data.get("sessionId")))
    if not hosts:
        pytest.skip("no live Claude desktop session")
    # The app keeps a record of each of its sessions, which peermesh reads when the app pauses
    # one. A child Claude process inherits a hostSessionId, so not every live record matches.
    matching = []
    for host, session in hosts:
        record = claude_app_session(host)
        if record is not None and record.get("cliSessionId") == session:
            matching.append(record)
    assert matching, "no Claude app record names a live desktop session"
    for record in matching:
        assert isinstance(record.get("isArchived"), bool)
        assert isinstance(record.get("title"), str)
