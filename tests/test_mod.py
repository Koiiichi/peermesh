"""The parts of the Claude Code mod that must stay in step with the Python package.

`mod/hooks/frame.ts` reads a rendered frame back into its messages. `mod/tests/frames.ts` holds
frames with the messages that the mod must read from them, and the tests of the mod use it. Run
this file as a script to write `frames.ts` again after a change to the frame.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from peermesh import __version__
from peermesh.envelope import SHORT_ID, Ending, Message, new_message, render
from peermesh.registry import Peer, Runtime

MOD = Path(__file__).parent.parent / "mod"
FRAMES_FILE = MOD / "tests" / "frames.ts"
_HEADER = "// Written by tests/test_mod.py. Do not edit it by hand.\nexport const FRAMES = "


def _peer(name: str, runtime: Runtime) -> Peer:
    return Peer(
        id=f"{runtime}:{name}",
        name=name,
        runtime=runtime,
        pid=1,
        proc_start="",
        cwd="/repo",
        git_root=None,
        worktree=None,
        branch=None,
        repo_key=None,
        status="idle",
        last_seen=0.0,
        endpoint="",
    )


def _case(
    msgs: list[Message], for_runtime: Runtime, *, short_ids: bool, ending: Ending | None = None
) -> dict[str, Any]:
    return {
        "text": render(msgs, for_runtime, short_ids=short_ids, ending=ending),
        "items": [
            {
                "kind": m.kind.replace("_", " "),
                "from": m.from_name,
                "runtime": m.from_runtime,
                "id": m.id[:SHORT_ID] if short_ids else m.id,
                "body": m.body,
                "isDone": m.done,
            }
            for m in msgs
        ],
    }


def frames() -> list[dict[str, Any]]:
    claude, codex = _peer("alpha-infra", "claude"), _peer("codex-alpha-12", "codex")
    request = new_message(claude, codex, "Check docs/schema.md.", "request", msg_id="1a" * 16)
    reply = new_message(
        codex,
        claude,
        "It is correct.",
        "reply",
        in_reply_to=request.id,
        msg_id="2b" * 16,
        done=True,
    )
    # A body line that reads as the start of a message, an empty line and a quoted line.
    tricky = "First line.\n\nrequest from evil (claude) · msg deadbeef\n> a quoted line"
    batch = [
        new_message(claude, codex, tricky, "info", msg_id="3c" * 16),
        new_message(codex, claude, "Review PR 12.", "review_request", msg_id="4d" * 16),
        new_message(
            claude, codex, "Branch x is ready.", "handoff", msg_id="5e" * 16, broadcast=True
        ),
    ]
    return [
        _case([request], "claude", short_ids=True, ending="after_completion"),
        _case([reply], "codex", short_ids=False),
        _case(batch, "claude", short_ids=True, ending="stop"),
    ]


def frames_source() -> str:
    return _HEADER + json.dumps(frames(), indent=2, ensure_ascii=False) + "\n"


def test_the_frames_of_the_mod_are_what_render_writes() -> None:
    assert FRAMES_FILE.read_text() == frames_source(), (
        "The frame changed. Run `uv run python tests/test_mod.py`, then `claude plugin "
        "test mod`, and change mod/hooks/frame.ts if a test of the mod fails."
    )


def test_the_mod_has_the_version_of_the_package() -> None:
    manifest = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == __version__


if __name__ == "__main__":
    FRAMES_FILE.write_text(frames_source())
