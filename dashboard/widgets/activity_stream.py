"""Activity stream widget for the unified CTO dashboard.

Shows a chronological feed of recent workflow events across all teams,
read from the per-team telemetry JSONL logs.

This widget is PASSIVE — the parent DashboardApp gathers data in a single
background worker and pushes it here via set_events().  This eliminates
the overlapping-poll problems that contributed to blanking.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from rich.table import Table
from rich.text import Text

from textual.reactive import reactive
from textual.widgets import Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"


class ActivityStream(Static):
    """Rich widget that renders the last N workflow events.

    PASSIVE: the parent DashboardApp pushes data via set_events().
    """

    events = reactive[list[dict[str, Any]]]([])
    can_focus = True

    def __init__(self, *args, limit: int = 20, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.limit = limit

    def set_events(self, events: list[dict[str, Any]]) -> None:
        """Called by the parent app after gathering data."""
        self.events = events
        self.border_title = f"Activity ({len(self.events)})"

    def render(self) -> Table:
        try:
            t = Table(expand=True, show_header=False, show_lines=False, pad_edge=False, box=None)
            t.add_column("TIME", overflow="ellipsis", no_wrap=True, width=8)
            t.add_column("EVENT", overflow="ellipsis", no_wrap=True, ratio=1)

            for ev in self.events:
                ts_str = ""
                try:
                    ts = dt.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                    ts_str = ts.astimezone().strftime("%H:%M:%S")
                except (KeyError, ValueError):
                    pass
                text = _format_event(ev)
                t.add_row(Text(ts_str, style="dim"), text)

            if not self.events:
                t.add_row(Text("—", style="dim"), Text("(no activity yet)", style="dim"))

            return t
        except Exception:
            return Table(title="(error rendering activity)")


def _format_event(ev: dict[str, Any]) -> Text:
    event_type = ev.get("event", "unknown")
    agent = ev.get("agent", "")
    issue_id = ev.get("issue_id", "") or ev.get("task_id", "")
    kind = ev.get("kind", "")
    title = ev.get("title", "")
    branch = ev.get("branch", "")

    if event_type == "agent_iteration_start":
        return Text(f"{agent} started iteration", style="cyan")
    if event_type == "agent_iteration_end":
        rc = ev.get("exit_code", 0)
        dur = ev.get("duration_ms", 0)
        style = "green" if int(rc) == 0 else "red"
        return Text(f"{agent} finished in {int(dur)//1000}s (exit={rc})", style=style)
    if event_type == "agent_output":
        out = ev.get("output", "")[:60]
        return Text(f"{agent}: {out}…", style="dim")
    if event_type == "issue_created":
        return Text(f"Created {kind} {issue_id}: {title[:40]}", style="yellow")
    if event_type == "merge_executed":
        return Text(f"Merged {branch} → {ev.get('target', '')}", style="green")
    if event_type == "commit_pushed":
        msg = ev.get("message", "")[:40]
        return Text(f"Commit on {branch}: {msg}", style="blue")
    if event_type == "reconciler_tick":
        actions = ev.get("actions", "")
        n = len([a for a in actions.split(";") if a.strip()])
        return Text(f"Reconciler took {n} action{'s' if n != 1 else ''}", style="magenta")
    if event_type == "stuck_detected":
        reason = ev.get("reason", "")
        return Text(f"Stuck: {issue_id} — {reason}", style="red")
    return Text(f"{event_type}: {str(ev)[:60]}", style="dim")
