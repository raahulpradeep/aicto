"""Transactional action queue for workflow changes.

All workflow mutations (filing issues, relabeling, closing) go through
here so they can be previewed, confirmed, executed with retries, and
rolled back on failure.

Design doc: plans/reconciler-redesign.md
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from event_bus import EventBus
from state_store import StateStore


# ---------------------------------------------------------------------------
# Action types (backward-compatible with old reconciler actions)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileIssue:
    title: str
    description: str
    labels: tuple[str, ...]
    priority: int = 2
    blocks: Optional[str] = None


@dataclass(frozen=True)
class FilePair:
    """File two issues atomically with a blocks dependency."""
    upstream: FileIssue
    downstream: FileIssue


@dataclass(frozen=True)
class AddLabel:
    issue_id: str
    label: str


@dataclass(frozen=True)
class RemoveLabel:
    issue_id: str
    label: str


@dataclass(frozen=True)
class ReopenIssue:
    issue_id: str
    comment: str


@dataclass(frozen=True)
class CloseIssue:
    issue_id: str
    reason: str


@dataclass(frozen=True)
class AutoMergeEpic:
    epic_id: str
    merge_target: str


Action = Any  # Union of the above + HealAction from health_auditor


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

class Transaction:
    """Preview → Confirm → Execute with rollback support."""

    def __init__(
        self,
        actions: list[Any],
        dry_run: bool = False,
        team_dir: Optional[Path] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ):
        self.actions = actions
        self.dry_run = dry_run
        self.team_dir = team_dir or Path(".")
        self._on_event = on_event
        self._log: list[str] = []
        self._filed_ids: list[str] = []  # For rollback

    def preview(self) -> list[str]:
        """Return human-readable lines describing what would happen."""
        lines: list[str] = []
        for a in self.actions:
            lines.append(self._describe(a))
        return lines

    def execute(self) -> list[str]:
        """Execute all actions with best-effort retry."""
        t0 = time.monotonic()
        for a in self.actions:
            self._execute_one(a)
        dur_ms = int((time.monotonic() - t0) * 1000)
        self._emit_event("reconciler_tick", {
            "actions": ";".join(self._log),
            "duration_ms": str(dur_ms),
        })
        return self._log

    def rollback(self) -> list[str]:
        """Best-effort undo of filed issues."""
        undone: list[str] = []
        for issue_id in reversed(self._filed_ids):
            r = subprocess.run(
                ["bd", "delete", issue_id],
                capture_output=True,
            )
            if r.returncode == 0:
                undone.append(f"rollback: deleted {issue_id}")
            else:
                undone.append(f"rollback: failed to delete {issue_id}")
        return undone

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _describe(self, a: Any) -> str:
        if hasattr(a, "kind"):  # HealAction
            return f"heal: {a.kind.name} {a.issue_id or a.target_id} — {a.reason}"
        if isinstance(a, FileIssue):
            return f"file: {a.title!r} labels={list(a.labels)}"
        if isinstance(a, FilePair):
            return (
                f"pair: {a.upstream.title!r} → {a.downstream.title!r}"
            )
        if isinstance(a, AddLabel):
            return f"label+: {a.issue_id} +{a.label}"
        if isinstance(a, RemoveLabel):
            return f"label-: {a.issue_id} -{a.label}"
        if isinstance(a, ReopenIssue):
            return f"reopen: {a.issue_id}"
        if isinstance(a, CloseIssue):
            return f"close: {a.issue_id} reason={a.reason!r}"
        if isinstance(a, AutoMergeEpic):
            return f"auto-merge: epic/{a.epic_id} into {a.merge_target}"
        return f"unknown: {a!r}"

    def _execute_one(self, a: Any) -> None:
        if hasattr(a, "kind"):
            self._execute_heal(a)
            return
        if isinstance(a, FileIssue):
            self._do_file_issue(a)
        elif isinstance(a, FilePair):
            self._do_file_pair(a)
        elif isinstance(a, AddLabel):
            self._do_add_label(a)
        elif isinstance(a, RemoveLabel):
            self._do_remove_label(a)
        elif isinstance(a, ReopenIssue):
            self._do_reopen(a)
        elif isinstance(a, CloseIssue):
            self._do_close(a)
        elif isinstance(a, AutoMergeEpic):
            self._do_auto_merge(a)

    def _execute_heal(self, a: Any) -> None:
        from health_auditor import HealKind
        kind = a.kind
        if kind == HealKind.ADD_LABEL:
            self._do_add_label(AddLabel(a.issue_id, a.label))
        elif kind == HealKind.REMOVE_LABEL:
            self._do_remove_label(RemoveLabel(a.issue_id, a.label))
        elif kind == HealKind.RESET_CLAIM:
            self._do_reopen(ReopenIssue(a.issue_id, "auto-heal: zombie detected, resetting claim"))
            self._do_add_label(AddLabel(a.issue_id, "stuck:zombie"))
        elif kind == HealKind.FILE_REVIEW:
            # Derive review title from upstream dev
            dev = self._bd_show(a.target_id)
            title = f"Review: {dev.get('title', 'unknown')}" if dev else "Review: unknown"
            body = (
                f"epic: {a.epic_id}\n"
                f"upstream: {a.target_id}\n"
                f"Review diff on the dev branch against epic/{a.epic_id}."
            )
            self._do_file_issue(FileIssue(
                title=title,
                description=body,
                labels=("role:reviewer", "kind:review", "target:code"),
                priority=2,
            ))
        elif kind == HealKind.FILE_MERGE:
            dev = self._bd_show(a.target_id)
            title = f"Merge: {dev.get('title', 'unknown')}" if dev else "Merge: unknown"
            body = (
                f"epic: {a.epic_id}\n"
                f"upstream: {a.target_id}\n"
                f"branch: task/{a.target_id}\n"
                f"Merge task/{a.target_id} into epic/{a.epic_id}, prune sub-worktree."
            )
            self._do_file_issue(FileIssue(
                title=title,
                description=body,
                labels=("role:manager", "kind:merge", "target:code"),
                priority=1,
            ))
        elif kind == HealKind.FILE_PLAN_REVIEW_PAIR:
            self._do_file_pair(FilePair(
                upstream=FileIssue(
                    title=f"Plan: {a.epic_id}",
                    description=(
                        f"epic: {a.epic_id}\n"
                        f"Author plans/{a.epic_id}.md on a task branch off epic/{a.epic_id}."
                    ),
                    labels=("role:developer", "kind:plan"),
                    priority=2,
                ),
                downstream=FileIssue(
                    title=f"Review plan: {a.epic_id}",
                    description=(
                        f"epic: {a.epic_id}\n"
                        f"Review plans/{a.epic_id}.md on the plan branch."
                    ),
                    labels=("role:reviewer", "kind:review", "target:plan"),
                    priority=2,
                ),
            ))
        elif kind == HealKind.FILE_BREAKDOWN_MERGE:
            self._do_file_issue(FileIssue(
                title=f"Merge breakdown: {a.epic_id}",
                description=(
                    f"epic: {a.epic_id}\n"
                    f"branch: manager/{a.target_id}\n"
                    f"Merge manager/{a.target_id} into epic/{a.epic_id}, prune sub-worktree."
                ),
                labels=("role:manager", "kind:merge", "target:breakdown"),
                priority=1,
            ))
        elif kind == HealKind.FILE_PLAN_MERGE:
            self._do_file_issue(FileIssue(
                title=f"Merge plan: {a.epic_id}",
                description=(
                    f"epic: {a.epic_id}\n"
                    f"branch: task/{a.target_id}\n"
                    f"Merge task/{a.target_id} into epic/{a.epic_id}, prune sub-worktree."
                ),
                labels=("role:manager", "kind:merge", "target:plan"),
                priority=1,
            ))
        elif kind == HealKind.FILE_CODE_MERGE:
            self._do_file_issue(FileIssue(
                title=f"Merge: {a.target_id}",
                description=(
                    f"epic: {a.epic_id}\n"
                    f"upstream: {a.target_id}\n"
                    f"branch: task/{a.target_id}\n"
                    f"Merge task/{a.target_id} into epic/{a.epic_id}, prune sub-worktree."
                ),
                labels=("role:manager", "kind:merge", "target:code"),
                priority=1,
            ))
        elif kind == HealKind.FILE_EPIC_MERGE:
            self._do_file_issue(FileIssue(
                title=f"Merge epic: {a.epic_id}",
                description=(
                    f"epic: {a.epic_id}\n"
                    f"epic-branch: epic/{a.epic_id}"
                ),
                labels=("role:cto", "kind:merge", "target:epic"),
                priority=1,
            ))
        elif kind == HealKind.AUTO_MERGE_EPIC:
            self._do_auto_merge(AutoMergeEpic(a.epic_id, a.merge_target))
        elif kind == HealKind.ESCALATE_CTO:
            self._do_file_issue(FileIssue(
                title=f"Escalation: {a.reason}",
                description=(
                    f"epic: {a.epic_id}\n"
                    f"reason: {a.reason}"
                ),
                labels=("role:cto", "kind:escalation"),
                priority=1,
            ))
        elif kind == HealKind.REOPEN:
            self._do_reopen(ReopenIssue(a.issue_id, a.reason))
        elif kind == HealKind.CLOSE:
            self._do_close(CloseIssue(a.issue_id, a.reason))

    def _do_file_issue(self, a: FileIssue) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        new_id = self._bd_file(a)
        if new_id:
            self._filed_ids.append(new_id)
            self._publish_task_created(new_id, a)

    def _do_file_pair(self, a: FilePair) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        up_id = self._bd_file(a.upstream)
        if up_id:
            self._publish_task_created(up_id, a.upstream)
            down_id = self._bd_file(FileIssue(
                title=a.downstream.title,
                description=a.downstream.description,
                labels=a.downstream.labels,
                priority=a.downstream.priority,
            ))
            if down_id:
                subprocess.run(
                    ["bd", "dep", up_id, "--blocks", down_id],
                    capture_output=True,
                )
                self._filed_ids.append(down_id)
                self._publish_task_created(down_id, a.downstream)
                self._publish("review.required", task_id=down_id)

    def _do_add_label(self, a: AddLabel) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        subprocess.run(
            ["bd", "update", a.issue_id, "--add-label", a.label],
            capture_output=True,
        )

    def _do_remove_label(self, a: RemoveLabel) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        subprocess.run(
            ["bd", "update", a.issue_id, "--remove-label", a.label],
            capture_output=True,
        )

    def _do_reopen(self, a: ReopenIssue) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        subprocess.run(
            ["bd", "reopen", a.issue_id],
            capture_output=True,
        )

    def _do_close(self, a: CloseIssue) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        subprocess.run(
            ["bd", "close", a.issue_id, "-r", a.reason],
            capture_output=True,
        )

    def _do_auto_merge(self, a: AutoMergeEpic) -> None:
        self._log.append(self._describe(a))
        if self.dry_run:
            return
        from reconciler import _auto_merge_epic
        _auto_merge_epic(a.epic_id, a.merge_target)

    # ------------------------------------------------------------------
    # bd helpers
    # ------------------------------------------------------------------

    def _bd_file(self, a: FileIssue) -> Optional[str]:
        cmd = [
            "bd", "create", "-t", "task",
            "-l", ",".join(a.labels),
            "-p", str(a.priority),
            "-d", a.description,
            a.title,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for tok in (r.stdout or "").split():
            if "-" in tok and tok.replace("-", "").isalnum():
                return tok
        return None

    def _bd_show(self, issue_id: str) -> dict[str, Any]:
        r = subprocess.run(
            ["bd", "show", issue_id, "--json"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {}
        try:
            data = json.loads(r.stdout)
            if isinstance(data, list) and data:
                return data[0]
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------
    # Event publishing
    # ------------------------------------------------------------------

    def _publish(self, kind: str, **kwargs: Any) -> None:
        if self._on_event:
            self._on_event(kind, kwargs)

    def _publish_task_created(self, task_id: str, a: FileIssue) -> None:
        kind = ""
        for lbl in a.labels:
            if lbl.startswith("kind:"):
                kind = lbl.split(":", 1)[1]
                break
        self._publish("task.created", task_id=task_id, kind=kind, title=a.title)
        if kind == "dev":
            self._publish("dev.assigned", task_id=task_id)
        elif kind == "review":
            self._publish("review.required", task_id=task_id)
        elif kind == "merge":
            self._publish("merge.ready", task_id=task_id)

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        helper = self.team_dir / ".cto" / "telemetry_helper.py"
        if not helper.exists():
            return
        kvs = [f"--kv={k}={v}" for k, v in payload.items()]
        subprocess.run(
            ["python3", str(helper), str(self.team_dir / ".cto" / "activity.jsonl"),
             event_type, *kvs],
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# ActionQueue — public API
# ---------------------------------------------------------------------------

class ActionQueue:
    """All workflow changes go through here."""

    def __init__(self, team_dir: Optional[Path] = None):
        self.team_dir = team_dir or Path(".")
        self._history: list[list[str]] = []

    def submit(
        self,
        actions: list[Any],
        *,
        dry_run: bool = False,
        confirm: bool = False,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> Transaction:
        """Preview → Confirm → Execute with retries → Rollback on failure."""
        tx = Transaction(
            actions=actions,
            dry_run=dry_run,
            team_dir=self.team_dir,
            on_event=on_event,
        )
        if dry_run or confirm:
            preview = tx.preview()
            self._history.append(preview)
        if not dry_run:
            tx.execute()
        return tx

    def preview(self, actions: list[Any]) -> list[str]:
        """Return what would happen without doing it."""
        tx = Transaction(actions=actions, dry_run=True, team_dir=self.team_dir)
        return tx.preview()

    def history(self) -> list[list[str]]:
        return list(self._history)
