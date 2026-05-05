"""Per-epic drill-down screen.

Shows full epic lifecycle: tasks by stage, cost accumulation, branch info, CI status.
Horizontal swimlane: [Breakdown] → [Plan] → [Dev] → [Review] → [Merge] → [Ship]
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from dashboard.widgets.epic_pipeline import _compute_stages, _human_age, _parse_iso

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
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


def _gather_epic_details(team: str, epic_id: str) -> dict[str, Any]:
    """Gather full epic details: children, branch info, CI status."""
    tdir = TEAMS_DIR / team
    if not tdir.is_dir():
        return {"error": "team not found"}

    # Get all issues related to this epic
    open_issues = _bd_json(["list", "--status", "open"], tdir)
    ip_issues = _bd_json(["list", "--status", "in_progress"], tdir)
    closed_issues = _bd_json(["list", "--status", "closed", "-n", "50"], tdir)
    all_issues = open_issues + ip_issues + closed_issues

    epic = None
    children: list[dict[str, Any]] = []
    for i in all_issues:
        labels = i.get("labels") or []
        iid = i.get("id", "")
        if ("kind:epic" in labels or i.get("issue_type") == "epic") and iid == epic_id:
            epic = i
            continue
        desc = i.get("description", "")
        for line in desc.splitlines():
            if line.startswith("epic:"):
                child_epic_id = line.split(":", 1)[1].strip()
                if child_epic_id == epic_id:
                    children.append(i)
                    break

    if not epic:
        epic = {"id": epic_id, "title": "Unknown epic", "created_at": ""}

    stages = _compute_stages(children)

    # Gather tasks by stage
    tasks_by_stage: dict[str, list[dict[str, Any]]] = {
        "breakdown": [],
        "plan": [],
        "dev": [],
        "review": [],
        "merge": [],
        "ship": [],
    }
    for c in children:
        labels = c.get("labels") or []
        if "kind:breakdown" in labels:
            tasks_by_stage["breakdown"].append(c)
        elif "kind:plan" in labels:
            tasks_by_stage["plan"].append(c)
        elif "kind:dev" in labels:
            tasks_by_stage["dev"].append(c)
        elif "kind:review" in labels:
            tasks_by_stage["review"].append(c)
        elif "kind:merge" in labels:
            target = [l for l in labels if l.startswith("target:")]
            t = target[0].split(":", 1)[1] if target else ""
            if t == "epic":
                tasks_by_stage["ship"].append(c)
            elif t == "code":
                tasks_by_stage["merge"].append(c)
            elif t == "plan":
                tasks_by_stage["plan"].append(c)

    # Branch info
    branch = f"epic/{epic_id}"
    worktree = tdir / ".cto" / "worktrees" / epic_id
    wt_exists = worktree.is_dir()

    # CI status (heuristic: check for .github/workflows and last run)
    ci_status = "unknown"
    ci_detail = ""
    # Try to get CI status from gh CLI if available
    try:
        repo_out = _run(["gh", "repo", "view", "--json", "url"], cwd=tdir).strip()
        if repo_out:
            # Check workflow runs for this branch
            wf_out = _run(
                ["gh", "run", "list", "--branch", branch, "--json", "status,conclusion,headSha,createdAt", "-L", "1"],
                cwd=tdir,
            )
            if wf_out:
                try:
                    runs = json.loads(wf_out)
                    if runs:
                        run = runs[0]
                        conclusion = run.get("conclusion", "")
                        status = run.get("status", "")
                        if conclusion == "success":
                            ci_status = "passing"
                        elif conclusion == "failure":
                            ci_status = "failing"
                        elif status == "in_progress":
                            ci_status = "running"
                        else:
                            ci_status = conclusion or status
                        ci_detail = f"(last run: {run.get('createdAt', '')})"
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    # Commit count
    commits = 0
    try:
        commit_out = _run(["git", "rev-list", "--count", branch], cwd=tdir).strip()
        if commit_out.isdigit():
            commits = int(commit_out)
    except Exception:
        pass

    # Cost accumulation (placeholder — would come from token.usage events)
    cost_estimate = 0.0
    for c in children:
        # Rough heuristic: $0.001 per token, 10K tokens per task
        cost_estimate += 0.01

    return {
        "team": team,
        "epic": epic,
        "stages": stages,
        "tasks_by_stage": tasks_by_stage,
        "branch": branch,
        "worktree_exists": wt_exists,
        "ci_status": ci_status,
        "ci_detail": ci_detail,
        "commits": commits,
        "cost_estimate": cost_estimate,
        "children": children,
    }


class EpicDetailScreen(ModalScreen[None]):
    """Full epic lifecycle drill-down screen."""

    def __init__(self, pipeline: dict[str, Any]) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.details: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="epic-detail"):
            yield Static("", id="detail-header")
            yield Static("", id="swimlane")
            yield Static("", id="tasks")
            yield Static("", id="costs")
            yield Static("", id="branch-info")
            with Horizontal(classes="dialog-buttons"):
                yield Button("View Diff", variant="primary", id="view-diff")
                yield Button("Comment", variant="primary", id="comment")
                yield Button("Back", variant="default", id="back")

    def on_mount(self) -> None:
        self.run_worker(self._load_details, thread=True)

    def _load_details(self) -> None:
        team = self.pipeline["team"]
        epic_id = self.pipeline["epic_id"]
        details = _gather_epic_details(team, epic_id)
        self.app.call_from_thread(self._update_display, details)

    def _update_display(self, details: dict[str, Any]) -> None:
        self.details = details
        epic = details.get("epic", {})
        title = epic.get("title", self.pipeline["title"])
        epic_id = epic.get("id", self.pipeline["epic_id"])
        team = details.get("team", self.pipeline["team"])
        created = epic.get("created_at", "")
        age = ""
        if created:
            parsed = _parse_iso(created)
            if parsed:
                age = _human_age(int((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()))

        # Header
        header_text = f"Epic: {epic_id} — {title}\nTeam: {team}  |  Age: {age}  |  Est. cost: ${details.get('cost_estimate', 0):.2f}"
        self.query_one("#detail-header", Static).update(header_text)

        # Swimlane
        stages = details.get("stages", self.pipeline.get("stages", {}))
        swimlane_parts: list[str] = []
        for stage_name in ["breakdown", "plan", "dev", "review", "merge", "ship"]:
            info = stages.get(stage_name, {"status": "none"})
            status = info["status"]
            icon = {"done": "✓", "active": "◐", "blocked": "✗", "pending": "●", "none": "○"}.get(status, "?")
            color = {"done": "green", "active": "yellow", "blocked": "red", "pending": "dim", "none": "dim"}.get(status, "white")
            swimlane_parts.append(f"[{color}]{stage_name.capitalize()} {icon}[/{color}]")
        swimlane_text = " → ".join(swimlane_parts)
        self.query_one("#swimlane", Static).update(swimlane_text)

        # Tasks by stage
        tasks = details.get("tasks_by_stage", {})
        task_table = Table(expand=True, show_lines=False, pad_edge=False)
        task_table.add_column("Stage", width=12)
        task_table.add_column("ID", width=12)
        task_table.add_column("Assignee", width=14)
        task_table.add_column("Status", width=10)
        task_table.add_column("Title", ratio=2)

        for stage_name in ["breakdown", "plan", "dev", "review", "merge", "ship"]:
            stage_tasks = tasks.get(stage_name, [])
            if stage_tasks:
                for t in stage_tasks:
                    assignee = t.get("assignee") or "—"
                    status = t.get("status", "open")
                    task_table.add_row(
                        stage_name.capitalize(),
                        t.get("id", "—"),
                        assignee,
                        status,
                        t.get("title", "")[:40],
                    )
            else:
                status_icon = stages.get(stage_name, {}).get("status", "none")
                if status_icon == "done":
                    task_table.add_row(stage_name.capitalize(), "—", "—", "done", "(no active tasks)")
                else:
                    task_table.add_row(stage_name.capitalize(), "—", "—", "—", "(no tasks)")

        self.query_one("#tasks", Static).update(Panel(task_table, title="Tasks by Stage", border_style="blue"))

        # Costs
        cost_text = (
            f"Branch: {details.get('branch', '—')}  |  "
            f"CI: {self._ci_dot(details.get('ci_status', 'unknown'))} {details.get('ci_status', 'unknown')} {details.get('ci_detail', '')}  |  "
            f"Commits: {details.get('commits', 0)}"
        )
        self.query_one("#costs", Static).update(cost_text)

        # Branch / worktree info
        branch_info = f"Worktree: {'✓ exists' if details.get('worktree_exists') else '✗ missing'}"
        self.query_one("#branch-info", Static).update(branch_info)

    def _ci_dot(self, status: str) -> str:
        if status == "passing":
            return "🟢"
        if status == "failing":
            return "🔴"
        if status == "running":
            return "🟡"
        return "⚪"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.dismiss()
        elif event.button.id == "view-diff":
            self.dismiss("view-diff")
        elif event.button.id == "comment":
            self.dismiss("comment")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()
