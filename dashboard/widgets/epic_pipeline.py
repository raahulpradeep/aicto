"""Epic pipeline widget for the unified CTO dashboard.

Shows one swimlane per open epic with columns for each workflow stage:
Epic → Breakdown → Plan → Dev → Review → Merge → Ship.

This widget is PASSIVE — the parent DashboardApp gathers data in a single
background worker and pushes it here via set_pipelines().  This eliminates
the thread-pool deadlock and overlapping-poll problems that caused blanking.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Optional, Any

from rich.table import Table
from rich.text import Text

from textual.reactive import reactive
from textual.widgets import Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"


def _run(cmd: list[str], cwd: Path, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _bd_json(args: list[str], cwd: Path) -> list[dict[str, Any]]:
    try:
        return json.loads(_run(["bd", *args, "--json"], cwd) or "[]")
    except json.JSONDecodeError:
        return []


def _parse_iso(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _human_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m"
    if seconds < 86400:
        return f"{seconds//3600}h"
    return f"{seconds//86400}d"


class EpicPipeline(Static):
    """Rich widget showing epic swimlanes.

    PASSIVE: the parent DashboardApp pushes data via set_pipelines().
    """

    pipelines = reactive[list[dict[str, Any]]]([])
    selected_index = reactive(0)
    can_focus = True

    def set_pipelines(self, pipelines: list[dict[str, Any]]) -> None:
        """Called by the parent app after gathering data."""
        self.pipelines = pipelines
        if self.selected_index >= len(self.pipelines):
            self.selected_index = max(0, len(self.pipelines) - 1)
        self.border_title = f"Epic Pipeline ({len(self.pipelines)})"

    def render(self) -> Table:
        try:
            t = Table(
                expand=True,
                show_lines=False,
                header_style="bold",
                pad_edge=False,
                box=None,
            )
            t.add_column("TEAM", overflow="ellipsis", no_wrap=True, width=10)
            t.add_column("EPIC", overflow="ellipsis", no_wrap=True, ratio=2)
            t.add_column("BRKDN", justify="center", width=7)
            t.add_column("PLAN", justify="center", width=7)
            t.add_column("DEV", justify="center", width=7)
            t.add_column("REVIEW", justify="center", width=7)
            t.add_column("MERGE", justify="center", width=7)
            t.add_column("SHIP", justify="center", width=7)
            t.add_column("AGE", justify="right", width=6)

            now = dt.datetime.now(dt.timezone.utc)
            for idx, pipe in enumerate(self.pipelines):
                row = _render_pipeline_row(pipe, now, selected=(idx == self.selected_index))
                t.add_row(*row)

            if not self.pipelines:
                t.add_row(
                    Text("—", style="dim"),
                    Text("(no open epics)", style="dim"),
                    *[Text("—", style="dim")] * 7,
                )

            return t
        except Exception:
            return Table(title="(error rendering pipeline)")

    def on_key(self, event) -> None:
        if not self.pipelines:
            return
        if event.key == "up":
            self.selected_index = max(0, self.selected_index - 1)
        elif event.key == "down":
            self.selected_index = min(len(self.pipelines) - 1, self.selected_index + 1)

    def selected_epic(self) -> Optional[dict[str, Any]]:
        if not self.pipelines or self.selected_index >= len(self.pipelines):
            return None
        return self.pipelines[self.selected_index]


def _render_pipeline_row(pipe: dict[str, Any], now: dt.datetime, selected: bool = False) -> list[Text]:
    team = pipe["team"]
    epic_title = pipe["title"][:30]
    stages = pipe["stages"]

    created = _parse_iso(pipe.get("created_at", ""))
    age = _human_age(int((now - created).total_seconds())) if created else "—"

    def cell(stage: str) -> Text:
        s = stages.get(stage, {"status": "none"})
        status = s["status"]
        if status == "done":
            return Text("✓", style="green")
        if status == "active":
            return Text("◐", style="yellow")
        if status == "blocked":
            return Text("✗", style="red")
        if status == "pending":
            return Text("●", style="dim")
        return Text("—", style="dim")

    sel = " reverse" if selected else ""
    return [
        Text(team, style="cyan" + sel),
        Text(epic_title, style="white" + sel),
        cell("breakdown"),
        cell("plan"),
        cell("dev"),
        cell("review"),
        cell("merge"),
        cell("ship"),
        Text(age, style="dim"),
    ]


def _bd_popen(args: list[str], cwd: Path) -> subprocess.Popen:
    """Launch a bd query asynchronously."""
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


def _gather_team_pipelines(
    team_dir: Path,
    open_issues: Optional[list[dict[str, Any]]] = None,
    in_progress_issues: Optional[list[dict[str, Any]]] = None,
    closed_issues: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Gather epic pipeline data for a single team.

    If callers pass pre-fetched issue lists, we skip the subprocess fan-out
    entirely (used by the dashboard to dedup `bd list` calls). Otherwise we
    parallelise the three bd queries as a fallback for standalone use.
    """
    if open_issues is None or in_progress_issues is None or closed_issues is None:
        open_proc = _bd_popen(["list", "--status", "open"], team_dir)
        ip_proc = _bd_popen(["list", "--status", "in_progress"], team_dir)
        closed_proc = _bd_popen(["list", "--status", "closed", "--limit", "50"], team_dir)
        if open_issues is None:
            open_issues = _read_bd_proc(open_proc)
        else:
            try: open_proc.kill()
            except Exception: pass
        if in_progress_issues is None:
            in_progress_issues = _read_bd_proc(ip_proc)
        else:
            try: ip_proc.kill()
            except Exception: pass
        if closed_issues is None:
            closed_issues = _read_bd_proc(closed_proc)
        else:
            try: closed_proc.kill()
            except Exception: pass

    issues = list(open_issues) + list(in_progress_issues)

    epics: list[dict[str, Any]] = []
    by_epic: dict[str, list[dict[str, Any]]] = {}

    for i in issues:
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

    for i in closed_issues:
        epic_id = None
        desc = i.get("description", "")
        for line in desc.splitlines():
            if line.startswith("epic:"):
                epic_id = line.split(":", 1)[1].strip()
                break
        if epic_id:
            by_epic.setdefault(epic_id, []).append(i)

    pipelines: list[dict[str, Any]] = []
    for epic in epics:
        epic_id = epic.get("id", "")
        children = by_epic.get(epic_id, [])
        stages = _compute_stages(children)
        pipelines.append({
            "team": team_dir.name,
            "epic_id": epic_id,
            "title": epic.get("title", ""),
            "created_at": epic.get("created_at", ""),
            "stages": stages,
        })
    return pipelines


