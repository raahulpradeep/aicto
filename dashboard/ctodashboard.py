#!/usr/bin/env -S uv run --quiet --with textual,rich python
"""Unified CTO Dashboard — observation + control in one TUI.

Screens:
  Overview    — agents, inbox, epic pipeline, activity stream, open tasks
  EpicDetail  — per-epic drill-down
  AgentDetail — per-agent log tail + controls

Quit with q, Q, Esc, or Ctrl-C.
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

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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

from dashboard.notify import notify
from dashboard.widgets.activity_stream import ActivityStream, _format_event
from dashboard.widgets.epic_pipeline import EpicPipeline

TEAMS_DIR = ROOT / "teams"
KEY_POLL_S = 1.0  # poll once per second — fast enough for "real-time" feel
_prev_data: dict[str, tuple] = {}


# ---- shell helpers (copied from top.py) -----------------------------------


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
    """Launch a bd query asynchronously. Caller must call communicate()."""
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
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return []


# ---- data shapers (copied from top.py) ------------------------------------

META_LABELS = {"kind:status-digest", "kind:status-request"}
_ARTIFACT_RE = re.compile(r"^artifact:\s*(.+?)\s+@\s*(.+)$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^branch:\s*(.+)$", re.MULTILINE)
_EPIC_RE = re.compile(r"^epic:\s*(.+)$", re.MULTILINE)


def _resolve_artifact_path(description: str, team: str, labels: list[str]) -> str:
    desc = description or ""
    m = _ARTIFACT_RE.search(desc)
    if m:
        rel_path = m.group(1).strip()
        branch = m.group(2).strip()
    else:
        branch_m = _BRANCH_RE.search(desc)
        branch = branch_m.group(1).strip() if branch_m else ""
        epic_m = _EPIC_RE.search(desc)
        epic_id = epic_m.group(1).strip() if epic_m else ""
        targets = [l for l in labels or [] if l.startswith("target:")]
        target = targets[0].split(":", 1)[1] if targets else ""
        if target == "breakdown" and epic_id:
            rel_path = f"breakdowns/{epic_id}.md"
        elif target == "plan" and epic_id:
            rel_path = f"plans/{epic_id}.md"
        else:
            return "—"
    if not branch:
        return "—"
    worktree_name = branch.split("/")[-1]
    full = TEAMS_DIR / team / ".cto" / "worktrees" / worktree_name / rel_path
    return str(full)


def _kind(labels: list[str]) -> str:
    for lbl in labels or []:
        if lbl.startswith("kind:"):
            return lbl[len("kind:"):]
    return "—"


def _is_meta(labels: list[str]) -> bool:
    return any(lbl in META_LABELS for lbl in labels or [])


def _human_duration(seconds: int) -> str:
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s and m < 10 else f"{m}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    h, _ = divmod(rem, 3600)
    return f"{d}d {h}h" if h else f"{d}d"


def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(issue: dict, now: dt.datetime) -> str:
    created = _parse_iso(issue.get("created_at") or "")
    if not created:
        return "—"
    return _human_duration(int((now - created).total_seconds()))


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ---- per-team gather (synchronous, no thread pools) -----------------------


def _read_team_config(tdir: Path) -> dict:
    cfg = {}
    cfg_path = tdir / ".cto" / "config.yaml"
    try:
        with open(cfg_path) as f:
            for line in f:
                line = line.split("#", 1)[0].rstrip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip().strip('"\'')
                cfg[k] = v
    except OSError:
        pass
    return cfg


def _gather_team(team: str, tdir: Path):
    """Gather data for a single team. Runs in a bg thread; bd calls are parallelised."""
    sess = f"cto-{team}"
    if not tmux_alive(sess):
        return None

    windows = tmux_windows(sess)
    cfg = _read_team_config(tdir)
    provider = cfg.get("agentProvider", "claude")
    model = cfg.get("model", "—")

    # Launch all 3 bd queries in parallel — cuts per-team time from ~6s to ~2s
    # -n 20 for closed is enough (we only show 10 most recent)
    ip_proc = _bd_popen(["list", "--status", "in_progress"], tdir)
    op_proc = _bd_popen(["list", "--status", "open"], tdir)
    cl_proc = _bd_popen(["list", "--status", "closed", "-n", "20"], tdir)

    ip = _read_bd_proc(ip_proc)
    op = _read_bd_proc(op_proc)
    cl = _read_bd_proc(cl_proc)

    ip_by_assignee = {i["assignee"]: i for i in ip if i.get("assignee")}
    agent_rows = [
        {"agent": f"{team}:{w}", "team": team, "window": w,
         "issue": ip_by_assignee.get(f"{team}:{w}"),
         "provider": provider, "model": model}
        for w in windows
    ]

    inbox_rows = [{"team": team, **i} for i in op if "role:cto" in (i.get("labels") or [])]
    open_rows = [
        {"team": team, **i}
        for i in op
        if "role:cto" not in (i.get("labels") or []) and not _is_meta(i.get("labels") or [])
    ]
    closed_rows = [{"team": team, **i} for i in cl]

    # Build pipeline data from the SAME open+in_progress issues (no extra bd calls)
    from dashboard.widgets.epic_pipeline import _compute_stages  # noqa: E402
    epics: list[dict] = []
    by_epic: dict[str, list[dict]] = {}
    for i in (op + ip):
        labels = i.get("labels") or []
        if "kind:epic" in labels or i.get("issue_type") == "epic":
            epics.append(i)
        epic_id = None
        desc = i.get("description", "")
        for line in desc.splitlines():
            if line.startswith("epic:"):
                epic_id = line.split(":", 1)[1].strip()
                break
        if epic_id:
            by_epic.setdefault(epic_id, []).append(i)

    # Also include recently closed children for accurate pipeline stage computation
    for i in cl:
        epic_id = None
        desc = i.get("description", "")
        for line in desc.splitlines():
            if line.startswith("epic:"):
                epic_id = line.split(":", 1)[1].strip()
                break
        if epic_id:
            by_epic.setdefault(epic_id, []).append(i)

    pipelines: list[dict] = []
    for epic in epics:
        epic_id = epic.get("id", "")
        children = by_epic.get(epic_id, [])
        stages = _compute_stages(children)
        pipelines.append({
            "team": team,
            "epic_id": epic_id,
            "title": epic.get("title", ""),
            "created_at": epic.get("created_at", ""),
            "stages": stages,
        })

    return agent_rows, inbox_rows, open_rows, closed_rows, pipelines


def gather_all():
    """Gather ALL dashboard data in one pass per team. Runs in a background thread."""
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


# ---- activity gather (imported from widget, gathered centrally) -----------

from dashboard.telemetry import ActivityLog  # noqa: E402


def gather_events(limit: int = 20):
    """Gather recent activity events from all teams."""
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


# ---- panel builders (copied from top.py) ----------------------------------


def _agent_panel(agents: list[dict], running: list[str], now: dt.datetime, has_focus: bool = False) -> Panel:
    if not running:
        return Panel(
            Text("no running teams — start one with `bin/cto start <team>`", style="dim", justify="center"),
            title="Agents (0)", border_style="bright_cyan" if has_focus else "dim",
        )
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("AGENT", overflow="ellipsis", no_wrap=True)
    t.add_column("PROVIDER", overflow="ellipsis", no_wrap=True)
    t.add_column("MODEL", overflow="ellipsis", no_wrap=True)
    t.add_column("STATUS", overflow="ellipsis", no_wrap=True)
    t.add_column("ISSUE", overflow="ellipsis", no_wrap=True, ratio=2)
    t.add_column("ELAPSED", justify="right", overflow="ellipsis", no_wrap=True)

    for row in agents:
        stale = row.get("stale", False)
        agent = ("~" if stale else "") + row["agent"]
        provider = row.get("provider", "—")
        model = row.get("model", "—")
        issue = row["issue"]
        row_style = "dim" if stale else ""
        if issue:
            started = _parse_iso(issue.get("started_at") or issue.get("updated_at") or "")
            secs = int((now - started).total_seconds()) if started else 0
            elapsed = _fmt_elapsed(max(0, secs)) if started else "—"
            title = _truncate(issue.get("title", ""), 50)
            t.add_row(
                Text(agent, style=row_style), Text(provider, style=row_style),
                Text(model, style=row_style), Text("working", style="green" if not stale else "dim"),
                Text(f"{issue['id']}  {title}", style=row_style),
                Text(elapsed, style="green" if not stale else "dim"),
            )
        else:
            t.add_row(
                Text(agent, style=row_style), Text(provider, style=row_style),
                Text(model, style=row_style), Text("idle", style="dim"),
                Text("—", style="dim"), Text("—", style="dim"),
            )
    return Panel(t, title=f"Agents ({len(agents)})", border_style="bright_cyan" if has_focus else "cyan")


def _inbox_panel(inbox: list[dict], selected_index: int = 0, has_focus: bool = False) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="ellipsis", no_wrap=True)
    t.add_column("ID", overflow="ellipsis", no_wrap=True)
    t.add_column("TITLE", overflow="ellipsis", no_wrap=True, ratio=2)
    t.add_column("ARTIFACT", no_wrap=False, ratio=2)
    t.add_column("LABELS", overflow="ellipsis", no_wrap=True, ratio=1)
    if inbox:
        for idx, row in enumerate(inbox):
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row["team"]
            selected = has_focus and idx == selected_index
            row_style = "reverse" if selected else ("dim" if stale else "")
            labels = ",".join(lbl for lbl in row.get("labels", []) if not lbl.startswith("role:cto"))
            artifact = _resolve_artifact_path(row.get("description", ""), row["team"], row.get("labels", []))
            t.add_row(
                Text(team_disp, style=row_style), Text(row["id"], style=row_style),
                Text(row.get("title", ""), style=row_style),
                Text(artifact, style="cyan" if not stale else "dim"),
                Text(labels, style="dim"),
            )
    else:
        t.add_row(
            Text("—", style="dim"), Text("—", style="dim"),
            Text("(nothing waiting on the CTO)", style="dim"),
            Text("—", style="dim"), Text("—", style="dim"),
        )
    border = "bright_magenta" if has_focus else ("magenta" if inbox else "dim")
    return Panel(t, title=f"CTO inbox ({len(inbox)})", border_style=border)


def _open_panel(open_tasks: list[dict], now: dt.datetime, has_focus: bool = False) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="ellipsis", no_wrap=True)
    t.add_column("ID", overflow="ellipsis", no_wrap=True)
    t.add_column("KIND", overflow="ellipsis", no_wrap=True)
    t.add_column("TITLE", overflow="ellipsis", no_wrap=True, ratio=3)
    t.add_column("ASSIGNEE", overflow="ellipsis", no_wrap=True)
    t.add_column("AGE", overflow="ellipsis", no_wrap=True, justify="right")
    if open_tasks:
        rows = sorted(open_tasks, key=lambda r: (r.get("priority") or 99, r.get("created_at") or ""))
        for row in rows:
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row["team"]
            row_style = "dim" if stale else ""
            assignee = row.get("assignee") or ""
            assignee_disp = Text(assignee, style="green") if assignee else Text("—", style="dim")
            if stale:
                assignee_disp = Text(assignee or "—", style="dim")
            t.add_row(
                Text(team_disp, style=row_style), Text(row.get("id", ""), style=row_style),
                Text(_kind(row.get("labels") or []), style=row_style),
                Text(_truncate(row.get("title", ""), 60), style=row_style),
                assignee_disp, Text(_age(row, now), style=row_style),
            )
    else:
        t.add_row(
            Text("—", style="dim"), Text("—", style="dim"), Text("—", style="dim"),
            Text("(no open tasks)", style="dim"),
            Text("—", style="dim"), Text("—", style="dim"),
        )
    return Panel(t, title=f"Open tasks ({len(open_tasks)})", border_style="bright_blue" if has_focus else ("blue" if open_tasks else "dim"))


def _activity_panel(events: list[dict], has_focus: bool = False) -> Panel:
    t = Table(expand=True, show_header=False, show_lines=False, pad_edge=False)
    t.add_column("TIME", overflow="ellipsis", no_wrap=True, width=8)
    t.add_column("EVENT", overflow="ellipsis", no_wrap=True, ratio=1)

    for ev in events:
        ts_str = ""
        try:
            ts = dt.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
            ts_str = ts.astimezone().strftime("%H:%M:%S")
        except (KeyError, ValueError):
            pass
        text = _format_event(ev)
        t.add_row(Text(ts_str, style="dim"), text)

    if not events:
        t.add_row(Text("—", style="dim"), Text("(no activity yet)", style="dim"))

    border = "bright_blue" if has_focus else "blue"
    return Panel(t, title=f"Activity ({len(events)})", border_style=border)


# ---- modals ---------------------------------------------------------------


class ApproveModal(ModalScreen[str | None]):
    def __init__(self, item: dict) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="approve-dialog"):
            yield Static(f"Approve {self.item['id']}?")
            yield Input(value="LGTM", id="comment")
            with Horizontal():
                yield Button("Approve", variant="success", id="submit")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#comment", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.dismiss(self.query_one("#comment", Input).value)
        else:
            self.dismiss(None)

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)


class RejectModal(ModalScreen[str | None]):
    def __init__(self, item: dict) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="reject-dialog"):
            yield Static(f"Reject {self.item['id']} — comment required:")
            yield TextArea(id="comment", show_line_numbers=False)
            with Horizontal():
                yield Button("Reject", variant="error", id="submit")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#comment", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            comment = self.query_one("#comment", TextArea).text.strip()
            if not comment:
                self.notify("Comment is required", severity="error")
                return
            self.dismiss(comment)
        else:
            self.dismiss(None)

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(None)


# ---- epic detail screen ---------------------------------------------------


class EpicDetailScreen(ModalScreen[None]):
    def __init__(self, pipeline: dict) -> None:
        super().__init__()
        self.pipeline = pipeline

    def compose(self) -> ComposeResult:
        with Vertical(id="epic-detail"):
            yield Static(f"Epic: {self.pipeline['epic_id']}", id="detail-header")
            yield Markdown(self._build_markdown())
            yield Button("Close", id="close")

    def _build_markdown(self) -> str:
        p = self.pipeline
        stages = p["stages"]
        lines = [
            f"# {p['title']}",
            "",
            f"**Team:** {p['team']}  ",
            f"**Epic ID:** {p['epic_id']}  ",
            "",
            "## Pipeline",
            "",
        ]
        for stage, info in stages.items():
            icon = {"done": "✓", "active": "◐", "blocked": "✗", "pending": "●", "none": "○"}.get(info["status"], "?")
            lines.append(f"- **{stage.capitalize()}:** {icon} {info['status']}")
        return "\n".join(lines)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss()


# ---- agent detail screen --------------------------------------------------


class AgentDetailScreen(ModalScreen[None]):
    def __init__(self, agent_row: dict) -> None:
        super().__init__()
        self.agent_row = agent_row

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-detail"):
            yield Static(f"Agent: {self.agent_row['agent']}", id="detail-header")
            yield Markdown(self._build_markdown())
            yield Button("Close", id="close")

    def _build_markdown(self) -> str:
        a = self.agent_row
        lines = [
            f"# {a['agent']}",
            "",
            f"**Provider:** {a.get('provider', '—')}  ",
            f"**Model:** {a.get('model', '—')}  ",
            f"**Status:** {'working' if a.get('issue') else 'idle'}  ",
        ]
        issue = a.get("issue")
        if issue:
            lines.extend([
                "",
                f"**Current issue:** {issue['id']} — {issue.get('title', '')}",
            ])
        # Try to read last lines from agent log
        log_path = TEAMS_DIR / a["team"] / ".cto" / "logs" / f"{a['agent']}.log"
        if log_path.exists():
            try:
                text = log_path.read_text(errors="replace")
                tail = "\n".join(text.splitlines()[-20:])
                lines.extend(["", "## Recent output", "", "```", tail, "```"])
            except OSError:
                pass
        return "\n".join(lines)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss()

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss()


# ---- textual app ----------------------------------------------------------


class FocusableStatic(Static):
    """A Static widget that can receive keyboard focus."""
    can_focus = True


class DashboardApp(App):
    """Unified CTO dashboard."""

    CSS = """
    #top { height: 2fr; }
    #agents { width: 55%; height: 100%; }
    #inbox { width: 45%; height: 100%; }
    #mid { height: 2fr; }
    #pipeline { width: 60%; height: 100%; }
    #activity { width: 40%; height: 100%; }
    #bottom { height: 1fr; }
    #open { width: 100%; height: 100%; }
    #footer { height: 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("Q", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("a", "approve", "Approve"),
        ("r", "reject", "Reject"),
        ("enter", "drill_down", "Drill down"),
        ("up", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("tab", "focus_next", "Next panel"),
    ]

    snapshot = reactive(None)
    gather_ms = reactive(0)
    data_ts = reactive(0.0)
    inbox_items = reactive[list[dict]]([])
    selected_inbox_index = reactive(0)
    _notified_ids: set[str] = set()
    _poll_in_flight: bool = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield FocusableStatic("", id="agents")
            yield FocusableStatic("", id="inbox")
        with Horizontal(id="mid"):
            yield EpicPipeline(id="pipeline")
            yield ActivityStream(id="activity")
        with Horizontal(id="bottom"):
            yield FocusableStatic("", id="open")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self.poll_data()
        self.set_interval(KEY_POLL_S, self.poll_data)
        self.set_interval(KEY_POLL_S, self._tick_footer)

    def poll_data(self) -> None:
        if self._poll_in_flight:
            return
        self._poll_in_flight = True
        self.run_worker(self._poll_worker, thread=True)

    def _poll_worker(self) -> None:
        """Runs in a background thread. Gathers ALL data and pushes to main thread."""
        error_msg: str | None = None
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
            error_msg = str(exc)
            self.app.call_from_thread(self._handle_poll_error, error_msg)
        finally:
            self.app.call_from_thread(self._clear_poll_flag)

    def _apply_data(
        self, agents: list[dict], inbox: list[dict], open_tasks: list[dict],
        closed: list[dict], running: list[str], pipelines: list[dict],
        events: list[dict], gather_ms: int,
    ) -> None:
        self.gather_ms = gather_ms
        self.data_ts = time.monotonic()
        self.snapshot = (agents, inbox, open_tasks, closed, running)
        self.inbox_items = inbox

        # Push pipeline + activity data to their widgets
        try:
            self.query_one("#pipeline", EpicPipeline).set_pipelines(pipelines)
        except Exception:
            pass
        try:
            self.query_one("#activity", ActivityStream).set_events(events)
        except Exception:
            pass

    def _handle_poll_error(self, msg: str) -> None:
        self.notify(f"Poll error: {msg}", severity="error", timeout=2)

    def _clear_poll_flag(self) -> None:
        self._poll_in_flight = False

    def watch_snapshot(self, snapshot) -> None:
        if snapshot is None:
            return
        agents, inbox, open_tasks, closed, running = snapshot
        now = dt.datetime.now(dt.timezone.utc)

        focused = self.app.focused
        agents_focus = focused is not None and focused.id == "agents"
        inbox_focus = focused is not None and focused.id == "inbox"
        open_focus = focused is not None and focused.id == "open"

        try:
            self.query_one("#agents", Static).update(_agent_panel(agents, running, now, has_focus=agents_focus))
        except Exception:
            pass
        try:
            self.query_one("#inbox", Static).update(_inbox_panel(inbox, selected_index=self.selected_inbox_index, has_focus=inbox_focus))
        except Exception:
            pass
        try:
            self.query_one("#open", Static).update(_open_panel(open_tasks, now, has_focus=open_focus))
        except Exception:
            pass

    def _tick_footer(self) -> None:
        data_age_ms = int((time.monotonic() - self.data_ts) * 1000) if self.data_ts else 999999
        fresh = data_age_ms < 3000
        indicator = "● live" if fresh else "○ stale"
        ts = dt.datetime.now().strftime("%H:%M:%S")
        teams_summary = ", ".join(self.snapshot[4] if self.snapshot else [])
        if not teams_summary:
            teams_summary = "—"
        text = Text(
            f"q quit · tab focus · ↑↓ navigate · enter drill-down · a approve · r reject · "
            f"{indicator} · gather {self.gather_ms}ms · teams: {teams_summary} · {ts}",
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
            item = next((i for i in items if i["id"] == iid), None)
            if item:
                notify(
                    "CTO Inbox",
                    f"{item['team']}: {item.get('title', iid)[:60]}",
                )
        self._notified_ids = current_ids

    # -- actions -----------------------------------------------------------

    def action_approve(self) -> None:
        if not self.inbox_items:
            self.notify("Inbox is empty", severity="warning")
            return
        item = self.inbox_items[self.selected_inbox_index]
        self.push_screen(ApproveModal(item), self._on_approve)

    def _on_approve(self, comment: str | None) -> None:
        if comment is None:
            return
        item = self.inbox_items[self.selected_inbox_index]
        self.run_worker(lambda: self._run_cto("approve", item, comment), thread=True)

    def action_reject(self) -> None:
        if not self.inbox_items:
            self.notify("Inbox is empty", severity="warning")
            return
        item = self.inbox_items[self.selected_inbox_index]
        self.push_screen(RejectModal(item), self._on_reject)

    def _on_reject(self, comment: str | None) -> None:
        if comment is None:
            return
        item = self.inbox_items[self.selected_inbox_index]
        self.run_worker(lambda: self._run_cto("reject", item, comment), thread=True)

    def _run_cto(self, cmd: str, item: dict, comment: str) -> None:
        team = item["team"]
        issue_id = item["id"]
        args = [str(ROOT / "bin" / "cto"), cmd, team, issue_id, "--comment", comment]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30.0)
        self.call_from_thread(self._handle_cto_result, cmd, result)

    def _handle_cto_result(self, cmd: str, result: subprocess.CompletedProcess) -> None:
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "unknown error"
            self.notify(f"{cmd} failed: {err}", severity="error", title="Error")
        else:
            self.notify(f"{cmd}d successfully", title="Done")
            self.poll_data()

    def action_drill_down(self) -> None:
        focused = self.app.focused
        if focused is None:
            return

        fid = focused.id

        # Epic Pipeline → epic detail
        if fid == "pipeline":
            epic = focused.selected_epic() if hasattr(focused, "selected_epic") else None
            if epic:
                self.push_screen(EpicDetailScreen(epic))
            return

        # Agents panel → agent detail (first agent for now)
        if fid == "agents":
            snapshot = self.snapshot
            if snapshot:
                agents = snapshot[0]
                if agents:
                    self.push_screen(AgentDetailScreen(agents[0]))
            return

        # Inbox panel → approve/reject for selected item
        if fid == "inbox":
            if self.inbox_items:
                item = self.inbox_items[self.selected_inbox_index]
                self.push_screen(EpicDetailScreen({
                    "team": item["team"],
                    "epic_id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "stages": {},
                }))
            return

    def action_cursor_up(self) -> None:
        focused = self.app.focused
        if focused and focused.id == "inbox":
            self.selected_inbox_index = max(0, self.selected_inbox_index - 1)
        elif focused and focused.id == "pipeline":
            focused.selected_index = max(0, focused.selected_index - 1)

    def action_cursor_down(self) -> None:
        focused = self.app.focused
        if focused and focused.id == "inbox":
            self.selected_inbox_index = min(len(self.inbox_items) - 1, self.selected_inbox_index + 1)
        elif focused and focused.id == "pipeline":
            max_idx = len(focused.pipelines) - 1 if hasattr(focused, "pipelines") else 0
            focused.selected_index = min(max_idx, focused.selected_index + 1)

    def action_focus_next(self) -> None:
        self.screen.focus_next()


def main() -> int:
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
