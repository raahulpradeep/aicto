"""Inline diff viewer screen for epic branches.

Shows git diff for selected epic branch with syntax highlighting via Rich.
Includes approve/reject buttons at bottom.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Any

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"


def _run(cmd: list[str], cwd: Optional[Path] = None, timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.stdout if r.returncode == 0 else (r.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _get_diff(team: str, epic_id: str) -> str:
    """Get git diff for an epic branch vs main."""
    tdir = TEAMS_DIR / team
    if not tdir.is_dir():
        return f"Team {team} not found"

    branch = f"epic/{epic_id}"
    # Check if branch exists
    result = _run(["git", "branch", "--list", branch], cwd=tdir)
    if not result.strip():
        return f"Branch {branch} does not exist"

    diff = _run(["git", "diff", f"main...{branch}"], cwd=tdir, timeout=10.0)
    if not diff:
        diff = "(no changes — branch is up to date with main)"
    return diff


class DiffViewerScreen(ModalScreen[Optional[str]]):
    """Scrollable diff viewer with approve/reject actions.

    Dismisses with 'approve', 'reject', or None.
    """

    def __init__(self, team: str, epic_id: str, title: str = "") -> None:
        super().__init__()
        self.team = team
        self.epic_id = epic_id
        self.epic_title = title
        self.diff_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-viewer"):
            header = f"Diff: epic/{self.epic_id}"
            if self.epic_title:
                header += f" — {self.epic_title}"
            yield Static(header, classes="dialog-title")
            yield Static("", id="diff-content")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Approve", variant="success", id="approve")
                yield Button("Reject", variant="error", id="reject")
                yield Button("Comment", variant="primary", id="comment")
                yield Button("Back", variant="default", id="back")

    def on_mount(self) -> None:
        self.run_worker(self._load_diff, thread=True)

    def _load_diff(self) -> None:
        diff = _get_diff(self.team, self.epic_id)
        self.app.call_from_thread(self._render_diff, diff)

    def _render_diff(self, diff: str) -> None:
        self.diff_text = diff
        if not diff.strip():
            diff = "(no diff available)"

        # Render as a scrollable syntax-highlighted diff
        # Rich Syntax handles diff format reasonably well
        lines = diff.splitlines()
        # Truncate very large diffs for TUI performance
        if len(lines) > 500:
            lines = lines[:500]
            lines.append("\n... (diff truncated, view in terminal with `git diff main...epic/{self.epic_id}`)")
            diff = "\n".join(lines)

        try:
            syntax = Syntax(diff, "diff", theme="monokai", line_numbers=True, word_wrap=False)
            panel = Panel(
                syntax,
                title=f"epic/{self.epic_id} vs main",
                border_style="green",
            )
            self.query_one("#diff-content", Static).update(panel)
        except Exception:
            # Fallback to plain text
            self.query_one("#diff-content", Static).update(
                Panel(Text(diff, no_wrap=False), border_style="green")
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.dismiss(None)
        elif event.button.id == "approve":
            self.dismiss("approve")
        elif event.button.id == "reject":
            self.dismiss("reject")
        elif event.button.id == "comment":
            self.dismiss("comment")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key == "a":
            self.dismiss("approve")
        elif event.key == "r":
            self.dismiss("reject")
