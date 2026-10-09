"""Append-only message ledger.

A send holds `locked()` for its checks and a "sending" entry, delivers without the lock, then
appends its outcome under the lock. Later entries for a message carry only the fields that
change; `latest()` and `find()` merge them into one record for each message.

When the active file grows by ROTATE_BYTES past its size after the last rotation, the entries
of messages that are final and old move to a compressed archive. The active file keeps the
messages that are open or recent. KEEP_ARCHIVES bounds the archives; the oldest is removed first.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from peermesh import paths

COUNTED_OUTCOMES = ("sending", "delivered", "queued", "pending", "injected")
OPEN_OUTCOMES = ("sending", "pending")
ROTATE_BYTES = 1 << 20
KEEP_ARCHIVES = 30
KEEP_RECENT_S = 3600.0


class Ledger:
    def __init__(self, root: Path | None = None) -> None:
        base = root or paths.home()
        self.path = base / "ledger.jsonl"
        self.archive_dir = base / "ledger-archive"
        self._lock = base / "ledger.lock"
        # The active size after the last rotation. Rotation waits for ROTATE_BYTES of new
        # entries past it, so retained entries alone never cause another rotation.
        self._kept_size = base / "ledger.kept-size"

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
        return _parse(text.splitlines())

    def find(self, msg_id: str) -> dict[str, Any] | None:
        """The merged record of one message, from the active file or, if absent, the archives."""
        found = _merge(e for e in self.entries() if e.get("id") == msg_id)
        if found is not None:
            return found
        for archive in sorted(self.archive_dir.glob("ledger-*.jsonl.gz"), reverse=True):
            try:
                with gzip.open(archive, "rt") as fh:
                    found = _merge(e for e in _parse(fh) if e.get("id") == msg_id)
            except (OSError, EOFError):
                continue
            if found is not None:
                return found
        return None

    def append(self, entry: dict[str, Any]) -> None:
        """Append one entry. The caller holds `locked()`, because a rotation can follow."""
        paths.ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.path.stat().st_size > ROTATE_BYTES + self._last_kept_size():
            self._rotate(time.time())

    def _last_kept_size(self) -> int:
        try:
            return int(self._kept_size.read_text())
        except (OSError, ValueError):
            return 0

    def latest(self) -> list[dict[str, Any]]:
        """One merged record for each message id, in the order of first appearance."""
        by_id: dict[str, dict[str, Any]] = {}
        for entry in self.entries():
            by_id.setdefault(str(entry.get("id")), {}).update(entry)
        return list(by_id.values())

    def count_recent(
        self,
        from_id: str,
        to_id: str,
        window_s: float,
        now: float,
        *,
        new_threads_only: bool = False,
    ) -> int:
        return sum(
            1
            for e in self.latest()
            if e.get("from") == from_id
            and e.get("to") == to_id
            and e.get("outcome") in COUNTED_OUTCOMES
            and now - float(e.get("sent_at", 0.0)) < window_s
            and not (new_threads_only and e.get("thread", e.get("id")) != e.get("id"))
        )

    def count_in_thread(self, from_id: str, thread: str, window_s: float, now: float) -> int:
        return sum(
            1
            for e in self.latest()
            if e.get("from") == from_id
            and e.get("thread") == thread
            and e.get("outcome") in COUNTED_OUTCOMES
            and now - float(e.get("sent_at", 0.0)) < window_s
        )

    def _rotate(self, now: float) -> None:
        lines = self.path.read_text().splitlines()
        merged: dict[str, dict[str, Any]] = {}
        for entry in _parse(lines):
            merged.setdefault(str(entry.get("id")), {}).update(entry)
        keep = {
            mid
            for mid, e in merged.items()
            if e.get("outcome") in OPEN_OUTCOMES
            or now - float(e.get("sent_at", 0.0)) < KEEP_RECENT_S
        }
        kept: list[str] = []
        moved: list[str] = []
        for line in lines:
            # A torn line has no id to keep; it moves to the archive as written.
            item = _parse([line])
            (kept if item and str(item[0].get("id")) in keep else moved).append(line)
        text = "".join(line + "\n" for line in kept)
        if moved:
            paths.ensure_private_dir(self.archive_dir)
            # A nanosecond name sorts by age, and O_EXCL makes sure no archive is overwritten.
            archive = self.archive_dir / f"ledger-{time.time_ns():020d}.jsonl.gz"
            # The archive is complete before the active file shrinks, so a crash between the
            # two steps leaves entries in both places, never in neither.
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt") as fh:
                fh.write("".join(line + "\n" for line in moved))
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
            for old in sorted(self.archive_dir.glob("ledger-*.jsonl.gz"))[:-KEEP_ARCHIVES]:
                old.unlink(missing_ok=True)
        self._kept_size.write_text(str(len(text.encode())))


def _parsed_pairs(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield line.rstrip("\n"), item


def _parse(lines: Iterable[str]) -> list[dict[str, Any]]:
    return [item for _, item in _parsed_pairs(lines)]


def _merge(entries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    merged: dict[str, Any] | None = None
    for entry in entries:
        merged = {**(merged or {}), **entry}
    return merged
