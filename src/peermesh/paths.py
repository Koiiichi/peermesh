"""Filesystem locations. Each one honours an environment override so tests stay isolated."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("PEERMESH_HOME") or Path.home() / ".peermesh")


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path
