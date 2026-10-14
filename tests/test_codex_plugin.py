"""The Codex plugin in `codex/` must stay in step with the Python package.

The plugin runs the hooks that `peers install` writes for Codex, through its launcher. Run this
file as a script to write `codex/.codex-plugin/plugin.json` again after a change to the hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from peermesh import __version__, install

ROOT = Path(__file__).parent.parent
PLUGIN = ROOT / "codex"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
LAUNCHER = PLUGIN / "bin" / "peers"


def _manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MANIFEST.read_text())
    return data


def test_the_plugin_hooks_are_the_install_hooks_and_the_version_is_the_package_version() -> None:
    manifest = _manifest()
    assert manifest["version"] == __version__
    assert manifest["hooks"] == install.plugin_hooks(), "Run python tests/test_codex_plugin.py."


def test_the_plugin_hook_commands_do_not_hold_the_plugin_path() -> None:
    # Codex replaces ${PLUGIN_ROOT} in the command text, and the replaced path holds the
    # version. Codex then asks the user to trust the hooks again after each update.
    commands = [
        h["command"] for g in install.plugin_hooks()["hooks"].values() for h in g[0]["hooks"]
    ]
    assert all('"$PLUGIN_ROOT/bin/peers"' in c and "${PLUGIN_ROOT}" not in c for c in commands)
    assert all("--plugin" in c for c in commands if "post-tool" not in c)


def test_the_marketplace_and_the_server_point_to_the_plugin_files() -> None:
    market = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    [entry] = market["plugins"]
    assert entry["name"] == _manifest()["name"]
    assert (ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json") == MANIFEST
    servers = json.loads((PLUGIN / _manifest()["mcpServers"]).read_text())["mcpServers"]
    assert servers["peermesh"]["command"] == "./bin/peers"
    assert servers["peermesh"]["args"] == ["--mcp"]
    assert os.access(LAUNCHER, os.X_OK)


def _launch(tmp_path: Path, path: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(tmp_path), "PATH": path}
    return subprocess.run(
        ["/bin/sh", str(LAUNCHER), *args], capture_output=True, text=True, env=env, check=False
    )


def _fake(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    exe.write_text(f'#!/bin/sh\necho "{folder.name} {name} $*"\n')
    exe.chmod(0o755)


def test_the_launcher_runs_peers_from_path_then_from_the_uv_tool_folder(tmp_path: Path) -> None:
    _fake(tmp_path / ".local" / "bin", "peers")
    _fake(tmp_path / ".local" / "bin", "peers-mcp")
    _fake(tmp_path / "onpath", "peers")
    on_path = _launch(tmp_path, f"{tmp_path / 'onpath'}:/usr/bin:/bin", "hook", "stop")
    assert on_path.stdout == "onpath peers hook stop\n"
    fallback = _launch(tmp_path, "/usr/bin:/bin", "--mcp")
    assert fallback.stdout == "bin peers-mcp \n"


def test_the_launcher_names_the_missing_command(tmp_path: Path) -> None:
    result = _launch(tmp_path, "/usr/bin:/bin", "--mcp")
    assert result.returncode == 127
    assert "peers-mcp command is not installed" in result.stderr


if __name__ == "__main__":
    manifest = _manifest() | {"version": __version__, "hooks": install.plugin_hooks()}
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
