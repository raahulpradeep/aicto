#!/usr/bin/env -S uv run --quiet --with textual,rich python
"""AI CTO Dashboard v2 — event-driven, real-time, interactive.

Uses Textual DataTable widgets for smooth in-place row updates.
No more panel collapse during polling.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Markdown,
    Static,
    TextArea,
)

from dashboard.bus_adapter import make_batched_adapter
from dashboard.notify import notify
from dashboard.widgets.activity_stream import ActivityStream, _format_event
from dashboard.widgets.epic_pipeline import EpicPipeline
from dashboard.widgets.agent_card import AgentCard

from dashboard.screens import EpicDetailScreen, AgentDetailScreen, DiffViewerScreen
from dashboard.modals import ConfirmModal, CommentModal

TEAMS_DIR = ROOT / "teams"
_prev_data: dict[str, tuple] = {}


# ---- shell helpers (copied from ctodashboard.py) --------------------------


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def tmux_alive(session: str) -> bool:
    return (
        subprocess.call(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) == 0
    )


def tmux_windows(session: str) -> list[str]:
    out = _run(["tmux", "list-windows", "-t", session, "-F", "#W"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _bd_json(args: list[str], cwd: Path) -> list:
    try:
        return json.loads(_run(["bd", *args, "--json"], cwd) or "[]")
    except json.JSONDecodeError:
        return []


def _bd_popen(args: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["bd", *args, "--json"],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _read_bd_proc(proc: subprocess.Popen, timeout: float = 5.0) -> list:
    try:
        stdout, _ = proc.communicate(timeout=timeout)
        return json.loads(stdout) if proc.returncode == 0 else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


# ---- data gatherers (same as before) -------------------------------------


def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _age(row: dict, now: dt.datetime) -> str:
    updated = _parse_iso(row.get("updated_at") or "")
    created = _parse_iso(row.get("created_at") or "")
    ts = updated or created
    if ts is None:
        return "—"
    secs = int((now - ts).total_seconds())
    return _fmt_elapsed(max(0, secs))


def _kind(labels: list[str]) -> str:
    for l in labels:
        if l.startswith("kind:"):
            return l.split(":", 1)[1]
    return "—"


def _agent_status_from_activity(team_name: str, team_dir: Path) -> dict[str, dict]:
    """Derive per-slot agent status by scanning the tail of activity.jsonl.

    Returns {slot: {"status": str, "last_ts": datetime|None,
                    "task_id": str|None}}.

    Status values:
        working      -- iteration_start without a matching iteration_end
        idle         -- iteration_end is the latest event
        stuck        -- working for > 15 minutes (likely zombie)
        unknown      -- no activity events seen
    """
    log_path = team_dir / ".cto" / "activity.jsonl"
    if not log_path.exists():
        return {}

    # Tail the last ~1MB to keep this cheap; agents emit small lines.
    try:
        size = log_path.stat().st_size
        offset = max(0, size - 1_000_000)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            if offset:
                f.readline()  # discard partial first line
            lines = f.readlines()
    except OSError:
        return {}

    latest: dict[str, dict] = {}
    prefix = f"{team_name}:"
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        evt = ev.get("event") or ev.get("topic") or ""
        if evt not in ("agent_iteration_start", "agent_iteration_end"):
            continue
        agent = ev.get("agent") or ""
        if not agent.startswith(prefix):
            continue
        slot = agent[len(prefix):]
        latest[slot] = {
            "event": evt,
            "ts_str": ev.get("ts") or ev.get("created_at") or "",
            "task_id": ev.get("task_id") or "",
        }

    now = dt.datetime.now(dt.timezone.utc)
    out: dict[str, dict] = {}
    for slot, info in latest.items():
        ts = _parse_iso(info["ts_str"])
        age = (now - ts).total_seconds() if ts else None
        if info["event"] == "agent_iteration_start":
            status = "stuck" if age is not None and age > 900 else "working"
        else:
            status = "idle"
        out[slot] = {"status": status, "last_ts": ts, "task_id": info["task_id"]}
    return out


def _gather_team(team_name: str, team_dir: Path) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    # Agents
    agents: list[dict] = []
    sess = f"cto-{team_name}"
    windows = tmux_windows(sess) if tmux_alive(sess) else []
    activity_status = _agent_status_from_activity(team_name, team_dir)
    proc = _bd_popen(["ready", "--label", f"role:developer", "--json"], team_dir)
    dev_rows = _read_bd_proc(proc)
    dev_map = {r["id"]: r for r in dev_rows}

    proc = _bd_popen(["ready", "--label", f"role:reviewer", "--json"], team_dir)
    rev_rows = _read_bd_proc(proc)
    rev_map = {r["id"]: r for r in rev_rows}

    proc = _bd_popen(["ready", "--label", f"role:manager", "--json"], team_dir)
    mgr_rows = _read_bd_proc(proc)
    mgr_map = {r["id"]: r for r in mgr_rows}

    role_map = {**dev_map, **rev_map, **mgr_map}

    for w in windows:
        parts = w.split(":", 1)
        slot = parts[1] if len(parts) > 1 else w
        issue = role_map.get(slot)
        act = activity_status.get(slot, {})
        agents.append({
            "agent": f"{team_name}:{slot}", "issue": issue, "team": team_name,
            "provider": "claude", "model": "sonnet", "window": slot,
            "activity_status": act.get("status"),  # working|idle|stuck|None
            "activity_last_ts": act.get("last_ts"),
            "activity_task_id": act.get("task_id"),
        })

    # Inbox (role:cto)
    inbox = _bd_json(["list", "--status", "open", "--label", "role:cto", "--json"], team_dir)
    inbox_rows = []
    for r in inbox:
        labels = r.get("labels", [])
        if any(l.startswith("kind:approval") or l.startswith("kind:merge") for l in labels):
            inbox_rows.append({**r, "team": team_name})

    # Open tasks (dev/review not claimed or in-progress)
    open_tasks = _bd_json(["list", "--status", "open", "--json"], team_dir)
    open_rows = []
    for r in open_tasks:
        labels = r.get("labels", [])
        if any(l.startswith("kind:dev") or l.startswith("kind:review") or l.startswith("kind:breakdown") or l.startswith("kind:plan") for l in labels):
            open_rows.append({**r, "team": team_name})

    # Closed recently
    closed = _bd_json(["list", "--status", "closed", "--limit", "20", "--json"], team_dir)
    closed_rows = [{**r, "team": team_name} for r in closed]

    # Pipelines (epics)
    epics = _bd_json(["list", "--status", "open", "--label", "kind:epic", "--json"], team_dir)
    pipelines = []
    for epic in epics:
        epic_id = epic.get("id", "")
        title = epic.get("title", "")
        labels = epic.get("labels", [])
        stages = []
        if "class:bypass-cto" in labels:
            stages = ["breakdown", "plan", "dev", "review", "merge", "ship"]
        else:
            stages = ["breakdown", "plan", "dev", "review", "merge", "ship"]
        pipelines.append({"epic_id": epic_id, "title": title, "stages": stages, "team": team_name})

    return agents, inbox_rows, open_rows, closed_rows, pipelines


def gather_all():
    agents: list[dict] = []
    inbox: list[dict] = []
    open_tasks: list[dict] = []
    closed: list[dict] = []
    running: list[str] = []
    pipelines: list[dict] = []

    if not TEAMS_DIR.is_dir():
        return agents, inbox, open_tasks, closed, running, pipelines

    teams = [p for p in sorted(TEAMS_DIR.iterdir()) if p.is_dir() and (p / ".cto").is_dir()]
    live_team_names = {t.name for t in teams}
    for gone in list(_prev_data.keys()):
        if gone not in live_team_names:
            del _prev_data[gone]

    for team in teams:
        result = None
        try:
            result = _gather_team(team.name, team)
        except Exception:
            pass

        if result is None:
            cached = _prev_data.get(team.name)
            if cached is None:
                continue
            a_rows, i_rows, o_rows, c_rows, p_rows = cached
            a_rows = [{**r, "stale": True} for r in a_rows]
            i_rows = [{**r, "stale": True} for r in i_rows]
            o_rows = [{**r, "stale": True} for r in o_rows]
            c_rows = [{**r, "stale": True} for r in c_rows]
            p_rows = [{**r, "stale": True} for r in p_rows]
        else:
            a_rows, i_rows, o_rows, c_rows, p_rows = result
            _prev_data[team.name] = result

        running.append(team.name)
        agents.extend(a_rows)
        inbox.extend(i_rows)
        open_tasks.extend(o_rows)
        closed.extend(c_rows)
        pipelines.extend(p_rows)

    closed.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
    closed_recent = closed[:10]
    return agents, inbox, open_tasks, closed_recent, running, pipelines


from dashboard.telemetry import ActivityLog


def gather_events(limit: int = 20):
    all_events: list[tuple[dt.datetime, dict]] = []
    if not TEAMS_DIR.is_dir():
        return []
    for team_dir in sorted(TEAMS_DIR.iterdir()):
        if not (team_dir / ".cto").is_dir():
            continue
        try:
            log = ActivityLog(team_dir)
            for ev in log.tail(n=limit):
                try:
                    ts = dt.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                all_events.append((ts, ev))
        except Exception:
            pass
    all_events.sort(key=lambda x: x[0], reverse=True)
    return [ev for _, ev in all_events[:limit]]


# ---- panel builders (replaced by DataTable in compose) --------------------


# ---- modals ---------------------------------------------------------------


class ApproveModal(ModalScreen[str | None]):
    """Modal to confirm approval with an optional comment."""

    def __init__(self, item: dict) -> None:
        self.item = item
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="approve-modal"):
            yield Markdown(f"### Approve: {self.item['title']}\n\n{item_desc(self.item)}")
            yield TextArea(id="comment", classes="comment-input")
            with Horizontal():
                yield Button("Approve", variant="success", id="approve")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            comment = self.query_one("#comment", TextArea).text
            self.dismiss(comment)
        else:
            self.dismiss(None)


class RejectModal(ModalScreen[str | None]):
    """Modal to reject with a required comment."""

    def __init__(self, item: dict) -> None:
        self.item = item
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="reject-modal"):
            yield Markdown(f"### Reject: {self.item['title']}\n\n{item_desc(self.item)}")
            yield TextArea(id="comment", classes="comment-input")
            with Horizontal():
                yield Button("Reject", variant="error", id="reject")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reject":
            comment = self.query_one("#comment", TextArea).text
            if not comment.strip():
                self.notify("A comment is required to reject", severity="error")
                return
            self.dismiss(comment)
        else:
            self.dismiss(None)


class FreezeModal(ModalScreen[str | None]):
    """Modal to freeze an epic or inbox item with a comment."""

    def __init__(self, item: dict) -> None:
        self.item = item
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="freeze-modal"):
            yield Markdown(f"### Freeze: {self.item.get('title', 'Item')}\n\n{item_desc(self.item)}")
            yield TextArea(id="comment", classes="comment-input", text="Need to review before proceeding")
            with Horizontal():
                yield Button("Freeze", variant="warning", id="freeze")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "freeze":
            comment = self.query_one("#comment", TextArea).text
            self.dismiss(comment)
        else:
            self.dismiss(None)


def item_desc(item: dict) -> str:
    labels = item.get("labels", [])
    kind = _kind(labels)
    return f"- **ID:** {item['id']}\n- **Kind:** {kind}\n- **Team:** {item.get('team', '—')}"


# ---- focusable static (for tab cycling) ----------------------------------


class FocusableStatic(Static, can_focus=True):
    """A Static widget that can receive focus."""

    BINDINGS = [
        ("enter", "select", "Select"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
    ]


# ---- main app -------------------------------------------------------------


class DashboardApp(App):
    """Event-driven CTO dashboard with DataTable widgets for smooth updates."""

    TITLE = "AI CTO Dashboard"

    CSS = """
    #top { height: 2fr; }
    #agents { width: 55%; height: 100%; min-height: 5; }
    #inbox { width: 45%; height: 100%; min-height: 5; }
    #mid { height: 2fr; }
    #pipeline { width: 60%; height: 100%; min-height: 5; }
    #activity { width: 40%; height: 100%; min-height: 5; }
    #bottom { height: 1fr; }
    #open { width: 100%; height: 100%; min-height: 3; }
    #footer { height: 1; }
    """

    snapshot = reactive(None)
    gather_ms = reactive(0)
    data_ts = reactive(0.0)
    inbox_items = reactive[list[dict]]([])
    selected_inbox_index = reactive(0)
    selected_agent_index = reactive(0)
    events = reactive[list[dict]]([])
    _notified_ids: set[str] = set()
    _poll_in_flight: bool = False
    _event_count = reactive(0)
    _last_event_ts = reactive(0.0)

    def compose(self) -> ComposeResult:
        # Use DataTable widgets instead of Static for smooth row-level updates
        self._agents_dt = DataTable(id="agents", show_cursor=True, cursor_type="row")
        self._agents_dt.add_columns("AGENT", "STATUS", "ELAPSED", "MODEL", "TOKENS", "ISSUE")
        self._agents_dt.zebra_stripes = True
        self._agents_dt.focus()
        yield self._agents_dt

        self._inbox_dt = DataTable(id="inbox", show_cursor=True, cursor_type="row")
        self._inbox_dt.add_columns("TEAM", "ID", "TITLE", "KIND", "AGE", "ACTIONS")
        self._inbox_dt.zebra_stripes = True
        yield self._inbox_dt

        with Horizontal(id="mid"):
            yield EpicPipeline(id="pipeline")
            yield ActivityStream(id="activity")

        self._open_dt = DataTable(id="open", show_cursor=True, cursor_type="row")
        self._open_dt.add_columns("TEAM", "ID", "KIND", "TITLE", "ASSIGNEE", "AGE")
        self._open_dt.zebra_stripes = True
        yield self._open_dt

        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._adapter = make_batched_adapter(self._on_events)
        self._adapter.start()
        self.poll_data()
        self.set_interval(5.0, self.poll_data)
        self.set_interval(1.0, self._tick_footer)

    def on_unmount(self) -> None:
        self._adapter.stop()

    # ------------------------------------------------------------------
    # Event bus callbacks (real-time)
    # ------------------------------------------------------------------

    def _on_events(self, batch: list[dict]) -> None:
        self.app.call_from_thread(self._apply_events, batch)

    def _apply_events(self, batch: list[dict]) -> None:
        now = time.monotonic()
        self._last_event_ts = now
        self._event_count += len(batch)

        current = list(self.events)
        for ev in batch:
            current.insert(0, ev)
        current = current[:100]
        self.events = current

        try:
            self.query_one("#activity", ActivityStream).set_events(current[:20])
        except Exception:
            pass

        self._tick_footer()

    # ------------------------------------------------------------------
    # Background polling (structural data)
    # ------------------------------------------------------------------

    def poll_data(self) -> None:
        if self._poll_in_flight:
            return
        self._poll_in_flight = True
        self.run_worker(self._poll_worker, thread=True)

    def _poll_worker(self) -> None:
        try:
            t0 = time.monotonic()
            agents, inbox, open_tasks, closed, running, pipelines = gather_all()
            events = gather_events(limit=20)
            gather_ms = int((time.monotonic() - t0) * 1000)
            self.app.call_from_thread(
                self._apply_data,
                agents, inbox, open_tasks, closed, running,
                pipelines, events, gather_ms,
            )
        except Exception as exc:
            self.app.call_from_thread(self._handle_poll_error, str(exc))
        finally:
            self.app.call_from_thread(self._clear_poll_flag)

    def _clear_poll_flag(self) -> None:
        self._poll_in_flight = False

    def _apply_data(
        self, agents: list[dict], inbox: list[dict], open_tasks: list[dict],
        closed: list[dict], running: list[str], pipelines: list[dict],
        events: list[dict], gather_ms: int,
    ) -> None:
        self.gather_ms = gather_ms
        self.data_ts = time.monotonic()
        self.snapshot = (agents, inbox, open_tasks, closed, running)
        self.inbox_items = inbox

        # Update DataTables in-place (no widget replacement = no collapse)
        self._update_agents_table(agents, running)
        self._update_inbox_table(inbox)
        self._update_open_table(open_tasks)

        try:
            self.query_one("#pipeline", EpicPipeline).set_pipelines(pipelines)
        except Exception:
            pass

        try:
            self.query_one("#activity", ActivityStream).set_events(events)
        except Exception:
            pass

        self._tick_footer()

    # ------------------------------------------------------------------
    # DataTable update methods (in-place, no collapse)
    # ------------------------------------------------------------------

    def _update_agents_table(self, agents: list[dict], running: list[str]) -> None:
        table = self._agents_dt
        table.clear()
        now = dt.datetime.now(dt.timezone.utc)

        if not running:
            table.add_row("—", "—", "—", "—", "—", "no running teams — start with `cto start <team>`")
            return

        for idx, row in enumerate(agents):
            stale = row.get("stale", False)
            agent = ("~" if stale else "") + row["agent"]
            issue = row.get("issue")
            act_status = row.get("activity_status")
            act_ts = row.get("activity_last_ts")

            if issue:
                started = _parse_iso(issue.get("started_at") or issue.get("updated_at") or "")
            else:
                started = act_ts
            secs = int((now - started).total_seconds()) if started else 0
            elapsed = _fmt_elapsed(max(0, secs)) if started else "—"

            # Prefer activity-derived status (real signal); fall back to bd lookup.
            if act_status == "working":
                status = "🟢 working"
            elif act_status == "stuck":
                status = "🟡 stuck"
            elif act_status == "idle":
                status = "⚪ idle"
            elif issue:
                status = "🟢 working"
            else:
                status = "⚪ idle"
            if stale:
                status = f"💤 {status}"

            if issue:
                title = _truncate(issue.get("title", ""), 40)
                issue_text = f"{issue['id']}  {title}"
            elif row.get("activity_task_id"):
                issue_text = row["activity_task_id"]
            else:
                issue_text = "—"

            model = f"{row.get('provider', '—')}:{row.get('model', '—')}"
            table.add_row(agent, status, elapsed, model, "—", issue_text)

    def _update_inbox_table(self, inbox: list[dict]) -> None:
        table = self._inbox_dt
        table.clear()
        now = dt.datetime.now(dt.timezone.utc)

        if not inbox:
            table.add_row("—", "—", "(nothing waiting on the CTO)", "—", "—", "—")
            return

        for row in inbox:
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row.get("team", "")
            kind = _kind(row.get("labels", []))
            age_str = _age(row, now)
            actions = "[A][R][F]" if not stale else ""
            table.add_row(team_disp, row["id"], _truncate(row.get("title", ""), 45), kind, age_str, actions)

    def _update_open_table(self, open_tasks: list[dict]) -> None:
        table = self._open_dt
        table.clear()
        now = dt.datetime.now(dt.timezone.utc)

        if not open_tasks:
            table.add_row("—", "—", "—", "(no open tasks)", "—", "—")
            return

        rows = sorted(open_tasks, key=lambda r: (r.get("priority") or 99, r.get("created_at") or ""))
        for row in rows:
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row.get("team", "")
            assignee = row.get("assignee") or ""
            table.add_row(team_disp, row.get("id", ""), _kind(row.get("labels") or []),
                       _truncate(row.get("title", ""), 50), assignee or "—", _age(row, now))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _tick_footer(self) -> None:
        data_age_ms = int((time.monotonic() - self.data_ts) * 1000) if self.data_ts else 999999
        event_age_ms = int((time.monotonic() - self._last_event_ts) * 1000) if self._last_event_ts else 999999
        fresh = data_age_ms < 5000
        event_fresh = event_age_ms < 3000

        indicator = "● live" if (fresh and event_fresh) else ("◐ events" if event_fresh else "○ stale")
        ts = dt.datetime.now().strftime("%H:%M:%S")
        teams_summary = ", ".join(self.snapshot[4] if self.snapshot else [])
        if not teams_summary:
            teams_summary = "—"

        text = Text(
            f"q quit · tab focus · ↑↓ navigate · enter drill · a approve · r reject · f freeze · "
            f"k kill · s spawn · d diff · c comment · "
            f"{indicator} · events={self._event_count} · gather {self.gather_ms}ms · teams: {teams_summary} · {ts}",
            style="dim", justify="center",
        )
        try:
            self.query_one("#footer", Static).update(text)
        except Exception:
            pass

    # -- inbox notification watcher ----------------------------------------

    def watch_inbox_items(self, items: list[dict]) -> None:
        current_ids = {i["id"] for i in items}
        new_ids = current_ids - self._notified_ids
        for iid in new_ids:
            for item in items:
                if item["id"] == iid:
                    title = item.get("title", "Inbox item")
                    notify("CTO Inbox", f"New: {title}")
                    break
        self._notified_ids = current_ids

    # -- data table selection handling --------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter/selection on DataTable widgets."""
        table_id = event.data_table.id
        row_key = event.row_key

        if table_id == "agents":
            # Get the selected agent and open agent detail
            self._show_agent_detail(row_key)
        elif table_id == "inbox":
            # Get the selected inbox item
            cursor_row = event.data_table.cursor_row
            if cursor_row is not None and cursor_row < len(self.inbox_items):
                self.selected_inbox_index = cursor_row
                self.action_approve()
        elif table_id == "open":
            # Open task detail
            self._show_open_task_detail(row_key)

    def _show_agent_detail(self, row_key) -> None:
        """Show agent detail screen for selected agent."""
        # Get agent info from the current snapshot
        if self.snapshot is None:
            return
        agents = self.snapshot[0]
        cursor_row = self._agents_dt.cursor_row
        if cursor_row is None or cursor_row >= len(agents):
            return
        agent = agents[cursor_row]
        self.push_screen(AgentDetailScreen(agent))

    def _show_open_task_detail(self, row_key) -> None:
        """Show detail for selected open task."""
        if self.snapshot is None:
            return
        open_tasks = self.snapshot[2]
        cursor_row = self._open_dt.cursor_row
        if cursor_row is None or cursor_row >= len(open_tasks):
            return
        task = open_tasks[cursor_row]
        self.notify(f"Task: {task.get('title', '—')}", title="Open Task")

    # -- keyboard actions ---------------------------------------------------

    def action_approve(self) -> None:
        focused = self.app.focused
        if focused and focused.id == "inbox":
            if not self.inbox_items:
                self.notify("Inbox is empty", severity="warning")
                return
            if self.selected_inbox_index >= len(self.inbox_items):
                self.selected_inbox_index = max(0, len(self.inbox_items) - 1)
            item = self.inbox_items[self.selected_inbox_index]
            self._pending_item = item
            self.push_screen(ApproveModal(item), self._on_approve)
        else:
            self.notify("Focus the inbox panel first (Tab)", severity="warning")

    def action_reject(self) -> None:
        focused = self.app.focused
        if focused and focused.id == "inbox":
            if not self.inbox_items:
                self.notify("Inbox is empty", severity="warning")
                return
            if self.selected_inbox_index >= len(self.inbox_items):
                self.selected_inbox_index = max(0, len(self.inbox_items) - 1)
            item = self.inbox_items[self.selected_inbox_index]
            self._pending_item = item
            self.push_screen(RejectModal(item), self._on_reject)
        else:
            self.notify("Focus the inbox panel first (Tab)", severity="warning")

    def _on_approve(self, comment: str | None) -> None:
        if comment is None:
            return
        item = getattr(self, "_pending_item", None)
        if item is None:
            self.notify("Item no longer available", severity="warning")
            return
        self._pending_cmd = "approve"
        self._pending_comment = comment
        self.run_worker(self._worker_cto, thread=True, exclusive=False)

    def _on_reject(self, comment: str | None) -> None:
        if comment is None:
            return
        item = getattr(self, "_pending_item", None)
        if item is None:
            self.notify("Item no longer available", severity="warning")
            return
        self._pending_cmd = "reject"
        self._pending_comment = comment
        self.run_worker(self._worker_cto, thread=True, exclusive=False)

    def _worker_cto(self) -> None:
        item = getattr(self, "_pending_item", None)
        cmd = getattr(self, "_pending_cmd", "")
        comment = getattr(self, "_pending_comment", "")
        if item is None:
            return
        try:
            team = item["team"]
            issue_id = item["id"]
            tdir = TEAMS_DIR / team
            if cmd == "approve":
                r1 = subprocess.run(["bd", "close", issue_id, "-r", comment], cwd=str(tdir), capture_output=True, text=True, timeout=10.0)
                success = r1.returncode == 0
            elif cmd == "reject":
                r1 = subprocess.run(["bd", "update", issue_id, "--add-label", "status:changes-requested"], cwd=str(tdir), capture_output=True, text=True, timeout=10.0)
                r2 = subprocess.run(["bd", "comment", issue_id, comment], cwd=str(tdir), capture_output=True, text=True, timeout=10.0)
                success = r1.returncode == 0 and r2.returncode == 0
            else:
                success = False
            self.call_from_thread(self._handle_cto_result, success, cmd, issue_id, comment)
        except Exception as exc:
            self.call_from_thread(self._notify_error, f"{cmd} failed: {exc}")

    def _handle_cto_result(self, success: bool, cmd: str, issue_id: str, comment: str) -> None:
        self._pending_item = None
        self._pending_cmd = ""
        self._pending_comment = ""
        if success:
            self.notify(f"{cmd.capitalize()}d {issue_id}", title="CTO Action")
        else:
            self.notify(f"Failed to {cmd} {issue_id}", severity="error")
        self.poll_data()

    def action_freeze(self) -> None:
        focused = self.app.focused
        if focused and focused.id == "inbox":
            if not self.inbox_items:
                self.notify("Inbox is empty", severity="warning")
                return
            item = self.inbox_items[self.selected_inbox_index]
            self._pending_item = item
            self.push_screen(FreezeModal(item), self._on_freeze)
        elif focused and focused.id == "pipeline":
            epic = focused.selected_epic() if hasattr(focused, "selected_epic") else None
            if epic:
                self._pending_item = {"team": epic["team"], "id": epic["epic_id"], "title": epic["title"]}
                self.push_screen(FreezeModal(self._pending_item), self._on_freeze)
        else:
            self.notify("Select an inbox item or epic first", severity="warning")

    def _on_freeze(self, comment: str | None) -> None:
        if comment is None:
            return
        item = getattr(self, "_pending_item", None)
        if item is None:
            self.notify("Item no longer available", severity="warning")
            return
        self._pending_cmd = "freeze"
        self._pending_comment = comment
        self.run_worker(self._worker_freeze, thread=True, exclusive=False)

    def _worker_freeze(self) -> None:
        item = getattr(self, "_pending_item", None)
        comment = getattr(self, "_pending_comment", "")
        if item is None:
            return
        try:
            team = item["team"]
            issue_id = item["id"]
            tdir = TEAMS_DIR / team
            r1 = subprocess.run(["bd", "update", issue_id, "--add-label", "status:frozen"], cwd=str(tdir), capture_output=True, text=True, timeout=10.0)
            r2 = subprocess.run(["bd", "comment", issue_id, comment], cwd=str(tdir), capture_output=True, text=True, timeout=10.0)
            success = r1.returncode == 0 and r2.returncode == 0
            self.call_from_thread(self._handle_freeze_result, success, issue_id, comment)
        except Exception as exc:
            self.call_from_thread(self._notify_error, f"freeze failed: {exc}")

    def _handle_freeze_result(self, success: bool, issue_id: str, comment: str) -> None:
        self._pending_item = None
        self._pending_cmd = ""
        self._pending_comment = ""
        if success:
            self.notify(f"Frozen {issue_id}: {comment}", title="Freeze")
        else:
            self.notify(f"Failed to freeze {issue_id}", severity="error")
        self.poll_data()

    def _notify_error(self, msg: str) -> None:
        self.notify(msg, severity="error", timeout=4)

    def action_kill(self) -> None:
        self.notify("Kill not yet implemented", severity="warning")

    def action_spawn(self) -> None:
        self.notify("Spawn not yet implemented", severity="warning")

    def action_diff(self) -> None:
        self.notify("Diff not yet implemented", severity="warning")

    def action_comment(self) -> None:
        self.notify("Comment not yet implemented", severity="warning")

    def action_cursor_up(self) -> None:
        focused = self.app.focused
        if focused and hasattr(focused, "action_cursor_up"):
            focused.action_cursor_up()

    def action_cursor_down(self) -> None:
        focused = self.app.focused
        if hasattr(focused, "action_cursor_down"):
            focused.action_cursor_down()


# Compat alias — tests and external callers import DashboardV2App.
DashboardV2App = DashboardApp


if __name__ == "__main__":
    app = DashboardApp()
    app.run()
