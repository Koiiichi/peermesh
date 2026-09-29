"""Append-only message ledger. Callers hold `locked()` across check, deliver and append."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from peermesh import paths

COUNTED_OUTCOMES = ("delivered", "queued")


class Ledger:
    def __init__(self, root: Path | None = None) -> None:
        base = root or paths.home()
        self.path = base / "ledger.jsonl"
        self._lock = base / "ledger.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        paths.ensure_private_dir(self.path.parent)
        fd = os.open(self._lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def entries(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def find(self, msg_id: str) -> dict[str, Any] | None:
        for entry in reversed(self.entries()):
            if entry.get("id") == msg_id:
                return entry
        return None

    def append(self, entry: dict[str, Any]) -> None:
        paths.ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def count_recent(self, from_id: str, to_id: str, window_s: float, now: float) -> int:
        return sum(
            1
            for e in self.entries()
            if e.get("from") == from_id
            and e.get("to") == to_id
            and e.get("outcome") in COUNTED_OUTCOMES
            and now - float(e.get("sent_at", 0.0)) < window_s
        )
