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
    for event, name in HOOK_EVENTS.items():
        # --inbox tells the hook that the post-tool hook below is installed with it.
        flag = " --inbox" if name in ("session-start", "prompt") else ""
        command = f"{shlex.quote(exe)} hook {name} --runtime {runtime}{flag} {MARK}"
        # A Stop hook can wake its own session through `codex queue`, which has 30 seconds.
        timeout = 45 if event == "Stop" else 10
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        )
    hooks.setdefault("PostToolUse", []).append(
        {"hooks": [{"type": "command", "command": post_tool_command(exe, runtime), "timeout": 10}]}
    )
    return config


def post_tool_command(exe: str, runtime: str) -> str:
    return f"/bin/sh -c {shlex.quote(POST_TOOL_GUARD)} peermesh {shlex.quote(exe)} {runtime} {MARK}"


def post_tool_installed(runtime: str) -> bool:
    """True when the hook configuration of runtime holds the peermesh post-tool hook."""
    path = paths.claude_dir() / "settings.json" if runtime == "claude" else paths.codex_home()
    try:
        config = _read_json(path if runtime == "claude" else path / "hooks.json")
    except (OSError, ValueError):
        return False
    hooks = config.get("hooks")
    groups = hooks.get("PostToolUse", []) if isinstance(hooks, dict) else []
    return any(
        _is_ours(h) and "hook post-tool" in str(h.get("command", ""))
        for g in groups
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
    settings = _read_json(settings_path)
    checks.append(("claude hooks", settings != strip_hooks(settings), str(settings_path)))
    codex_hooks = paths.codex_home() / "hooks.json"
    codex_cfg = _read_json(codex_hooks)
    checks.append(
        (
            "codex hooks",
            codex_cfg != strip_hooks(codex_cfg),
            f"{codex_hooks} (approve hook trust in Codex once)",
        )
    )
    for runtime in ("claude", "codex"):
        ok = post_tool_installed(runtime)
        detail = "installed" if ok else "run `peers install` again"
        checks.append((f"{runtime} post-tool hook", ok, detail))
    rule = paths.codex_home() / "rules" / "peermesh.rules"
    checks.append(("codex rule", rule.exists(), str(rule)))
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
