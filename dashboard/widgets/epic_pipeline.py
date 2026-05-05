"""Epic pipeline widget for the unified CTO dashboard.

Shows one swimlane per open epic with columns for each workflow stage:
Epic → Breakdown → Plan → Dev → Review → Merge → Ship.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual.reactive import reactive
from textual.widgets import Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"
_POOL = ThreadPoolExecutor(max_workers=12)


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


def _parse_iso(s: str) -> dt.datetime | None:
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
    """Rich widget showing epic swimlanes."""

    pipelines = reactive[list[dict[str, Any]]]([])

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def on_mount(self) -> None:
        self.refresh_pipelines()
        self.set_interval(5, self.refresh_pipelines)

    def refresh_pipelines(self) -> None:
        pipes: list[dict[str, Any]] = []
        if not TEAMS_DIR.is_dir():
            self.pipelines = pipes
            return

        teams = [p for p in sorted(TEAMS_DIR.iterdir()) if (p / ".cto").is_dir()]
        futures = {t.name: _POOL.submit(_gather_team_pipelines, t) for t in teams}
        for team_name, fut in futures.items():
            try:
                result = fut.result(timeout=10.0)
                pipes.extend(result)
            except Exception:
                pass
        self.pipelines = pipes

    def render(self) -> Panel:
        t = Table(
            expand=True,
            show_lines=False,
            header_style="bold",
            pad_edge=False,
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
        for pipe in self.pipelines:
            row = _render_pipeline_row(pipe, now)
            t.add_row(*row)

        if not self.pipelines:
            t.add_row(
                Text("—", style="dim"),
                Text("(no open epics)", style="dim"),
                *[Text("—", style="dim")] * 7,
            )

        return Panel(
            t,
            title=f"Epic Pipeline ({len(self.pipelines)})",
            border_style="bright_green",
        )


def _render_pipeline_row(pipe: dict[str, Any], now: dt.datetime) -> list[Text]:
    team = pipe["team"]
    epic_title = pipe["title"][:30]
    stages = pipe["stages"]

    created = _parse_iso(pipe.get("created_at", ""))
    age = _human_age(int((now - created).total_seconds())) if created else "—"

    def cell(stage: str) -> Text:
        s = stages.get(stage, {"status": "none"})
        status = s["status"]
        assignee = s.get("assignee", "")
        if status == "done":
            return Text("✓", style="green")
        if status == "active":
            return Text("◐", style="yellow")
        if status == "blocked":
            return Text("✗", style="red")
        if status == "pending":
            return Text("●", style="dim")
        return Text("—", style="dim")

    return [
        Text(team, style="cyan"),
        Text(epic_title, style="white"),
        cell("breakdown"),
        cell("plan"),
        cell("dev"),
        cell("review"),
        cell("merge"),
        cell("ship"),
        Text(age, style="dim"),
    ]


def _gather_team_pipelines(team_dir: Path) -> list[dict[str, Any]]:
    issues = _bd_json(["list", "--status", "open"], team_dir)
    issues += _bd_json(["list", "--status", "in_progress"], team_dir)

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

    closed_issues = _bd_json(["list", "--status", "closed", "--limit", "50"], team_dir)
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

    # Breakdown stage
    if any(c.get("status") == "closed" for c in breakdowns):
        stages["breakdown"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in breakdowns):
        stages["breakdown"]["status"] = "active"
    else:
        stages["breakdown"]["status"] = "pending"

    # Plan stage
    plan_merges = [c for c in merges if "target:plan" in (c.get("labels") or [])]
    if any(c.get("status") == "closed" for c in plan_merges):
        stages["plan"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in plans):
        stages["plan"]["status"] = "active"
    elif stages["breakdown"]["status"] == "done":
        stages["plan"]["status"] = "pending"

    # Dev stage
    code_merges = [c for c in merges if "target:code" in (c.get("labels") or [])]
    if any(c.get("status") == "closed" for c in code_merges):
        stages["dev"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in devs):
        stages["dev"]["status"] = "active"
    elif stages["plan"]["status"] == "done":
        stages["dev"]["status"] = "pending"

    # Review stage
    if any(c.get("status") == "closed" and "changes-requested" not in (c.get("close_reason", "") or "") for c in reviews):
        stages["review"]["status"] = "done"
    elif any(c.get("status") in ("open", "in_progress") for c in reviews):
        stages["review"]["status"] = "active"
    elif stages["dev"]["status"] == "done":
        stages["review"]["status"] = "pending"

    # Merge stage (sub-merges)
    if all(stages[s]["status"] == "done" for s in ("breakdown", "plan", "dev", "review")):
        if code_merges and all(c.get("status") == "closed" for c in code_merges):
            stages["merge"]["status"] = "done"
        elif any(c.get("status") in ("open", "in_progress") for c in code_merges):
            stages["merge"]["status"] = "active"
        else:
            stages["merge"]["status"] = "pending"

    # Ship stage
    if epic_merges and any(c.get("status") == "closed" for c in epic_merges):
        stages["ship"]["status"] = "done"
    elif epic_merges and any(c.get("status") in ("open", "in_progress") for c in epic_merges):
        stages["ship"]["status"] = "active"
    elif stages["merge"]["status"] == "done":
        stages["ship"]["status"] = "pending"

    return stages
