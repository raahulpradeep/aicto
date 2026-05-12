#!/usr/bin/env -S uv run --quiet --with textual,rich python
"""Live `top`-style dashboard for the AI CTO workspace.

Four panels:
  1. Agents       — one row per tmux window across every running team.
  2. CTO inbox    — open issues with role:cto across all teams.
  3. Open tasks   — open work nobody's holding (excludes role:cto + status meta).
  4. Recently closed — last 5 closures across all teams.

Quit with q, Q, Esc, or Ctrl-C. Read-only.

Known limitation: `bd list --status closed --json` doesn't include the
`assignee` or `started_at` fields, so:
  * "CLOSED-BY" is best-effort: derived from labels (verdict:approved →
    "CTO", or the `role:*` label).
  * "RESOLVE-TIME" is full lifecycle (`closed_at - created_at`), not
    claim-to-close.
"""
from __future__ import annotations
from typing import Optional

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

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Static

ROOT = Path(__file__).resolve().parent.parent
TEAMS_DIR = ROOT / "teams"
KEY_POLL_S = 0.25  # how often the gather worker is re-triggered
RECENT_CLOSED_LIMIT = 10
RECENT_CLOSED_SCAN_PER_TEAM = 0  # 0 = unlimited; bd ordering is not chronological, so we fetch all and sort in Python
# Module-level executor reused across refreshes — bd subprocesses are
# expensive (each invocation cold-starts dolt), so we run the per-team
# queries in parallel.
_POOL = ThreadPoolExecutor(max_workers=12)

_prev_data: dict[str, tuple] = {}


# ---- shell helpers --------------------------------------------------------


