"""Activity stream widget for the unified CTO dashboard.

Shows a chronological feed of recent workflow events across all teams,
read from the per-team telemetry JSONL logs.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual.reactive import reactive
from textual.widgets import Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"

import sys
sys.path.insert(0, str(TEAMS_DIR.parent))
from dashboard.telemetry import ActivityLog  # noqa: E402


class ActivityStream(Static):
    """Rich widget that renders the last N workflow events.

    All I/O is done in a background worker so the Textual event loop
    never stalls — this prevents the screen from blanking.
    """

    events = reactive[list[dict[str, Any]]]([])
    can_focus = True

    def __init__(self, *args, limit: int = 20, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.limit = limit

    def on_mount(self) -> None:
        self.run_worker(self._refresh_worker, thread=True)
        self.set_interval(2, lambda: self.run_worker(self._refresh_worker, thread=True))

    def _refresh_worker(self) -> None:
        all_events: list[tuple[dt.datetime, dict[str, Any]]] = []
        try:
            if TEAMS_DIR.is_dir():
                for team_dir in sorted(TEAMS_DIR.iterdir()):
                    if not (team_dir / ".cto").is_dir():
                        continue
                    log = ActivityLog(team_dir)
                    for ev in log.tail(n=self.limit):
                        try:
                            ts = dt.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                        except (KeyError, ValueError):
                            continue
                        all_events.append((ts, ev))
            all_events.sort(key=lambda x: x[0], reverse=True)
            new_events = [ev for _, ev in all_events[: self.limit]]
        except Exception:
            new_events = []
        self.app.call_from_thread(self._set_events, new_events)

    def _set_events(self, new_events: list[dict[str, Any]]) -> None:
        self.events = new_events

    def render(self) -> Panel:
        try:
            t = Table(expand=True, show_header=False, show_lines=False, pad_edge=False)
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

            border = "bright_blue" if self.has_focus else "blue"
            return Panel(t, title=f"Activity ({len(self.events)})", border_style=border)
        except Exception:
            return Panel("(error rendering activity)", title="Activity", border_style="red")


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
