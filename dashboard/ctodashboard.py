#!/usr/bin/env -S uv run --quiet --with textual,rich python
"""Unified CTO Dashboard — observation + control in one TUI.

Screens:
  Overview    — agents, inbox, epic pipeline, activity stream, open tasks
  EpicDetail  — per-epic drill-down (Phase 3)
  AgentDetail — per-agent log tail + controls (Phase 3)

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import concurrent.futures
import threading

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
from dashboard.widgets.activity_stream import ActivityStream
from dashboard.widgets.epic_pipeline import EpicPipeline
TEAMS_DIR = ROOT / "teams"
KEY_POLL_S = 0.25
_POOL = ThreadPoolExecutor(max_workers=12)
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


# ---- per-team gather (copied from top.py) ---------------------------------


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
    sess = f"cto-{team}"
    if not tmux_alive(sess):
        return None

    windows = tmux_windows(sess)
    cfg = _read_team_config(tdir)
    provider = cfg.get("agentProvider", "claude")
    model = cfg.get("model", "—")

    f_ip = _POOL.submit(_bd_json, ["list", "--status", "in_progress"], tdir)
    f_open = _POOL.submit(_bd_json, ["list", "--status", "open"], tdir)
    f_closed = _POOL.submit(_bd_json, ["list", "--status", "closed", "-n", "0"], tdir)
    ip = f_ip.result()
    op = f_open.result()
    cl = f_closed.result()

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
    return agent_rows, inbox_rows, open_rows, closed_rows


def gather():
    agents: list[dict] = []
    inbox: list[dict] = []
    open_tasks: list[dict] = []
    closed: list[dict] = []
    running: list[str] = []

    if not TEAMS_DIR.is_dir():
        return agents, inbox, open_tasks, closed, running

    teams = [p for p in sorted(TEAMS_DIR.iterdir()) if p.is_dir() and (p / ".cto").is_dir()]
    live_team_names = {t.name for t in teams}
    for gone in list(_prev_data.keys()):
        if gone not in live_team_names:
            del _prev_data[gone]

    futures = {team.name: _POOL.submit(_gather_team, team.name, team) for team in teams}
    for team_name, fut in futures.items():
        result = None
        try:
            result = fut.result(timeout=10.0)
        except (concurrent.futures.TimeoutError, Exception):
            pass

        if result is None:
            cached = _prev_data.get(team_name)
            if cached is None:
                continue
            a_rows, i_rows, o_rows, c_rows = cached
            a_rows = [{**r, "stale": True} for r in a_rows]
            i_rows = [{**r, "stale": True} for r in i_rows]
            o_rows = [{**r, "stale": True} for r in o_rows]
            c_rows = [{**r, "stale": True} for r in c_rows]
        else:
            a_rows, i_rows, o_rows, c_rows = result
            _prev_data[team_name] = result

        running.append(team_name)
        agents.extend(a_rows)
        inbox.extend(i_rows)
        open_tasks.extend(o_rows)
        closed.extend(c_rows)

    closed.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
    closed_recent = closed[:10]
    return agents, inbox, open_tasks, closed_recent, running


# ---- panel builders (copied from top.py) ----------------------------------


def _agent_panel(agents: list[dict], running: list[str], now: dt.datetime) -> Panel:
    if not running:
        return Panel(
            Text("no running teams — start one with `bin/cto start <team>`", style="dim", justify="center"),
            title="Agents (0)", border_style="dim",
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
    return Panel(t, title=f"Agents ({len(agents)})", border_style="cyan")


def _inbox_panel(inbox: list[dict]) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="ellipsis", no_wrap=True)
    t.add_column("ID", overflow="ellipsis", no_wrap=True)
    t.add_column("TITLE", overflow="ellipsis", no_wrap=True, ratio=2)
    t.add_column("ARTIFACT", no_wrap=False, ratio=2)
    t.add_column("LABELS", overflow="ellipsis", no_wrap=True, ratio=1)
    if inbox:
        for row in inbox:
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row["team"]
            row_style = "dim" if stale else ""
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
    return Panel(t, title=f"CTO inbox ({len(inbox)})", border_style="magenta" if inbox else "dim")


def _open_panel(open_tasks: list[dict], now: dt.datetime) -> Panel:
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
    return Panel(t, title=f"Open tasks ({len(open_tasks)})", border_style="blue" if open_tasks else "dim")


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
    ]

    snapshot = reactive(None)
    gather_ms = reactive(0)
    data_ts = reactive(0.0)
    inbox_items = reactive[list[dict]]([])
    selected_inbox_index = reactive(0)
    _notified_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("", id="agents")
            yield Static("", id="inbox")
        with Horizontal(id="mid"):
            yield EpicPipeline(id="pipeline")
            yield ActivityStream(id="activity")
        with Horizontal(id="bottom"):
            yield Static("", id="open")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._gather_lock = threading.Lock()
        self.poll_data()
        self.set_interval(KEY_POLL_S, self.poll_data)
        self.set_interval(KEY_POLL_S, self._tick_footer)

    def poll_data(self) -> None:
        if self._gather_lock.acquire(blocking=False):
            self.run_worker(self._poll_worker, thread=True)

    def _poll_worker(self) -> None:
        try:
            t0 = time.monotonic()
            agents, inbox, open_tasks, closed, running = gather()
            gather_ms = int((time.monotonic() - t0) * 1000)
            self.call_from_thread(
                self._apply_data,
                agents, inbox, open_tasks, closed, running, gather_ms, time.monotonic(),
            )
        finally:
            self._gather_lock.release()

    def _apply_data(
        self, agents: list[dict], inbox: list[dict], open_tasks: list[dict],
        closed: list[dict], running: list[str], gather_ms: int, data_ts: float,
    ) -> None:
        self.gather_ms = gather_ms
        self.data_ts = data_ts
        self.snapshot = (agents, inbox, open_tasks, closed, running, gather_ms, data_ts)
        self.inbox_items = inbox

    def watch_snapshot(self, snapshot) -> None:
        if snapshot is None:
            return
        agents, inbox, open_tasks, closed, running, _gather_ms, _data_ts = snapshot
        now = dt.datetime.now(dt.timezone.utc)

        prev = getattr(self, "_prev_snapshot", None)
        self.query_one("#agents", Static).update(_agent_panel(agents, running, now))
        if prev is None or prev[1] != inbox:
            self.query_one("#inbox", Static).update(_inbox_panel(inbox))
        self.query_one("#open", Static).update(_open_panel(open_tasks, now))

        self._prev_snapshot = snapshot

    def _tick_footer(self) -> None:
        data_age_ms = int((time.monotonic() - self.data_ts) * 1000) if self.data_ts else 999999
        fresh = data_age_ms < 2000
        indicator = "● live" if fresh else "○ stale"
        ts = dt.datetime.now().strftime("%H:%M:%S")
        teams_summary = ", ".join(self.snapshot[4] if self.snapshot else [])
        if not teams_summary:
            teams_summary = "—"
        text = Text(
            f"q quit · a approve · r reject · enter drill-down · {indicator} · "
            f"gather {self.gather_ms}ms · teams: {teams_summary} · {ts}",
            style="dim", justify="center",
        )
        self.query_one("#footer", Static).update(text)

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
        # Drill down into epic pipeline or agent detail depending on focus.
        # For now, if there are inbox items, open the selected one.
        # Future: detect which panel has focus.
        if self.inbox_items:
            item = self.inbox_items[self.selected_inbox_index]
            self.push_screen(EpicDetailScreen({
                "team": item["team"],
                "epic_id": item.get("id", ""),
                "title": item.get("title", ""),
                "stages": {},
            }))


def main() -> int:
    DashboardApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
