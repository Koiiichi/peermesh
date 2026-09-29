"""Tripwires for the experimental and undocumented interfaces peermesh relies on."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from peermesh.install import MIN_CLAUDE, _version

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


def test_claude_session_record_fields() -> None:
    files = glob.glob(str(REAL_HOME / ".claude" / "sessions" / "*.json"))
    if not files:
        pytest.skip("no live Claude Code session")
    data = json.loads(Path(files[0]).read_text())
    assert {"pid", "sessionId", "messagingSocketPath", "status"} <= set(data)
