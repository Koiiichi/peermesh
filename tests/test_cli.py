from __future__ import annotations

import io
import json
import time
from collections.abc import Callable

import pytest

from peermesh import cli
from peermesh.ledger import Ledger
from peermesh.registry import Peer, Registry
from peermesh.service import Mesh
from peermesh.transports import Outcome


class Fake:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def deliver(self, target: Peer, rendered: str, msg: object) -> Outcome:
        self.calls.append(rendered)
        return Outcome("delivered")


@pytest.fixture
def setup(make_peer: Callable[..., Peer], monkeypatch: pytest.MonkeyPatch) -> tuple[Mesh, Fake]:
    fake = Fake()
    mesh = Mesh(Registry(), Ledger(), {"claude": fake, "codex": fake})
    me = make_peer(id="codex:ME", name="codex-me", runtime="codex", endpoint="ME")
    mesh.registry.put(me)
    mesh.registry.put(make_peer(id="claude:P", name="claude-p"))
    monkeypatch.setattr(cli, "_mesh", lambda: mesh)
    monkeypatch.setenv("CODEX_THREAD_ID", "ME")
    return mesh, fake


def test_list_json(setup: tuple[Mesh, Fake], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--json", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["claude-p"]


def test_send_via_cli(setup: tuple[Mesh, Fake], capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--json", "send", "claude-p", "--body", "API v2 merged at 1a2b", "--kind", "info"]
    assert cli.main(args) == 0
    [res] = json.loads(capsys.readouterr().out)
    assert res["status"] == "delivered"
    assert "from codex-me (codex)" in setup[1].calls[0]


def test_send_body_from_stdin(setup: tuple[Mesh, Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("multi\nline body\n"))
    assert cli.main(["send", "claude-p", "--body", "-"]) == 0
    assert "> multi\n> line body" in setup[1].calls[0]


def test_refused_exit_code(setup: tuple[Mesh, Fake]) -> None:
    assert cli.main(["send", "nobody", "--body", "hi there"]) == 1


def test_outside_session_exit_code(
    setup: tuple[Mesh, Fake],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID")
    assert cli.main(["list"]) == 2
    assert "not inside" in capsys.readouterr().err


def test_log_shows_recent(setup: tuple[Mesh, Fake], capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["send", "claude-p", "--body", "first message"])
    capsys.readouterr()
    assert cli.main(["log", "--limit", "5"]) == 0
    assert "codex-me → claude-p" in capsys.readouterr().out


def test_status_unknown_exit_code(
    setup: tuple[Mesh, Fake], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["status", "nobody"]) == 1
    assert "claude-p" in capsys.readouterr().out


def test_log_shows_each_message_once(
    setup: tuple[Mesh, Fake], capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["send", "claude-p", "--body", "only once"])
    capsys.readouterr()
    cli.main(["log"])
    out = capsys.readouterr().out
    assert out.count("only once") == 1 and "  delivered  " in out


def test_styles_only_for_a_person_at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert cli._style("refused", "red") == "\033[31mrefused\033[0m"
    monkeypatch.setenv("CODEX_THREAD_ID", "T")
    assert cli._style("refused", "red") == "refused"
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli._style("refused", "red") == "refused"


def test_log_line_fits_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    entry = {
        "id": "0123456789abcdef" * 2,
        "ts": "2026-10-10T09:33:12+00:00",
        "from_name": "codex-me",
        "to_name": "claude-p",
        "kind": "request",
        "outcome": "injected",
        "body": "Please review\ncart.py " + "and more " * 20,
    }
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    line = cli._log_line(entry)
    assert line.startswith("10-10 02:33  codex-me → claude-p  request  injected  msg 01234567  ")
    assert len(line) == 100 and line.endswith("…") and "\n" not in line
