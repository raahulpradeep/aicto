#!/usr/bin/env -S uv run --quiet --with textual,rich python
"""CTO Command Center — interactive TUI for approving / rejecting inbox items."""
from __future__ import annotations
from typing import Optional

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
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

ROOT = Path(__file__).resolve().parent.parent
TEAMS_DIR = ROOT / "teams"
CTO_BIN = ROOT / "bin" / "cto"

_ARTIFACT_RE = re.compile(r"^artifact:\s*(.+?)\s+@\s*(.+)$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^branch:\s*(.+)$", re.MULTILINE)
_EPIC_RE = re.compile(r"^epic:\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class InboxItem:
    team: str
    id: str
    title: str
    labels: list[str] = field(default_factory=list)
    description: str = ""
    created_at: str = ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Optional[Path] = None, timeout: float = 10.0) -> str:
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


def fetch_inbox() -> list[InboxItem]:
    out = _run([str(CTO_BIN), "inbox", "--json"], cwd=ROOT, timeout=10.0)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [
        InboxItem(
            team=i.get("team", ""),
            id=i.get("id", ""),
            title=i.get("title", ""),
            labels=i.get("labels") or [],
            description=i.get("description") or "",
            created_at=i.get("created_at") or "",
        )
        for i in data
    ]


def _kind_and_target(labels: list[str]) -> tuple[str, str]:
    kind = ""
    target = ""
    for lbl in labels:
        if lbl.startswith("kind:"):
            kind = lbl.split(":", 1)[1]
        elif lbl.startswith("target:"):
            target = lbl.split(":", 1)[1]
    return kind, target


def _epic_merge_summary(item: InboxItem) -> tuple[str, Optional[Path]]:
    desc = item.description or ""
    epic_id = ""
    m = _EPIC_RE.search(desc)
    if m:
        epic_id = m.group(1).strip()
    if not epic_id:
        bm = re.search(r"epic/([A-Za-z0-9._-]+)", desc)
        if bm:
            epic_id = bm.group(1)
    if not epic_id:
        return "Epic merge approval (cannot determine epic id).", None

    wt = TEAMS_DIR / item.team / ".cto" / "worktrees" / epic_id
    if not wt.exists():
        return f"Epic merge approval\nepic: {epic_id}\n(worktree missing)", None

    log = _run(
        ["git", "-C", str(wt), "log", "--oneline", f"epic/{epic_id}...main"],
        timeout=5.0,
    )
    commits = log.strip()
    count = len([l for l in commits.splitlines() if l]) if commits else 0
    meta = f"Epic merge approval\nepic: {epic_id}\ncommits ahead of main: {count}"
    if commits:
        meta += f"\n\n{commits}"
    return meta, None


def resolve_artifact(item: InboxItem) -> tuple[str, Optional[Path]]:
    """Return (meta_text, path_or_none)."""
    desc = item.description or ""
    kind, target = _kind_and_target(item.labels)

    if kind == "merge" and target == "epic":
        return _epic_merge_summary(item)

    m = _ARTIFACT_RE.search(desc)
    if m:
        rel_path = m.group(1).strip()
        branch = m.group(2).strip()
    else:
        branch_m = _BRANCH_RE.search(desc)
        branch = branch_m.group(1).strip() if branch_m else ""
        epic_m = _EPIC_RE.search(desc)
        epic_id = epic_m.group(1).strip() if epic_m else ""

        if target == "breakdown" and epic_id:
            rel_path = f"breakdowns/{epic_id}.md"
        elif target == "plan" and epic_id:
            rel_path = f"plans/{epic_id}.md"
        else:
            return "No artifact found.", None

    if not branch:
        return "No artifact found.", None

    worktree_name = branch.split("/")[-1]
    full = TEAMS_DIR / item.team / ".cto" / "worktrees" / worktree_name / rel_path
    meta = f"artifact: {rel_path}\nbranch: {branch}\nteam: {item.team}\nid: {item.id}"
    return meta, full


def read_artifact_safe(path: Path) -> str:
    base = TEAMS_DIR.resolve()
    try:
        target = path.resolve()
    except OSError:
        return f"Error: cannot resolve path ({path})."
    if not str(target).startswith(str(base) + os.sep) and target != base:
        return "Error: path escapes team workspace."
    if not target.exists():
        return (
            f"Error: artifact missing ({path}).\n\n"
            "You may still approve or reject."
        )
    try:
        text = target.read_text(errors="replace")
        if len(text) > 100_000:
            text = text[:100_000] + f"\n…(truncated; full size {len(text)} bytes)"
        return text
    except Exception as exc:
        return f"Error reading artifact: {exc}"


# ---------------------------------------------------------------------------
# modals
# ---------------------------------------------------------------------------

class ApproveModal(ModalScreen[Optional[str]]):
    def __init__(self, item: InboxItem) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="approve-dialog"):
            yield Static(f"Approve {self.item.id}?")
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


class RejectModal(ModalScreen[Optional[str]]):
    def __init__(self, item: InboxItem) -> None:
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="reject-dialog"):
            yield Static(f"Reject {self.item.id} — comment required:")
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


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

