"""Reversible install of hooks, MCP server, instruction blocks and the Codex rule."""

from __future__ import annotations

import argparse
import copy
import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from peermesh import paths, text
from peermesh.transports.codex import codex_bin

MARK = "# peermesh"
BEGIN = "<!-- peermesh:begin -->"
END = "<!-- peermesh:end -->"
HOOK_EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "SessionEnd": "session-end",
}
# PostToolUse runs on every tool call, so a shell check skips Python when the inbox of the
# session is empty. The session id comes from the payload: the environment of a hook can hold
# the id of a parent session. $1 is the peers executable and $2 the runtime.
POST_TOOL_GUARD = (
    "p=$(cat); "
    'id=$(printf "%s" "$p" | grep -o "\\"session_id\\": *\\"[^\\"]*\\"" | head -n 1 '
    '| sed "s/.*\\"\\([^\\"]*\\)\\"$/\\1/"); '
    'box="${PEERMESH_HOME:-$HOME/.peermesh}/inbox/${2}_${id}"; '
    'if [ -n "$id" ] && [ -z "$(ls -A "$box" 2>/dev/null)" ]; then exit 0; fi; '
    'printf "%s" "$p" | exec "$1" hook post-tool --runtime "$2"'
)
AGENT_SUBCOMMANDS = ("list", "send", "reply", "status", "whoami", "track")
CODEX_RULE = (
    "# Installed by peermesh. These `peers` subcommands must run outside the sandbox: they\n"
    "# write ~/.peermesh and connect to local inbox sockets. install, uninstall, log and\n"
    "# doctor are not listed, so they still need approval. Remove with `peers uninstall`.\n"
    + "".join(
        f'prefix_rule(pattern=["peers", "{sub}"], decision="allow")\n' for sub in AGENT_SUBCOMMANDS
    )
)
MIN_CLAUDE = (2, 1, 224)
# The launcher in the Codex plugin that runs the installed peers command.
PLUGIN_EXE = '"$PLUGIN_ROOT/bin/peers"'

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]


class InstallError(Exception):
    """Install or uninstall stopped before it changed a file. The text names the file or command."""


