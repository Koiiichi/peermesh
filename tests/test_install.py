from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from peermesh import install, paths


def _which(name: str) -> str | None:
    return f"/usr/local/bin/{name}"


class Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "2.1.285 (Claude Code)\n", "")


def test_merge_hooks_keeps_foreign_and_is_idempotent() -> None:
    cfg: dict[str, Any] = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
        "model": "x",
    }
    once = install.merge_hooks(json.loads(json.dumps(cfg)), "/bin/peers", "claude")
    twice = install.merge_hooks(json.loads(json.dumps(once)), "/bin/peers", "claude")
    assert once == twice
    stop = twice["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == "say done"
    assert stop[1]["hooks"][0]["command"] == "/bin/peers hook stop --runtime claude # peermesh"
    assert set(twice["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
    [post] = twice["hooks"]["PostToolUse"]
    assert "hook post-tool" in post["hooks"][0]["command"]
    stripped = install.strip_hooks(twice)
    assert stripped == cfg


def test_blocks_upsert_and_remove() -> None:
    original = "# Mine\n\nKeep this.\n"
    once = install.upsert_block(original, "BLOCK A")
    twice = install.upsert_block(once, "BLOCK B")
    assert twice.count(install.BEGIN) == 1 and "BLOCK B" in twice and "BLOCK A" not in twice
    assert install.remove_block(twice) == original


def test_install_and_uninstall_roundtrip() -> None:
    claude_md = paths.claude_dir() / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# user rules\n")
    runner = Runner()
    actions = install.install({"claude", "codex"}, dry_run=False, runner=runner, which=_which)
    assert actions
    settings = json.loads((paths.claude_dir() / "settings.json").read_text())
    assert "SessionStart" in settings["hooks"]
    codex_hooks = json.loads((paths.codex_home() / "hooks.json").read_text())
    assert set(codex_hooks) == {"hooks"}
    assert "--runtime codex" in codex_hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert install.BEGIN in claude_md.read_text()
    assert install.BEGIN in (paths.codex_home() / "AGENTS.md").read_text()
    rule = (paths.codex_home() / "rules" / "peermesh.rules").read_text()
    for sub in ("list", "send", "reply", "status", "whoami"):
        assert f'prefix_rule(pattern=["peers", "{sub}"], decision="allow")' in rule
    for sub in ("install", "uninstall", "log", "doctor", "hook"):
        assert f'"{sub}"' not in rule
    assert 'pattern=["peers"]' not in rule
    assert any(c[:4] == ["claude", "mcp", "add", "--scope"] for c in runner.calls)
    assert (claude_md.with_name("CLAUDE.md.peermesh.bak")).read_text() == "# user rules\n"

    install.uninstall({"claude", "codex"}, runner=runner)
    assert claude_md.read_text() == "# user rules\n"
    assert json.loads((paths.claude_dir() / "settings.json").read_text()) == {}
    assert not (paths.codex_home() / "rules" / "peermesh.rules").exists()
    assert any(c[:3] == ["claude", "mcp", "remove"] for c in runner.calls)


def test_dry_run_writes_nothing() -> None:
    actions = install.install({"claude", "codex"}, dry_run=True, runner=Runner(), which=_which)
    assert actions and not paths.claude_dir().exists() and not paths.codex_home().exists()


def test_doctor_reports_each_check() -> None:
    checks = install.doctor(runner=Runner())
    names = [c[0] for c in checks]
    assert "claude version" in names and "codex hooks" in names
    assert all(isinstance(ok, bool) for _, ok, _ in checks)


def test_install_keeps_non_ascii_text() -> None:
    settings = paths.claude_dir() / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"statusLine": "→ ✓"}, indent=2, ensure_ascii=False) + "\n")
    install.install({"claude"}, dry_run=False, runner=Runner(), which=_which)
    assert "→ ✓" in settings.read_text()
    install.uninstall({"claude"}, runner=Runner())
    assert settings.read_text() == '{\n  "statusLine": "→ ✓"\n}\n'


def test_strip_keeps_user_hook_in_mixed_group() -> None:
    ours = "/bin/peers hook stop --runtime claude # peermesh"
    cfg: dict[str, Any] = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "say done"},
                        {"type": "command", "command": ours},
                    ]
                }
            ]
        }
    }
    assert install.strip_hooks(cfg) == {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}
    }


