from __future__ import annotations

import io
import json
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
    assert "(codex, id codex:ME)" in setup[1].calls[0]


def test_send_body_from_stdin(setup: tuple[Mesh, Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("multi\nline body\n"))
    assert cli.main(["send", "claude-p", "--body", "-"]) == 0
    assert "multi\nline body" in setup[1].calls[0]


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
    assert "codex-me -> claude-p" in capsys.readouterr().out
