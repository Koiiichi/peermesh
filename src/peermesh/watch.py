"""`peers watch`: the live view of all sessions and their messages."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from peermesh import views
from peermesh.envelope import short_id
from peermesh.registry import Peer
from peermesh.service import Mesh

# Below this terminal width the message table has no kind and id columns.
WIDE = 100


def frame(
    peers: list[Peer], waiting: Mapping[str, int], rows: list[dict[str, Any]], width: int
) -> Group:
    """The live view: one table of sessions and one table of the messages in `rows`.

    `width` is the terminal width. A narrow terminal gets fewer columns and shorter names, so
    the time, the state and the text stay readable.
    """
    # Names, branches and bodies come from sessions and from messages. They go into Text, which
    # rich prints as written; rich reads a plain string as markup, and "[/x]" in a body stops it.
    wide = width >= WIDE
    name_width = 26 if wide else 14
    sessions = Table(title="Sessions", title_justify="left", expand=True, box=None, pad_edge=False)
    sessions.add_column("name", no_wrap=True, max_width=name_width + 6)
    sessions.add_column("runtime", style="dim", no_wrap=True)
    sessions.add_column("status", no_wrap=True, min_width=4)
    sessions.add_column("waiting", no_wrap=True)
    sessions.add_column("branch", no_wrap=True, overflow="ellipsis", ratio=1)
    sessions.add_column("directory", style="dim", no_wrap=True, max_width=20)
    for peer in sorted(peers, key=lambda p: (p.repo_key or p.cwd, p.name)):
        sessions.add_row(
            Text(views.printable(peer.name), style="bold"),
            peer.runtime,
            Text(peer.status, style="yellow" if peer.status == "busy" else "dim"),
            str(waiting.get(peer.id, 0) or ""),
            Text(views.printable(peer.branch or "-")),
            Text(views.printable(Path(peer.worktree or peer.cwd).name)),
        )
    if not peers:
        sessions.add_row(Text("No live session.", style="dim"))
    messages = Table(title="Messages", title_justify="left", expand=True, box=None, pad_edge=False)
    messages.add_column("time", no_wrap=True, min_width=5)
    messages.add_column("from", no_wrap=True, max_width=name_width)
    messages.add_column("to", no_wrap=True, max_width=name_width)
    if wide:
        messages.add_column("kind", no_wrap=True)
    messages.add_column("state", no_wrap=True, min_width=9)
    if wide:
        messages.add_column("msg", no_wrap=True, min_width=8)
    messages.add_column("text", no_wrap=True, overflow="ellipsis", ratio=1, min_width=12)
    for row in rows:
        state = str(row["state"])
        cells: list[Text] = [
            Text(views.clock_time(row)[6:], style="dim"),
            Text(str(row.get("from_name"))),
            Text(str(row.get("to_name"))),
            Text(str(row.get("kind", "")).replace("_", " ")),
            Text(state, style=views.STATE_STYLE.get(state, "dim")),
            Text(short_id(str(row.get("id", ""))), style="dim"),
            Text(views.one_line(row)),
        ]
        messages.add_row(*(cells if wide else [*cells[:3], cells[4], cells[6]]))
    if not rows:
        messages.add_row(Text("No message yet.", style="dim"))
    return Group(sessions, Text(""), messages)


def snapshot(mesh: Mesh, width: int, height: int) -> Group:
    """Read the registry, the inboxes and the ledger, and build a live view of that size.

    The view shows all sessions and the newest messages that fit below them.
    """
    peers = mesh.all_peers()
    waiting = {p.id: len(mesh.inbox.waiting(p.id)) for p in peers}
    # Two titles, two header rows and the gap take five lines; an empty session table takes one.
    room = height - 5 - max(1, len(peers))
    rows = views.message_rows(mesh.ledger.latest())[-max(3, room) :]
    return frame(peers, waiting, rows, width)


def watch(
    mesh: Mesh,
    console: Console,
    *,
    interval: float,
    once: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Show the live view until the user stops it with ctrl+c. `once` prints it one time."""
    if once:
        console.print(snapshot(mesh, console.width, console.height))
        return
    with Live(
        snapshot(mesh, console.width, console.height),
        console=console,
        screen=True,
        auto_refresh=False,
    ) as live:
        try:
            while True:
                sleep(interval)
                live.update(snapshot(mesh, console.width, console.height), refresh=True)
        except KeyboardInterrupt:
            pass
