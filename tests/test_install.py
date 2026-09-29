from __future__ import annotations

import json
import subprocess
from typing import Any

from peermesh import install, paths


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
    assert set(twice["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
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
    actions = install.install({"claude", "codex"}, dry_run=False, runner=runner)
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
    actions = install.install({"claude", "codex"}, dry_run=True, runner=Runner())
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
    install.install({"claude"}, dry_run=False, runner=Runner())
    assert "→ ✓" in settings.read_text()
    install.uninstall({"claude"}, runner=Runner())
    assert settings.read_text() == '{\n  "statusLine": "→ ✓"\n}\n'