def test_malformed_codex_hooks_stops_before_any_write() -> None:
    hooks = paths.codex_home() / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{not json")
    with pytest.raises(install.InstallError, match="hooks.json"):
        install.install({"claude", "codex"}, dry_run=False, runner=Runner(), which=_which)
    assert not (paths.claude_dir() / "settings.json").exists()


def test_missing_claude_binary_stops_before_any_write() -> None:
    runner = Runner()
    with pytest.raises(install.InstallError, match="claude"):
        install.install({"claude", "codex"}, dry_run=False, runner=runner, which=lambda n: None)
    assert not paths.claude_dir().exists() and not paths.codex_home().exists()
    assert runner.calls == []


def test_orphan_begin_marker_stops_install() -> None:
    md = paths.claude_dir() / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text(f"# mine\n{install.BEGIN}\nhalf a block\n")
    with pytest.raises(install.InstallError, match="CLAUDE.md"):
        install.install({"claude"}, dry_run=False, runner=Runner(), which=_which)
    assert md.read_text() == f"# mine\n{install.BEGIN}\nhalf a block\n"


def test_run_reports_install_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    def fail(*args: object, **kwargs: object) -> list[str]:
        raise install.InstallError("reason here")

    monkeypatch.setattr(install, "install", fail)
    ns = argparse.Namespace(cmd="install", runtime="both", dry_run=False)
    assert install.run(ns) == 1
    assert "reason here" in capsys.readouterr().err


def test_out_of_order_markers_stop_install() -> None:
    md = paths.claude_dir() / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    content = f"# mine\n{install.END}\nuser text\n{install.BEGIN}\n"
    md.write_text(content)
    with pytest.raises(install.InstallError, match="CLAUDE.md"):
        install.install({"claude"}, dry_run=False, runner=Runner(), which=_which)
    assert md.read_text() == content


def test_failed_mcp_add_changes_no_file() -> None:
    class FailAdd(Runner):
        def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(cmd)
            code = 1 if cmd[:3] == ["claude", "mcp", "add"] else 0
            return subprocess.CompletedProcess(cmd, code, "", "boom")

    with pytest.raises(install.InstallError, match="mcp add"):
        install.install({"claude", "codex"}, dry_run=False, runner=FailAdd(), which=_which)
    assert not (paths.claude_dir() / "settings.json").exists()
    assert not paths.codex_home().exists()


def test_uninstall_claude_ignores_malformed_codex_file() -> None:
    hooks = paths.codex_home() / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{not json")
    install.uninstall({"claude"}, runner=Runner())


def test_doctor_gives_the_install_hint_only_for_a_missing_post_tool_hook() -> None:
    settings = paths.claude_dir() / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(install.merge_hooks({}, "/bin/peers", "claude")))
    details = {name: (ok, detail) for name, ok, detail in install.doctor(runner=Runner())}
    assert details["claude post-tool hook"] == (True, "installed")
    assert details["codex post-tool hook"] == (False, "run `peers install` again")


def test_session_end_timeout_fits_the_codex_cap() -> None:
    config = install.merge_hooks({}, "/bin/peers", "codex")
    [end] = config["hooks"]["SessionEnd"]
    assert end["hooks"][0]["timeout"] == 3


def _codex_config(text: str) -> None:
    config = paths.codex_home() / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)


def _trust(events: list[str]) -> str:
    return "".join(
        f'[hooks.state."peermesh@peermesh:plugin.json#hooks[0]:{event}:0:0"]\n'
        'trusted_hash = "sha256:0"\n'
        for event in events
    )


EVENTS = ["session_start", "user_prompt_submit", "stop", "session_end", "post_tool_use"]


def _doctor() -> dict[str, tuple[bool, str]]:
    return {name: (ok, detail) for name, ok, detail in install.doctor(runner=Runner())}


def test_doctor_accepts_the_codex_plugin_in_place_of_peers_install() -> None:
    _codex_config('[plugins."peermesh@peermesh"]\nenabled = true\n\n' + _trust(EVENTS))
    details = _doctor()
    assert details["codex hooks"] == (True, "plugin peermesh@peermesh, 5 of 5 hooks trusted")
    assert details["codex post-tool hook"] == (True, "installed")
    assert details["codex rule"][0] is True


def test_doctor_names_untrusted_plugin_hooks() -> None:
    _codex_config('[plugins."peermesh@peermesh"]\nenabled = true\n\n' + _trust(EVENTS[:2]))
    ok, detail = _doctor()["codex hooks"]
    assert not ok and detail.startswith("plugin peermesh@peermesh, 2 of 5 hooks trusted (trust")


def test_doctor_ignores_a_disabled_plugin_and_an_empty_hook_file() -> None:
    _codex_config('[plugins."peermesh@peermesh"]\nenabled = false\n')
    (paths.codex_home() / "hooks.json").write_text('{"hooks": {}}')
    details = _doctor()
    assert details["codex hooks"][0] is False
    assert details["codex post-tool hook"] == (False, "run `peers install` again")
    assert details["codex rule"][0] is False


def test_doctor_runs_with_a_config_that_it_cannot_read() -> None:
    _codex_config("[plugins\nnot toml")
    assert _doctor()["codex hooks"][0] is False
    _codex_config('hooks = "x"\n[plugins."peermesh@peermesh"]\nenabled = true\n')
    assert _doctor()["codex hooks"] == (
        False,
        "plugin peermesh@peermesh, 0 of 5 hooks trusted "
        "(trust them in the hook review of a new Codex session)",
    )
