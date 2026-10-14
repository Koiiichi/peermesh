from __future__ import annotations

import gzip
import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from rich.console import Console

from peermesh import cli, paths, views, watch
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry
from peermesh.service import Mesh
from peermesh.transports import Outcome


class Fake:
    def deliver(self, target: Peer, rendered: str, msg: object) -> Outcome:
        return Outcome("delivered")


@pytest.fixture
def mesh(make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch) -> Mesh:
    mesh = Mesh(Registry(), Ledger(), {"claude": Fake(), "codex": Fake()})
    mesh.registry.put(make_peer(id="claude:A", name="claude-a", repo_key="k"))
    mesh.registry.put(make_peer(id="codex:X", name="codex-x", runtime="codex", repo_key="k"))
    monkeypatch.setattr(cli, "_mesh", lambda: mesh)
    return mesh


def _exchange(mesh: Mesh) -> tuple[str, str]:
    a, x = mesh.registry.get("claude:A"), mesh.registry.get("codex:X")
    assert a is not None and x is not None
    [request] = mesh.send(a, "codex-x", "Please review cart.py", kind="request")
    reply = mesh.reply(x, str(request.msg_id), "One bug: rounding in line 12.", done=True)
    return str(request.msg_id), str(reply.msg_id)


def _text(renderable: Any, width: int) -> str:
    console = Console(file=io.StringIO(), width=width, record=True)
    console.print(renderable)
    return console.export_text()


def test_message_rows_give_the_last_state_of_each_message(mesh: Mesh) -> None:
    request, reply = _exchange(mesh)
    a = mesh.registry.get("claude:A")
    assert a is not None
    mesh.send(a, "nobody", "Is anyone there?")
    rows = views.message_rows(mesh.ledger.latest())
    assert [(r["id"], r["state"]) for r in rows[:2]] == [(request, "done"), (reply, "delivered")]
    assert rows[2]["state"] == "refused" and len(rows) == 3


def test_message_with_a_refused_reply_is_not_answered() -> None:
    entries = [
        {"id": "m1", "from": "a", "outcome": "injected"},
        {"id": "r1", "from": "b", "in_reply_to": "m1", "outcome": "refused"},
        {"id": "m2", "from": "a", "outcome": "pending"},
        {"id": "r2", "from": "b", "in_reply_to": "m2", "outcome": "delivered"},
    ]
    assert [r["state"] for r in views.message_rows(entries)] == [
        "injected",
        "refused",
        "answered",
        "delivered",
    ]


@pytest.mark.parametrize("width", [60, 120])
def test_frame_shows_sessions_and_messages_at_any_width(mesh: Mesh, width: int) -> None:
    request, _ = _exchange(mesh)
    mesh.inbox.put("codex:X", {"id": "w" * 32, "kind": "info"})
    text = _text(watch.snapshot(mesh, width, 30), width)
    assert "claude-a" in text and "codex-x" in text and "done" in text
    assert (request[:8] in text) == (width >= watch.WIDE)
    assert "Please review" in text
    assert all(len(line) <= width for line in text.splitlines())


def test_frame_without_sessions_or_messages() -> None:
    text = _text(watch.frame([], {}, [], 80), 80)
    assert "No live session." in text and "No message yet." in text


def test_snapshot_keeps_the_newest_messages_that_fit(mesh: Mesh) -> None:
    a = mesh.registry.get("claude:A")
    assert a is not None
    for number in range(6):
        mesh.send(a, "codex-x", f"fact number {number}", kind="handoff")
    text = _text(watch.snapshot(mesh, 120, 11), 120)
    assert "fact number 2" in text and "fact number 1" not in text


def test_watch_updates_until_ctrl_c(mesh: Mesh) -> None:
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 3:
            raise KeyboardInterrupt

    console = Console(file=io.StringIO(), width=100, height=20, force_terminal=True)
    watch.watch(mesh, console, interval=0.5, once=False, sleep=sleep)
    assert calls == [0.5, 0.5, 0.5]


