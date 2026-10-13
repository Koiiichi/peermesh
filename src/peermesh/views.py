"""Text for a person outside the agent sessions: message states, one thread, the status line.

This module does not import rich, so the status line command starts fast.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from peermesh.envelope import short_id
from peermesh.registry import scoped
from peermesh.service import SENT_OUTCOMES, Mesh

STATE_STYLE = {
    "done": "bold green",
    "answered": "green",
    "delivered": "green",
    "queued": "green",
    "injected": "green",
    "pending": "yellow",
    "sending": "yellow",
    "refused": "red",
    "undelivered": "red",
}


def message_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record for each message in send order, with `state`: the last thing that happened.

    The names in a record are made printable, because a view prints them as they are.

    `entries` are merged ledger records. The state is "done" or "answered" when the receiver
    replied, and the delivery outcome until then.
    """
    replies: dict[str, bool] = {}
    for entry in entries:
        parent = entry.get("in_reply_to")
        if parent and entry.get("outcome") in SENT_OUTCOMES:
            replies[str(parent)] = replies.get(str(parent), False) or bool(entry.get("done"))
    rows = []
    for entry in entries:
        if "from" not in entry:
            continue
        answered = replies.get(str(entry.get("id")))
        state = str(entry.get("outcome", "")) if answered is None else "answered"
        rows.append(
            entry
            | {
                "state": "done" if answered else state,
                "from_name": printable(str(entry.get("from_name"))),
                "to_name": printable(str(entry.get("to_name"))),
            }
        )
    return rows


def clock_time(entry: Mapping[str, Any]) -> str:
    """The local time of a ledger record as month-day hour:minute, or "--" without a time."""
    try:
        return datetime.fromisoformat(str(entry.get("ts"))).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return "--"


def printable(text: str) -> str:
    """Text from a peer, safe to print: control characters show as escapes such as \\x1b.

    A message body can hold terminal control sequences, which can clear the screen or set the
    clipboard of the person who reads it.
    """
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode() for ch in text)


def one_line(entry: Mapping[str, Any]) -> str:
    """The printable body of a record, or the note of a refusal, on one line."""
    return printable(" ".join(str(entry.get("body") or entry.get("note") or "").split()))


def thread_lines(
    records: list[dict[str, Any]], width: int, style: Callable[[str, str], str]
) -> list[str]:
    """The messages of one thread as text lines: a header line, then the indented body.

    `records` are the merged ledger records of the thread. `style` colors a word for a person
    at a terminal and returns it unchanged for an agent.
    """
    rows = message_rows(records)
    names = sorted({r["from_name"] for r in rows} | {r["to_name"] for r in rows})
    count = f"{len(rows)} message" + ("" if len(rows) == 1 else "s")
    lines = [style(f"thread {short_id(str(rows[0].get('thread', rows[0]['id'])))}", "bold")]
    lines[0] += f" · {' ↔ '.join(names)} · {count}"
    for row in rows:
        state = str(row["state"])
        # STATE_STYLE holds rich styles such as "bold green"; the last word is the color.
        color = STATE_STYLE.get(state, "dim").split()[-1]
        lines.append("")
        lines.append(
            f"{style(clock_time(row), 'dim')}  {style(str(row.get('from_name')), 'bold')} → "
            f"{row.get('to_name')}  {str(row.get('kind', '')).replace('_', ' ')}  "
            f"{style(state, color)}  msg {short_id(str(row.get('id', '')))}"
        )
        text = str(row.get("body") or row.get("note") or "")
        for paragraph in map(printable, text.splitlines() or [""]):
            lines += textwrap.wrap(
                paragraph, max(20, width - 2), initial_indent="  ", subsequent_indent="  "
            ) or [""]
    return lines


def statusline(mesh: Mesh, payload: Mapping[str, Any]) -> str:
    """The status line segment of one Claude Code session, or "" when it is not registered.

    `payload` is the JSON that Claude Code gives a status line command on stdin.
    """
    me = mesh.registry.get(f"claude:{payload.get('session_id')}")
    if me is None:
        return ""
    peers = len(scoped(mesh.registry.live(), me, "repo"))
    parts = [me.name, f"{peers} peer" + ("" if peers == 1 else "s")]
    waiting = len(mesh.inbox.waiting(me.id))
    if waiting:
        parts.append(f"{waiting} waiting")
    return " · ".join(parts)