class CommandCenter(App):
    CSS = """
    Screen { align: center middle; }
    #main { width: 100%; height: 100%; }
    #header { height: 1; background: $primary; color: $text; content-align: center middle; }
    #footer { height: 1; background: $surface; color: $text-muted; content-align: center middle; }
    #inbox-pane { width: 40%; height: 1fr; }
    #detail-pane { width: 60%; height: 1fr; }
    #detail-meta { height: auto; max-height: 6; }
    #action-bar { height: 3; }
    #detail-scroll { height: 1fr; }
    #detail-md { height: auto; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("a", "approve", "Approve"),
        ("r", "reject", "Reject"),
        ("o", "open_editor", "Open"),
        ("R", "refresh", "Refresh"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("enter", "reload_detail", "Reload"),
    ]

    items: reactive[list[InboxItem]] = reactive([])
    selected_index: reactive[int] = reactive(0)
    has_new: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Static("CTO Command Center", id="header")
        with Horizontal(id="main"):
            with Vertical(id="inbox-pane"):
                yield DataTable(id="inbox-table")
            with Vertical(id="detail-pane"):
                yield Static("", id="detail-meta")
                with Horizontal(id="action-bar"):
                    yield Button("Approve (a)", variant="success", id="btn-approve")
                    yield Button("Reject (r)", variant="error", id="btn-reject")
                with VerticalScroll(id="detail-scroll"):
                    yield Markdown(id="detail-md")
        yield Static(
            "↑/↓ select · a approve · r reject · o open · R refresh · q quit",
            id="footer",
        )

    def on_mount(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.add_columns("ID", "Kind", "Team", "Title")
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.focus()
        self.poll_inbox()
        self.set_interval(3, self.poll_inbox)

    # -- reactive watchers --------------------------------------------------

    def watch_items(self, items: list[InboxItem]) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.clear()
        for item in items:
            kind, target = _kind_and_target(item.labels)
            tag = f"{kind}:{target}" if target else kind
            table.add_row(item.id, tag, item.team, item.title)
        if self.selected_index >= len(items):
            self.selected_index = max(0, len(items) - 1)
        if items and table.cursor_row != self.selected_index:
            table.move_cursor(row=self.selected_index)
        self._update_header()
        self._update_detail()

    def watch_selected_index(self, index: int) -> None:
        self._update_detail()

    def watch_has_new(self, val: bool) -> None:
        self._update_header()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected_index = event.cursor_row

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._update_detail()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-approve":
            self.action_approve()
        elif event.button.id == "btn-reject":
            self.action_reject()

    # -- detail rendering ---------------------------------------------------

    def _update_header(self) -> None:
        count = len(self.items)
        teams = len({i.team for i in self.items})
        indicator = "● " if self.has_new else ""
        text = (
            f"{indicator}CTO Command Center · {count} pending across {teams} "
            f"team{'s' if teams != 1 else ''}"
        )
        self.query_one("#header", Static).update(text)

    def _update_detail(self) -> None:
        if not self.items:
            self.query_one("#detail-meta", Static).update(
                "Nothing to review. Polling every 3s — q to exit."
            )
            self.query_one("#detail-md", Markdown).update("")
            return

        item = self.items[self.selected_index]
        meta, path = resolve_artifact(item)

        if path:
            content = read_artifact_safe(path)
            self.query_one("#detail-meta", Static).update(meta)
        else:
            content = meta
            self.query_one("#detail-meta", Static).update("")

        self.query_one("#detail-md", Markdown).update(content)

    # -- polling ------------------------------------------------------------

    def poll_inbox(self) -> None:
        self.run_worker(self._poll_worker, thread=True)

    def _poll_worker(self) -> None:
        new_items = fetch_inbox()
        self.call_from_thread(self._apply_items, new_items)

    def _apply_items(self, new_items: list[InboxItem]) -> None:
        old_ids = {i.id for i in self.items}
        new_ids = {i.id for i in new_items}
        arrivals = new_ids - old_ids
        if arrivals:
            self.notify(f"New: {', '.join(arrivals)}", title="Inbox")
            print("\a", end="", flush=True)
            self.has_new = True
            self.set_timer(2.0, lambda: setattr(self, "has_new", False))
        self.items = new_items

    # -- actions ------------------------------------------------------------

    def action_cursor_down(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        if table.cursor_row < len(self.items) - 1:
            table.move_cursor(row=table.cursor_row + 1)

    def action_cursor_up(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        if table.cursor_row > 0:
            table.move_cursor(row=table.cursor_row - 1)

    def action_reload_detail(self) -> None:
        self._update_detail()

    def action_refresh(self) -> None:
        self.poll_inbox()

    def action_approve(self) -> None:
        if not self.items:
            return
        item = self.items[self.selected_index]
        self.push_screen(ApproveModal(item), self._on_approve)

    def _on_approve(self, comment: Optional[str]) -> None:
        if comment is None:
            return
        item = self.items[self.selected_index]
        self._run_cto("approve", item, comment)

    def action_reject(self) -> None:
        if not self.items:
            return
        item = self.items[self.selected_index]
        self.push_screen(RejectModal(item), self._on_reject)

    def _on_reject(self, comment: Optional[str]) -> None:
        if comment is None:
            return
        item = self.items[self.selected_index]
        self._run_cto("reject", item, comment)

    def _run_cto(self, cmd: str, item: InboxItem, comment: str) -> None:
        def worker():
            args = [
                str(CTO_BIN),
                cmd,
                item.team,
                item.id,
                "--comment",
                comment,
            ]
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30.0
            )
            self.call_from_thread(self._handle_cto_result, cmd, result)

        self.run_worker(worker, thread=True)

    def _handle_cto_result(
        self, cmd: str, result: subprocess.CompletedProcess
    ) -> None:
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "unknown error"
            self.notify(f"{cmd} failed: {err}", severity="error", title="Error")
        else:
            self.notify(f"{cmd}d successfully", title="Done")
            self.poll_inbox()

    def action_open_editor(self) -> None:
        if not self.items:
            return
        item = self.items[self.selected_index]
        meta, path = resolve_artifact(item)
        if not path or not path.exists():
            self.notify("No artifact to open", severity="warning")
            return

        editor = os.environ.get("EDITOR", "vi")
        with self.app.suspend():
            subprocess.run([editor, str(path)])
        self.poll_inbox()


if __name__ == "__main__":
    CommandCenter().run()