def test_thread_lines_show_each_message_with_its_body(mesh: Mesh) -> None:
    request, reply = _exchange(mesh)
    lines = views.thread_lines(mesh.ledger.thread(request), 60, lambda text, color: text)
    assert lines[0] == f"thread {request[:8]} · claude-a ↔ codex-x · 2 messages"
    assert lines[2].endswith(f"claude-a → codex-x  request  done  msg {request[:8]}")
    assert lines[3] == "  Please review cart.py"
    assert lines[5].endswith(f"codex-x → claude-a  reply  delivered  msg {reply[:8]}")
    wide = views.thread_lines([{"id": "m", "from": "a", "body": "word " * 40}], 40, lambda t, c: t)
    assert all(len(line) <= 40 for line in wide)


def test_ledger_thread_reads_archived_messages(mesh: Mesh) -> None:
    request, reply = _exchange(mesh)
    led = mesh.ledger
    paths.ensure_private_dir(led.archive_dir)
    lines = led.path.read_text().splitlines()
    old = [line for line in lines if json.loads(line)["id"] == request]
    with gzip.open(led.archive_dir / "ledger-00000000000000000001.jsonl.gz", "wt") as fh:
        fh.write("".join(line + "\n" for line in old))
    led.path.write_text("".join(line + "\n" for line in lines if line not in old))
    assert [r["id"] for r in led.thread(request)] == [request, reply]


def test_thread_command_accepts_a_short_id(mesh: Mesh, capsys: pytest.CaptureFixture[str]) -> None:
    request, _ = _exchange(mesh)
    assert cli.main(["thread", request[:8]]) == 0
    assert "One bug: rounding in line 12." in capsys.readouterr().out
    assert cli.main(["thread", "0" * 32]) == 2


def test_statusline_counts_waiting_and_sent_messages_by_state(mesh: Mesh) -> None:
    assert views.statusline(mesh, {"session_id": "A"}) == "no peer messages"
    assert views.statusline(mesh, {"session_id": "unknown"}) == ""
    a, x = mesh.registry.get("claude:A"), mesh.registry.get("codex:X")
    assert a is not None and x is not None
    mesh.inbox.put("claude:A", {"id": "w" * 32, "kind": "info"})
    assert views.statusline(mesh, {"session_id": "A"}) == "in 1 waiting"
    request, _ = _exchange(mesh)
    mesh.send(a, "codex-x", "Schema v2 is live.", kind="handoff")
    x.status, x.capabilities = "busy", ["inbox"]
    mesh.registry.put(x)
    mesh.send(a, "codex-x", "Please rebase.", kind="request")
    mesh.send(a, "nobody", "Is anyone there?")
    assert views.statusline(mesh, {"session_id": "A"}) == (
        "in 1 waiting · out 1 pending, 1 delivered, 1 answered, 1 failed"
    )
    # The reply that codex-x sent counts for codex-x, not for this session.
    assert request in {r["id"] for r in views.message_rows(mesh.ledger.latest())}


def test_statusline_drops_messages_older_than_the_window(mesh: Mesh) -> None:
    a = mesh.registry.get("claude:A")
    assert a is not None
    mesh.send(a, "codex-x", "Schema v2 is live.", kind="handoff")
    assert views.statusline(mesh, {"session_id": "A"}) == "out 1 delivered"
    start = mesh.clock()
    mesh.clock = lambda: start + views.STATUSLINE_WINDOW_S + 1
    assert views.statusline(mesh, {"session_id": "A"}) == "no peer messages"


def test_statusline_command_never_fails(
    mesh: Mesh, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["statusline"]) == 0 and capsys.readouterr().out == ""
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "A"}'))
    assert cli.main(["statusline"]) == 0
    assert capsys.readouterr().out == "no peer messages\n"


def test_watch_once_prints_plain_text_for_an_agent(
    mesh: Mesh, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "X")
    assert cli.main(["watch"]) == 0
    out = capsys.readouterr().out
    assert "claude-a" in out and "\033[" not in out