def _run(cmd: list[str], cwd: Optional[Path] = None, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def tmux_alive(session: str) -> bool:
    return (
        subprocess.call(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


def tmux_windows(session: str) -> list[str]:
    out = _run(["tmux", "list-windows", "-t", session, "-F", "#W"])
    return [line.strip() for line in out.splitlines() if line.strip()]


# ---- bd query -------------------------------------------------------------


def _bd_json(args: list[str], cwd: Path) -> list:
    try:
        return json.loads(_run(["bd", *args, "--json"], cwd=cwd) or "[]")
    except json.JSONDecodeError:
        return []


# ---- data shapers ---------------------------------------------------------


META_LABELS = {"kind:status-digest", "kind:status-request"}

# Extract artifact path from issue description.
# Format: "artifact: <rel-path> @ <branch>"  (branch may be "task/...", "manager/...", etc.)
_ARTIFACT_RE = re.compile(r"^artifact:\s*(.+?)\s+@\s*(.+)$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^branch:\s*(.+)$", re.MULTILINE)
_EPIC_RE = re.compile(r"^epic:\s*(.+)$", re.MULTILINE)


def _resolve_artifact_path(description: str, team: str, labels: list[str]) -> str:
    """Return the absolute filesystem path to the artifact, or '—'."""
    desc = description or ""

    # 1. Try explicit artifact: line (includes branch context after @)
    m = _ARTIFACT_RE.search(desc)
    if m:
        rel_path = m.group(1).strip()
        branch = m.group(2).strip()
    else:
        # 2. No artifact: line — infer from branch + target
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

    # Worktree directory name is the last path component of the branch
    # e.g. "task/health-kb-sdg" → "health-kb-sdg"
    worktree_name = branch.split("/")[-1]
    full = TEAMS_DIR / team / ".cto" / "worktrees" / worktree_name / rel_path
    return str(full)


def _kind(labels: list[str]) -> str:
    for lbl in labels or []:
        if lbl.startswith("kind:"):
            return lbl[len("kind:") :]
    return "—"


def _is_meta(labels: list[str]) -> bool:
    return any(lbl in META_LABELS for lbl in labels or [])


def _classify_closed(issue: dict) -> str:
    """Best-effort closer identification for a closed bd issue.

    bd doesn't track an explicit closer; we fall back to label hints.
    """
    labels = issue.get("labels") or []
    if "verdict:approved" in labels or "verdict:rejected" in labels:
        return "CTO"
    for lbl in labels:
        if lbl.startswith("role:") and lbl != "role:cto":
            return lbl[len("role:") :]
    return "—"


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


def _parse_iso(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_time(issue: dict) -> str:
    end = _parse_iso(issue.get("closed_at") or "")
    start = _parse_iso(issue.get("started_at") or "") or _parse_iso(issue.get("created_at") or "")
    if not (end and start):
        return "—"
    return _human_duration(int((end - start).total_seconds()))


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


# ---- per-team gather ------------------------------------------------------


def _read_team_config(tdir: Path) -> dict:
    """Parse .cto/config.yaml for agentProvider and model."""
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


def _doing_for_team(team: str, tdir: Path, now: dt.datetime, stale_secs: int = 120) -> dict:
    """Read the tail of `<tdir>/.cto/activity.jsonl` and return the latest
    activity per slot. Returns `{slot: {"summary": str, "kind": str, "stale": bool}}`.
    """
    log = tdir / ".cto" / "activity.jsonl"
    if not log.exists():
        return {}
    try:
        size = log.stat().st_size
        with open(log, "rb") as f:
            if size > 1_000_000:
                f.seek(size - 1_000_000)
                f.readline()  # discard partial first line
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    out: dict = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        slot = row.get("slot") or ""
        if not slot and isinstance(row.get("agent"), str) and ":" in row["agent"]:
            slot = row["agent"].split(":", 1)[1]
        if not slot:
            continue
        out[slot] = {
            "summary": row.get("summary") or row.get("event") or row.get("kind") or "",
            "kind": row.get("kind") or row.get("event") or "",
            "ts": row.get("ts", ""),
        }
    for slot, info in out.items():
        ts = _parse_iso(info.get("ts", ""))
        info["stale"] = bool(ts and (now - ts).total_seconds() > stale_secs)
    return out


def _model_for_slot(slot: str, manager_model: str, reviewer_model: str, dev_model: str) -> str:
    """Mirror bin/cto's per-role model assignment for dashboard display."""
    if slot == "manager":
        return manager_model
    if slot.startswith("review"):
        return reviewer_model
    if slot.startswith("dev"):
        return dev_model
    return "—"


def _gather_team(team: str, tdir: Path):
    """Return (agent_rows, inbox_rows, open_rows, closed_rows) or None.

    Three bd queries fired in parallel; the broader `--status open`
    query covers both the CTO inbox and the Open Tasks panels.
    """
    sess = f"cto-{team}"
    if not tmux_alive(sess):
        return None

    windows = tmux_windows(sess)
    cfg = _read_team_config(tdir)
    provider = cfg.get("agentProvider", "claude")
    dev_model = cfg.get("model", "—")
    manager_model = cfg.get("managerModel", dev_model)
    reviewer_model = cfg.get("reviewerModel", dev_model)

    doing = _doing_for_team(team, tdir, dt.datetime.now(dt.timezone.utc))

    f_ip = _POOL.submit(_bd_json, ["list", "--status", "in_progress"], tdir)
    f_open = _POOL.submit(_bd_json, ["list", "--status", "open"], tdir)
    f_closed = _POOL.submit(
        _bd_json,
        ["list", "--status", "closed", "-n", str(RECENT_CLOSED_SCAN_PER_TEAM)],
        tdir,
    )
    ip = f_ip.result()
    op = f_open.result()
    cl = f_closed.result()

    ip_by_assignee = {i["assignee"]: i for i in ip if i.get("assignee")}
    agent_rows = [
        {
            "agent": f"{team}:{w}",
            "team": team,
            "window": w,
            "issue": ip_by_assignee.get(f"{team}:{w}"),
            "provider": provider,
            "model": _model_for_slot(w, manager_model, reviewer_model, dev_model),
            "doing": (doing.get(w) or {}).get("summary", ""),
            "doing_kind": (doing.get(w) or {}).get("kind", ""),
            "doing_stale": bool((doing.get(w) or {}).get("stale")),
        }
        for w in windows
    ]

    inbox_rows = [
        {"team": team, **i} for i in op if "role:cto" in (i.get("labels") or [])
    ]
    open_rows = [
        {"team": team, **i}
        for i in op
        if "role:cto" not in (i.get("labels") or []) and not _is_meta(i.get("labels") or [])
    ]
    closed_rows = [{"team": team, **i} for i in cl]
    return agent_rows, inbox_rows, open_rows, closed_rows


def gather():
    """Return (agents, inbox, open_tasks, closed_recent, running).

    Fetch is intentionally separated from render so every panel sees a
    consistent snapshot — render() is never called with partially-gathered data.
    """
    agents: list[dict] = []
    inbox: list[dict] = []
    open_tasks: list[dict] = []
    closed: list[dict] = []
    running: list[str] = []

    if not TEAMS_DIR.is_dir():
        return agents, inbox, open_tasks, closed, running

    teams = [
        p for p in sorted(TEAMS_DIR.iterdir()) if p.is_dir() and (p / ".cto").is_dir()
    ]
    live_team_names = {t.name for t in teams}
    # Evict stale-cache entries for teams that no longer exist on disk.
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
            # Mark all rows from cache as stale so panels can signal the user.
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

    # sort closed across teams by closed_at desc, take top N
    closed.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
    closed_recent = closed[:RECENT_CLOSED_LIMIT]

    return agents, inbox, open_tasks, closed_recent, running


# ---- panel builders -------------------------------------------------------


def _agent_panel(agents: list[dict], running: list[str], now: dt.datetime) -> Panel:
    if not running:
        return Panel(
            Text(
                "no running teams — start one with `bin/cto start <team>`",
                style="dim",
                justify="center",
            ),
            title="Agents (0)",
            border_style="dim",
        )

    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("AGENT", overflow="ellipsis", no_wrap=True)
    t.add_column("PROVIDER", overflow="ellipsis", no_wrap=True)
    t.add_column("MODEL", overflow="ellipsis", no_wrap=True)
    t.add_column("STATUS", overflow="ellipsis", no_wrap=True)
    t.add_column("ISSUE", overflow="ellipsis", no_wrap=True, ratio=2)
    t.add_column("DOING", overflow="ellipsis", no_wrap=True, ratio=2)
    t.add_column("ELAPSED", justify="right", overflow="ellipsis", no_wrap=True)

    for row in agents:
        stale = row.get("stale", False)
        agent = ("~" if stale else "") + row["agent"]
        provider = row.get("provider", "—")
        model = row.get("model", "—")
        issue = row["issue"]
        row_style = "dim" if stale else ""
        doing_text = _truncate(row.get("doing", "") or "", 60)
        doing_style = "dim" if (row.get("doing_stale") or stale or not doing_text) else ""
        doing_display = doing_text or "—"
        if issue:
            started = _parse_iso(issue.get("started_at") or issue.get("updated_at") or "")
            secs = int((now - started).total_seconds()) if started else 0
            elapsed = _fmt_elapsed(max(0, secs)) if started else "—"
            title = _truncate(issue.get("title", ""), 50)
            t.add_row(
                Text(agent, style=row_style),
                Text(provider, style=row_style),
                Text(model, style=row_style),
                Text("working", style="green" if not stale else "dim"),
                Text(f"{issue['id']}  {title}", style=row_style),
                Text(doing_display, style=doing_style or row_style),
                Text(elapsed, style="green" if not stale else "dim"),
            )
        else:
            t.add_row(
                Text(agent, style=row_style),
                Text(provider, style=row_style),
                Text(model, style=row_style),
                Text("idle", style="dim"),
                Text("—", style="dim"),
                Text(doing_display, style=doing_style or "dim"),
                Text("—", style="dim"),
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
            labels = ",".join(
                lbl for lbl in row.get("labels", []) if not lbl.startswith("role:cto")
            )
            artifact = _resolve_artifact_path(
                row.get("description", ""), row["team"], row.get("labels", [])
            )
            t.add_row(
                Text(team_disp, style=row_style),
                Text(row["id"], style=row_style),
                Text(row.get("title", ""), style=row_style),
                Text(artifact, style="cyan" if not stale else "dim"),
                Text(labels, style="dim"),
            )
    else:
        t.add_row(
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("(nothing waiting on the CTO)", style="dim"),
            Text("—", style="dim"),
            Text("—", style="dim"),
        )
    return Panel(
        t,
        title=f"CTO inbox ({len(inbox)})",
        border_style="magenta" if inbox else "dim",
    )


def _open_panel(open_tasks: list[dict], now: dt.datetime) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="ellipsis", no_wrap=True)
    t.add_column("ID", overflow="ellipsis", no_wrap=True)
    t.add_column("KIND", overflow="ellipsis", no_wrap=True)
    t.add_column("TITLE", overflow="ellipsis", no_wrap=True, ratio=3)
    t.add_column("ASSIGNEE", overflow="ellipsis", no_wrap=True)
    t.add_column("AGE", overflow="ellipsis", no_wrap=True, justify="right")
    if open_tasks:
        # Stable sort: priority asc (lower=more urgent), then created_at asc.
        rows = sorted(
            open_tasks,
            key=lambda r: (r.get("priority") or 99, r.get("created_at") or ""),
        )
        for row in rows:
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row["team"]
            row_style = "dim" if stale else ""
            assignee = row.get("assignee") or ""
            if stale:
                assignee_disp = Text(assignee or "—", style="dim")
            else:
                assignee_disp = Text(assignee, style="green") if assignee else Text("—", style="dim")
            t.add_row(
                Text(team_disp, style=row_style),
                Text(row.get("id", ""), style=row_style),
                Text(_kind(row.get("labels") or []), style=row_style),
                Text(_truncate(row.get("title", ""), 60), style=row_style),
                assignee_disp,
                Text(_age(row, now), style=row_style),
            )
    else:
        t.add_row(
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text("(no open tasks)", style="dim"),
            Text("—", style="dim"),
            Text("—", style="dim"),
        )
    return Panel(
        t,
        title=f"Open tasks ({len(open_tasks)})",
        border_style="blue" if open_tasks else "dim",
    )


def _closed_panel(closed: list[dict]) -> Panel:
    t = Table(expand=True, show_lines=False, header_style="bold", pad_edge=False)
    t.add_column("TEAM", overflow="ellipsis", no_wrap=True)
    t.add_column("ID", overflow="ellipsis", no_wrap=True)
    t.add_column("KIND", overflow="ellipsis", no_wrap=True)
    t.add_column("TITLE", overflow="ellipsis", no_wrap=True, ratio=3)
    t.add_column("CLOSED-BY", overflow="ellipsis", no_wrap=True)
    t.add_column("RESOLVE", overflow="ellipsis", no_wrap=True, justify="right")
    t.add_column("AT", overflow="ellipsis", no_wrap=True)
    if closed:
        for row in closed:
            stale = row.get("stale", False)
            team_disp = ("~" if stale else "") + row["team"]
            row_style = "dim" if stale else ""
            closed_at = _parse_iso(row.get("closed_at") or "")
            at_str = closed_at.astimezone().strftime("%H:%M:%S") if closed_at else "—"
            closer = _classify_closed(row)
            closer_style = "dim" if stale else ("green" if closer == "CTO" else "yellow")
            t.add_row(
                Text(team_disp, style=row_style),
                Text(row.get("id", ""), style=row_style),
                Text(_kind(row.get("labels") or []), style=row_style),
                Text(_truncate(row.get("title", ""), 50), style=row_style),
                Text(closer, style=closer_style),
                Text(_resolve_time(row), style=row_style),
                Text(at_str, style=row_style),
            )
    else:
        t.add_row(
            *[Text("—", style="dim")] * 6,
            Text("(no closures yet)", style="dim"),
        )
    return Panel(
        t,
        title=f"Recently closed ({len(closed)})",
        border_style="green" if closed else "dim",
    )


# ---- textual app ----------------------------------------------------------


class TopApp(App):
    """Textual TUI for the CTO top dashboard."""

    CSS = """
    #top { height: 2fr; }
    #agents { width: 60%; height: 100%; }
    #inbox { width: 40%; height: 100%; }
    #open { height: 1fr; }
    #closed { height: 1fr; }
    #footer { height: 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("Q", "quit", "Quit"),
        ("escape", "quit", "Quit"),
    ]

    snapshot = reactive(None)
    gather_ms = reactive(0)
    data_ts = reactive(0.0)

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("", id="agents")
            yield Static("", id="inbox")
        yield Static("", id="open")
        yield Static("", id="closed")
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
                agents,
                inbox,
                open_tasks,
                closed,
                running,
                gather_ms,
                time.monotonic(),
            )
        finally:
            self._gather_lock.release()

    def _apply_data(
        self,
        agents: list[dict],
        inbox: list[dict],
        open_tasks: list[dict],
        closed: list[dict],
        running: list[str],
        gather_ms: int,
        data_ts: float,
    ) -> None:
        self.gather_ms = gather_ms
        self.data_ts = data_ts
        self.snapshot = (agents, inbox, open_tasks, closed, running, gather_ms, data_ts)

    def watch_snapshot(self, snapshot) -> None:
        if snapshot is None:
            return
        agents, inbox, open_tasks, closed, running, _gather_ms, _data_ts = snapshot
        now = dt.datetime.now(dt.timezone.utc)

        prev = getattr(self, "_prev_snapshot", None)
        # Always re-render: ELAPSED column is computed from `now` at render time.
        self.query_one("#agents", Static).update(_agent_panel(agents, running, now))
        if prev is None or prev[1] != inbox:
            self.query_one("#inbox", Static).update(_inbox_panel(inbox))
        # Always re-render: AGE column is computed from `now` at render time.
        self.query_one("#open", Static).update(_open_panel(open_tasks, now))
        if prev is None or prev[3] != closed:
            self.query_one("#closed", Static).update(_closed_panel(closed))

        self._prev_snapshot = snapshot

    def _tick_footer(self) -> None:
        data_age_ms = (
            int((time.monotonic() - self.data_ts) * 1000)
            if self.data_ts
            else 999999
        )
        fresh = data_age_ms < 2000
        indicator = "● live" if fresh else "○ stale"
        ts = dt.datetime.now().strftime("%H:%M:%S")
        teams_summary = ", ".join(self.snapshot[4] if self.snapshot else [])
        if not teams_summary:
            teams_summary = "—"
        text = Text(
            f"q quit · {indicator} · gather {self.gather_ms}ms · teams: {teams_summary} · {ts}",
            style="dim",
            justify="center",
        )
        self.query_one("#footer", Static).update(text)


# ---- main -----------------------------------------------------------------


def main() -> int:
    TopApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