def _exe(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    return str(Path(sys.argv[0]).resolve().with_name(name))


def _is_ours(hook: Any) -> bool:
    return isinstance(hook, dict) and MARK in str(hook.get("command", ""))


def merge_hooks(config: dict[str, Any], exe: str, runtime: str) -> dict[str, Any]:
    config = strip_hooks(config)
    hooks = config.setdefault("hooks", {})
    for event, groups in _hook_groups(shlex.quote(exe), runtime).items():
        hooks.setdefault(event, []).extend(groups)
    return config


def plugin_hooks() -> dict[str, Any]:
    """The hooks of the Codex plugin: the Codex hooks of `peers install`, run through the
    launcher of the plugin. Codex sets $PLUGIN_ROOT for a plugin hook. The command text does
    not change when the plugin path changes, so an update does not ask for trust again."""
    return {"hooks": _hook_groups(PLUGIN_EXE, "codex", plugin=True)}


def _hook_groups(exe: str, runtime: str, *, plugin: bool = False) -> dict[str, Any]:
    """The hook groups for each event. `exe` is the peers executable as a shell word."""
    groups: dict[str, Any] = {}
    for event, name in HOOK_EVENTS.items():
        # --inbox tells the hook that the post-tool hook below is installed with it.
        flag = " --inbox" if name in ("session-start", "prompt") else ""
        flag += " --plugin" if plugin else ""
        command = f"{exe} hook {name} --runtime {runtime}{flag} {MARK}"
        # A Stop hook can wake its own session through `codex queue`, which has 30 seconds.
        # Codex caps SessionEnd at 3 seconds and warns at every start when a hook asks for more.
        timeout = {"Stop": 45, "SessionEnd": 3}.get(event, 10)
        groups[event] = [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
    groups["PostToolUse"] = [
        {"hooks": [{"type": "command", "command": _post_tool(exe, runtime, plugin), "timeout": 10}]}
    ]
    return groups


def post_tool_command(exe: str, runtime: str) -> str:
    return _post_tool(shlex.quote(exe), runtime)


def _post_tool(exe: str, runtime: str, plugin: bool = False) -> str:
    # The plugin has its own copy of the guard: a change to the guard of `peers install`
    # changes its command, and Codex then asks every user to trust the hook again.
    guard = (
        POST_TOOL_GUARD.replace('--runtime "$2"', '--runtime "$2" --plugin')
        if plugin
        else POST_TOOL_GUARD
    )
    return f"/bin/sh -c {shlex.quote(guard)} peermesh {exe} {runtime} {MARK}"


def _hook_config(runtime: str) -> dict[str, Any]:
    """The hook file of runtime that `peers install` writes, or {} when it is not valid."""
    if runtime == "claude":
        path = paths.claude_dir() / "settings.json"
    else:
        path = paths.codex_home() / "hooks.json"
    try:
        return _read_json(path)
    except (OSError, ValueError):
        return {}


def hooks_installed(runtime: str, event: str | None = None) -> bool:
    """True when the hook configuration of runtime holds hooks from `peers install`, for the
    hook event `event` (such as "SessionStart") when it is given."""
    return _has_ours(_hook_config(runtime), event)


def post_tool_installed(runtime: str) -> bool:
    """True when the hook configuration of runtime holds the peermesh post-tool hook."""
    return _has_ours(_hook_config(runtime), "PostToolUse", "hook post-tool")


def _has_ours(config: dict[str, Any], event: str | None = None, part: str = "") -> bool:
    """True when config has a peermesh hook, for `event` if given, with `part` in its command."""
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return False
    events = [event] if event is not None else list(hooks)
    return any(
        _is_ours(h) and part in str(h.get("command", ""))
        for name in events
        if isinstance(hooks.get(name), list)
        for g in hooks[name]
        if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        for h in g["hooks"]
    )


def strip_hooks(config: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(config)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return config
    for event in list(hooks):
        groups: list[Any] = []
        for group in hooks[event]:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                groups.append(group)
                continue
            kept = [h for h in entries if not _is_ours(h)]
            if kept or len(kept) == len(entries):
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        del config["hooks"]
    return config


def upsert_block(content: str, block: str) -> str:
    base = remove_block(content)
    sep = "" if not base or base.endswith("\n\n") else ("\n" if base.endswith("\n") else "\n\n")
    return f"{base}{sep}{BEGIN}\n{block.rstrip()}\n{END}\n"


def remove_block(content: str) -> str:
    pattern = re.compile(r"\n*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    stripped = pattern.sub("\n", content)
    return stripped.rstrip("\n") + "\n" if stripped.strip() else ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object.")
    return data


def _backup(path: Path) -> None:
    bak = path.with_name(path.name + ".peermesh.bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)


def _write(path: Path, content: str, dry_run: bool, actions: list[str]) -> None:
    actions.append(f"write {path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(content)


def _dump(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def _check_json(path: Path) -> None:
    try:
        _read_json(path)
    except ValueError as exc:
        raise InstallError(f"{path} is not a valid JSON object. Fix the file, then retry.") from exc


def _check_markers(path: Path) -> None:
    content = path.read_text() if path.exists() else ""
    begins, ends = content.count(BEGIN), content.count(END)
    misordered = begins == 1 and ends == 1 and content.index(BEGIN) > content.index(END)
    if begins != ends or begins > 1 or misordered:
        raise InstallError(
            f"{path} has an unmatched or repeated peermesh marker. "
            "Remove the peermesh block from the file by hand, then retry."
        )


def preflight(runtimes: set[str], which: Which = shutil.which) -> None:
    """Check every input before install writes anything."""
    if "claude" in runtimes:
        if which("claude") is None:
            raise InstallError("The claude executable is not on PATH. Install Claude Code first.")
        _check_json(paths.claude_dir() / "settings.json")
        _check_markers(paths.claude_dir() / "CLAUDE.md")
    if "codex" in runtimes:
        _check_json(paths.codex_home() / "hooks.json")
        _check_markers(paths.codex_home() / "AGENTS.md")


def install(
    runtimes: set[str],
    *,
    dry_run: bool,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> list[str]:
    preflight(runtimes, which)
    actions: list[str] = []
    peers = _exe("peers")
    if "claude" in runtimes:
        # Register the MCP server first: it is the step that can fail after preflight, and a
        # failure here leaves every file unchanged.
        mcp_cmd = ["claude", "mcp", "add", "--scope", "user", "peermesh", "--", _exe("peers-mcp")]
        actions.append("run " + " ".join(mcp_cmd))
        if not dry_run:
            runner(
                ["claude", "mcp", "remove", "--scope", "user", "peermesh"],
                capture_output=True,
                text=True,
                check=False,
            )
            result = runner(mcp_cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise InstallError(
                    f"claude mcp add failed: {(result.stderr or result.stdout).strip()[-300:]}. "
                    "No file was changed."
                )
        settings = paths.claude_dir() / "settings.json"
        merged = merge_hooks(_read_json(settings), peers, "claude")
        _write(settings, _dump(merged), dry_run, actions)
        md = paths.claude_dir() / "CLAUDE.md"
        current = md.read_text() if md.exists() else ""
        _write(md, upsert_block(current, text.CLAUDE_BLOCK), dry_run, actions)
    if "codex" in runtimes:
        hooks_file = paths.codex_home() / "hooks.json"
        merged = merge_hooks(_read_json(hooks_file), peers, "codex")
        _write(hooks_file, _dump(merged), dry_run, actions)
        agents = paths.codex_home() / "AGENTS.md"
        current = agents.read_text() if agents.exists() else ""
        _write(agents, upsert_block(current, text.CODEX_BLOCK), dry_run, actions)
        _write(paths.codex_home() / "rules" / "peermesh.rules", CODEX_RULE, dry_run, actions)
    return actions


def uninstall(runtimes: set[str], *, runner: Runner = subprocess.run) -> list[str]:
    if "claude" in runtimes:
        _check_json(paths.claude_dir() / "settings.json")
    if "codex" in runtimes:
        _check_json(paths.codex_home() / "hooks.json")
    actions: list[str] = []
    if "claude" in runtimes:
        settings = paths.claude_dir() / "settings.json"
        if settings.exists():
            _write(settings, _dump(strip_hooks(_read_json(settings))), False, actions)
        md = paths.claude_dir() / "CLAUDE.md"
        if md.exists():
            _write(md, remove_block(md.read_text()), False, actions)
        runner(
            ["claude", "mcp", "remove", "--scope", "user", "peermesh"],
            capture_output=True,
            text=True,
            check=False,
        )
        actions.append("run claude mcp remove --scope user peermesh")
    if "codex" in runtimes:
        hooks_file = paths.codex_home() / "hooks.json"
        if hooks_file.exists():
            _write(hooks_file, _dump(strip_hooks(_read_json(hooks_file))), False, actions)
        agents = paths.codex_home() / "AGENTS.md"
        if agents.exists():
            _write(agents, remove_block(agents.read_text()), False, actions)
        rule = paths.codex_home() / "rules" / "peermesh.rules"
        if rule.exists():
            rule.unlink()
            actions.append(f"delete {rule}")
    return actions


def _version(output: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    return tuple(int(g) for g in match.groups()) if match else None


def _run_text(runner: Runner, cmd: list[str]) -> tuple[int, str]:
    try:
        result = runner(cmd, capture_output=True, text=True, check=False)
    except OSError:
        return 127, ""
    return result.returncode, result.stdout


def codex_plugin() -> tuple[str, int] | None:
    """The id of the enabled peermesh plugin in the Codex config, such as "peermesh@peermesh",
    and the number of its hooks that have a trust record; None when no such plugin is enabled."""
    try:
        config = tomllib.loads((paths.codex_home() / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return None
    for plugin, entry in plugins.items():
        if plugin.partition("@")[0] == "peermesh" and isinstance(entry, dict):
            if entry.get("enabled") is True:
                hooks = config.get("hooks")
                state = hooks.get("state") if isinstance(hooks, dict) else None
                keys = state if isinstance(state, dict) else {}
                return plugin, sum(1 for key in keys if key.startswith(f"{plugin}:"))
    return None


def doctor(runner: Runner = subprocess.run) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    peers = shutil.which("peers")
    checks.append(("peers on PATH", peers is not None, peers or "not found"))
    _, out = _run_text(runner, ["claude", "--version"])
    version = _version(out)
    ok = version is not None and version >= MIN_CLAUDE
    checks.append(("claude version", ok, out.strip() or "not found"))
    exe = codex_bin()
    checks.append(("codex binary", exe is not None, exe or "not found"))
    settings_path = paths.claude_dir() / "settings.json"
    checks.append(("claude hooks", hooks_installed("claude"), str(settings_path)))
    plugin = codex_plugin()
    if hooks_installed("codex") or plugin is None:
        codex_hooks = paths.codex_home() / "hooks.json"
        detail = f"{codex_hooks} (approve hook trust in Codex once)"
        checks.append(("codex hooks", hooks_installed("codex"), detail))
    else:
        name, trusted = plugin
        expected = len(plugin_hooks()["hooks"])
        detail = f"plugin {name}, {trusted} of {expected} hooks trusted"
        if trusted < expected:
            detail += " (trust them in the hook review of a new Codex session)"
        checks.append(("codex hooks", trusted >= expected, detail))
    for runtime in ("claude", "codex"):
        ok = post_tool_installed(runtime) or (runtime == "codex" and plugin is not None)
        detail = "installed" if ok else "run `peers install` again"
        checks.append((f"{runtime} post-tool hook", ok, detail))
    rule = paths.codex_home() / "rules" / "peermesh.rules"
    if rule.exists() or plugin is None:
        checks.append(("codex rule", rule.exists(), str(rule)))
    else:
        checks.append(("codex rule", True, "not needed: the plugin gives Codex the MCP tools"))
    code, _ = _run_text(runner, ["claude", "mcp", "get", "peermesh"])
    checks.append(("claude mcp server", code == 0, "claude mcp get peermesh"))
    return checks


def run(args: argparse.Namespace) -> int:
    choice = getattr(args, "runtime", "both")
    runtimes = {"claude", "codex"} if choice == "both" else {choice}
    if args.cmd == "doctor":
        checks = doctor()
        for name, ok, detail in checks:
            print(f"{'ok ' if ok else 'BAD'} {name:18} {detail}")
        return 0 if all(ok for _, ok, _ in checks) else 1
    try:
        if args.cmd == "install":
            for action in install(runtimes, dry_run=args.dry_run):
                print(("would " if args.dry_run else "") + action)
            return 0
        for action in uninstall(runtimes):
            print(action)
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
