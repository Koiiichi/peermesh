from __future__ import annotations

import gzip
import json
import multiprocessing
import os
import stat
import time

import pytest

from peermesh import ledger
from peermesh.ledger import Ledger


def _entry(
    i: int, frm: str = "a", to: str = "b", sent_at: float = 1000.0, outcome: str = "delivered"
) -> dict[str, object]:
    return {"id": f"m{i}", "from": frm, "to": to, "sent_at": sent_at, "outcome": outcome}


def test_append_find_and_mode() -> None:
    led = Ledger()
    with led.locked():
        led.append(_entry(1))
    assert led.find("m1") is not None
    assert led.find("m2") is None
    assert stat.S_IMODE(led.path.stat().st_mode) == 0o600


def test_entries_skip_bad_lines() -> None:
    led = Ledger()
    with led.locked():
        led.append(_entry(1))
    with led.path.open("a") as fh:
        fh.write("{torn\n[1,2]\n")
    assert [e["id"] for e in led.entries()] == ["m1"]


def test_count_recent_window_and_outcome() -> None:
    led = Ledger()
    with led.locked():
        led.append(_entry(1, sent_at=100.0))
        led.append(_entry(2, sent_at=650.0))
        led.append(_entry(3, sent_at=690.0, outcome="refused"))
        led.append(_entry(4, sent_at=695.0, outcome="queued"))
        led.append(_entry(5, frm="b", to="a", sent_at=699.0))
    assert led.count_recent("a", "b", 600.0, now=700.0) == 2


def _worker(home: str, worker: int) -> None:
    os.environ["PEERMESH_HOME"] = home
    led = Ledger()
    for i in range(50):
        with led.locked():
            led.append({"id": f"w{worker}-{i}", "pad": "x" * 2000})


def test_concurrent_appends_are_whole_lines() -> None:
    home = os.environ["PEERMESH_HOME"]
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker, args=(home, w)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    lines = Ledger().path.read_text().splitlines()
    assert len(lines) == 200
    assert len({json.loads(line)["id"] for line in lines}) == 200


def test_count_recent_counts_each_message_once() -> None:
    led = Ledger()
    with led.locked():
        led.append(_entry(1, sent_at=690.0, outcome="sending"))
        led.append(_entry(1, sent_at=690.0, outcome="delivered"))
        led.append(_entry(2, sent_at=695.0, outcome="sending"))
    assert led.count_recent("a", "b", 600.0, now=700.0) == 2
    assert led.find("m1") is not None and led.find("m1")["outcome"] == "delivered"


def test_outcome_entries_merge_into_one_record() -> None:
    led = Ledger()
    with led.locked():
        led.append({"id": "m1", "from": "a", "to": "b", "body": "hi", "outcome": "sending"})
        led.append({"id": "m1", "outcome": "delivered", "note": "ok"})
    [record] = led.latest()
    assert record["body"] == "hi" and record["outcome"] == "delivered"
    found = led.find("m1")
    assert found is not None and found["body"] == "hi" and found["note"] == "ok"


def test_rotation_archives_everything_and_keeps_open_and_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ledger, "ROTATE_BYTES", 2000)
    led = Ledger()
    now = time.time()
    with led.locked():
        led.append(_entry(1, sent_at=now - 90_000))
        led.append({"id": "open", "sent_at": now - 90_000, "outcome": "pending"})
        led.append(_entry(2, sent_at=now - 10))
        for i in range(3, 30):
            led.append(_entry(i, sent_at=now - 90_000) | {"pad": "x" * 100})
    archives = sorted(led.archive_dir.glob("ledger-*.jsonl.gz"))
    archived: set[str] = set()
    for archive in archives:
        with gzip.open(archive, "rt") as fh:
            archived |= {json.loads(line)["id"] for line in fh}
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    active = {e["id"] for e in led.entries()}
    assert {f"m{i}" for i in range(1, 30)} | {"open"} <= archived | active
    assert "m1" in archived and not archived & {"open", "m2"}
    assert "open" in active and "m2" in active and "m1" not in active
    found = led.find("m1")
    assert found is not None and found["outcome"] == "delivered"


def test_archives_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "ROTATE_BYTES", 300)
    monkeypatch.setattr(ledger, "KEEP_ARCHIVES", 3)
    led = Ledger()
    with led.locked():
        for i in range(40):
            led.append(_entry(i, sent_at=0.0) | {"pad": "x" * 200})
    assert len(list(led.archive_dir.glob("ledger-*.jsonl.gz"))) == 3


def test_count_recent_new_threads_only() -> None:
    led = Ledger()
    with led.locked():
        led.append(_entry(1, sent_at=690.0) | {"hop": 0})
        led.append(_entry(2, sent_at=691.0) | {"hop": 3})
    assert led.count_recent("a", "b", 600.0, now=700.0) == 2
    assert led.count_recent("a", "b", 600.0, now=700.0, new_threads_only=True) == 1


def test_retained_entries_alone_do_not_repeat_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "ROTATE_BYTES", 2000)
    led = Ledger()
    now = time.time()
    with led.locked():
        led.append(_entry(0, sent_at=now - 90_000) | {"pad": "x" * 100})
        for i in range(1, 40):
            led.append(_entry(i, sent_at=now - 5) | {"pad": "x" * 100})
        archives = sorted(led.archive_dir.glob("ledger-*.jsonl.gz"))
        for i in range(40, 50):
            led.append(_entry(i, sent_at=now - 5) | {"pad": "x" * 10})
    assert sorted(led.archive_dir.glob("ledger-*.jsonl.gz")) == archives
    found = led.find("m0")
    assert found is not None and found["outcome"] == "delivered"
    assert len(led.latest()) == 49


def test_torn_lines_move_to_the_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger, "ROTATE_BYTES", 500)
    led = Ledger()
    with led.locked():
        led.append(_entry(1, sent_at=0.0))
    with led.path.open("a") as fh:
        fh.write("{torn\n")
    with led.locked():
        led.append(_entry(2, sent_at=0.0) | {"pad": "x" * 600})
    [archive] = led.archive_dir.glob("ledger-*.jsonl.gz")
    with gzip.open(archive, "rt") as fh:
        assert "{torn" in fh.read()
