from __future__ import annotations

import json
import multiprocessing
import os
import stat

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
