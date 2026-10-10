"""Messages that wait for a hook of the target session to inject them into the model context.

Each session has one directory under `~/.peermesh/inbox/`; each waiting message is one file.
Every method runs while the caller holds the ledger lock: the lock orders the file names, and a
hook takes the files, emits them and records them before another process can see them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from peermesh import paths
from peermesh.envelope import Message

CLAIMED = ".claimed"


@dataclass(frozen=True)
class Waiting:
    path: Path
    record: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.record["id"])

    @property
    def actionable(self) -> bool:
        """True for a message that asks the target to do or decide something."""
        return self.record.get("kind") != "info" or self.record.get("urgency") == "now"

    @property
    def message(self) -> Message:
        return Message.from_json(self.record)


class Inbox:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or paths.home()) / "inbox"

    def dir(self, peer_id: str) -> Path:
        return self.root / re.sub(r"[^A-Za-z0-9_.-]", "_", peer_id)

    def put(self, peer_id: str, record: dict[str, Any]) -> None:
        """Add one message. `record` holds the fields of the message."""
        folder = paths.ensure_private_dir(self.dir(peer_id))
        # The ledger lock serializes senders, so nanosecond names give the order of sends.
        name = f"{time.time_ns():020d}-{record['id']}.json"
        tmp = folder / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, folder / name)

    def waiting(self, peer_id: str) -> list[Waiting]:
        """The messages in send order, including any that a crashed hook claimed."""
        folder = self.dir(peer_id)
        if not folder.is_dir():
            return []
        found: list[Waiting] = []
        for path in sorted(folder.iterdir()):
            if path.name.startswith(".") or not path.name.endswith((".json", ".json" + CLAIMED)):
                continue
            try:
                found.append(Waiting(path, json.loads(path.read_text())))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
        return found

    def claim(self, peer_id: str) -> list[Waiting]:
        """Mark all waiting messages as taken. A crash before `done` leaves them to retry."""
        claimed: list[Waiting] = []
        for item in self.waiting(peer_id):
            target = item.path
            if not target.name.endswith(CLAIMED):
                target = item.path.with_name(item.path.name + CLAIMED)
                os.replace(item.path, target)
            claimed.append(Waiting(target, item.record))
        return claimed

    def done(self, items: list[Waiting]) -> None:
        for item in items:
            item.path.unlink(missing_ok=True)

    def release(self, items: list[Waiting]) -> None:
        """Put claimed messages back to wait for a later hook."""
        for item in items:
            if item.path.name.endswith(CLAIMED):
                os.replace(item.path, item.path.with_name(item.path.name[: -len(CLAIMED)]))

    def remove(self, peer_id: str) -> list[Waiting]:
        """Delete the directory of one session and return the messages that it held."""
        items = self.waiting(peer_id)
        shutil.rmtree(self.dir(peer_id), ignore_errors=True)
        return items

    def peer_ids(self) -> list[str]:
        """The directory names that hold an inbox; they are the sanitized peer ids."""
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())