def _compute_stages(children: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return stage statuses for an epic's children."""
    stages = {
        "breakdown": {"status": "none"},
        "plan": {"status": "none"},
        "dev": {"status": "none"},
        "review": {"status": "none"},
        "merge": {"status": "none"},
        "ship": {"status": "none"},
    }

    breakdowns = [c for c in children if "kind:breakdown" in (c.get("labels") or [])]
    plans = [c for c in children if "kind:plan" in (c.get("labels") or [])]
    devs = [c for c in children if "kind:dev" in (c.get("labels") or [])]
    reviews = [c for c in children if "kind:review" in (c.get("labels") or [])]
    merges = [c for c in children if "kind:merge" in (c.get("labels") or [])]
    epic_merges = [c for c in children if "kind:merge" in (c.get("labels") or []) and "target:epic" in (c.get("labels") or [])]

    if any(c.get("status") == "closed" for c in breakdowns):
        stages["breakdown"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in breakdowns):
        stages["breakdown"]["status"] = "active"
    else:
        stages["breakdown"]["status"] = "pending"

    plan_merges = [c for c in merges if "target:plan" in (c.get("labels") or [])]
    if any(c.get("status") == "closed" for c in plan_merges):
        stages["plan"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in plans):
        stages["plan"]["status"] = "active"
    elif stages["breakdown"]["status"] == "done":
        stages["plan"]["status"] = "pending"

    code_merges = [c for c in merges if "target:code" in (c.get("labels") or [])]
    if any(c.get("status") == "closed" for c in code_merges):
        stages["dev"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in devs):
        stages["dev"]["status"] = "active"
    elif stages["plan"]["status"] == "done":
        stages["dev"]["status"] = "pending"

    if any(c.get("status") == "closed" and "changes-requested" not in (c.get("close_reason", "") or "") for c in reviews):
        stages["review"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in reviews):
        stages["review"]["status"] = "active"
    elif stages["dev"]["status"] == "done":
        stages["review"]["status"] = "pending"

    if all(stages[s]["status"] == "done" for s in ("breakdown", "plan", "dev", "review")):
        if code_merges and all(c.get("status") == "closed" for c in code_merges):
            stages["merge"]["status"] = "done"
        elif any(c.get("status") in ("open", "in_progress") for c in code_merges):
            stages["merge"]["status"] = "active"
        else:
            stages["merge"]["status"] = "pending"

    if epic_merges and any(c.get("status") == "closed" for c in epic_merges):
        stages["ship"]["status"] = "done"
    elif epic_merges and any(c.get("status") in ("open", "in_progress") for c in epic_merges):
        stages["ship"]["status"] = "active"
    elif stages["merge"]["status"] == "done":
        stages["ship"]["status"] = "pending"

    return stages