def test_snapshot_has_the_sessions_and_the_messages_with_their_states(mesh: Mesh) -> None:
    request, reply = _exchange(mesh)
    a = mesh.registry.get("claude:A")
    data = views.snapshot(mesh, a)
    assert data["me"] == "claude:A"
    assert [(p["name"], p["waiting"]) for p in data["peers"]] == [("claude-a", 0), ("codex-x", 0)]
    assert [(m["id"], m["state"]) for m in data["messages"]] == [
        (request, "done"),
        (reply, "delivered"),
    ]
    outside = views.snapshot(mesh, None)
    assert outside["me"] is None and len(outside["messages"]) == 2


def test_watch_prints_the_snapshot_as_json(
    mesh: Mesh, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _exchange(mesh)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert cli.main(["--json", "watch"]) == 0
    data = json.loads(capsys.readouterr().out)
    # Outside a session there is no "me". The sessions and the messages are the same.
    assert data["me"] is None and len(data["peers"]) == 2 and len(data["messages"]) == 2


def test_frame_prints_markup_characters_as_written(mesh: Mesh) -> None:
    a = mesh.registry.get("claude:A")
    assert a is not None
    a.branch = "fix/[bold]"
    mesh.registry.put(a)
    mesh.send(a, "codex-x", "Review the closing tag [/section] and [red]this[/red].")
    text = _text(watch.snapshot(mesh, 140, 30), 140)
    assert "Review the closing tag [/section] and [red]this[/red]." in text
    assert "fix/[bold]" in text


def test_views_show_control_sequences_as_text(mesh: Mesh) -> None:
    a = mesh.registry.get("claude:A")
    assert a is not None
    a.branch = "fix\x1b[2J"
    mesh.registry.put(a)
    [sent] = mesh.send(
        a, "codex-x", "copy \x1b]52;c;ZXZpbA==\x07 now\nclear \x1b[2J", kind="handoff"
    )
    console = Console(file=io.StringIO(), width=160, force_terminal=True, color_system=None)
    console.print(watch.snapshot(mesh, 160, 30))
    shown = console.file.getvalue()  # type: ignore[attr-defined]
    assert "\x1b]52" not in shown and "\x07" not in shown and "\x1b[2J" not in shown
    assert "copy \\x1b]52;c;ZXZpbA==\\x07 now" in shown and "fix\\x1b[2J" in shown
    lines = views.thread_lines(mesh.ledger.thread(str(sent.msg_id)), 100, lambda t, c: t)
    assert "  copy \\x1b]52;c;ZXZpbA==\\x07 now" in lines and "  clear \\x1b[2J" in lines


def test_ledger_thread_orders_by_send_time_across_files(mesh: Mesh) -> None:
    led = mesh.ledger
    older = {"id": "m1", "from": "a", "thread": "m1", "sent_at": 1.0, "outcome": "pending"}
    newer = {"id": "m2", "from": "b", "thread": "m1", "sent_at": 2.0, "outcome": "delivered"}
    paths.ensure_private_dir(led.archive_dir)
    with gzip.open(led.archive_dir / "ledger-00000000000000000001.jsonl.gz", "wt") as fh:
        fh.write(json.dumps(newer) + "\n")
    led.path.write_text(json.dumps(older) + "\n")
    assert [r["id"] for r in led.thread("m1")] == ["m1", "m2"]


def test_snapshot_shows_the_newer_status_of_claude_code(mesh: Mesh) -> None:
    a = mesh.registry.get("claude:A")
    assert a is not None and a.status == "idle"
    sessions = paths.claude_dir() / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{a.pid}.json").write_text(json.dumps({"status": "busy"}))
    rows = [
        line
        for line in _text(watch.snapshot(mesh, 120, 20), 120).splitlines()
        if "claude-a" in line
    ]
    assert "busy" in rows[0]


def test_watch_stays_live_without_colors(mesh: Mesh, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        watch, "watch", lambda mesh, console, *, interval, once: seen.update(once=once)
    )
    assert cli.main(["watch"]) == 0
    assert seen == {"once": False} and not cli._styled()
