"""Activity telemetry layer for the AI CTO workspace.

Captures structured workflow events to a per-team JSONL log
(`teams/<team>/.cto/activity.jsonl`) so dashboards, notifications,
and health checks can observe *what happened* without repeatedly
cold-querying bd.

Events are append-only and rotated automatically (7-day retention).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any


class ActivityLog:
    """Thread-safe per-team activity logger.

    Usage::

        log = ActivityLog(team_dir)
        log.emit("issue_claimed", issue_id="abc-123", assignee="demo:dev-1")
    """

    RETENTION_DAYS = 7
    _locks: dict[str, threading.Lock] = {}
    _global_lock = threading.Lock()

    def __init__(self, team_dir: Path | str) -> None:
        self.team_dir = Path(team_dir)
        self.log_dir = self.team_dir / ".cto" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.team_dir / ".cto" / "activity.jsonl"
        self._ensure_lock()

    def _ensure_lock(self) -> None:
        key = str(self.team_dir.resolve())
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
        self._lock = self._locks[key]

    def emit(self, event_type: str, **kwargs: Any) -> None:
        """Append a single event atomically."""
        event = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event_type,
            **kwargs,
        }
        line = json.dumps(event, separators=(",", ":"), default=str)
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            self._rotate_if_needed()

    def tail(
        self,
        n: int = 50,
        since: dt.datetime | None = None,
        event_types: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most recent *n* events, optionally filtered."""
        out: list[dict[str, Any]] = []
        if not self.log_path.exists():
            return out
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return out

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_types and ev.get("event") not in event_types:
                continue
            if since is not None:
                try:
                    ev_ts = dt.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if ev_ts < since:
                    continue
            out.append(ev)
            if len(out) >= n:
                break
        out.reverse()
        return out

    def _rotate_if_needed(self) -> None:
        """Remove entries older than RETENTION_DAYS."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.RETENTION_DAYS)
        if not self.log_path.exists():
            return
        tmp = self.log_path.with_suffix(".jsonl.tmp")
        try:
            with open(self.log_path, "r", encoding="utf-8") as src, \
                 open(tmp, "w", encoding="utf-8") as dst:
                for line in src:
                    try:
                        ev = json.loads(line)
                        ts = dt.datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
                        if ts >= cutoff:
                            dst.write(line)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        dst.write(line)
            os.replace(tmp, self.log_path)
        except OSError:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    @staticmethod
    def for_team(team_dir: Path | str) -> ActivityLog:
        return ActivityLog(team_dir)


# Convenience helpers used by supervisor / reconciler ------------------------


def emit_agent_iteration_start(team_dir: Path | str, agent: str, task_id: str | None) -> None:
    ActivityLog(team_dir).emit(
        "agent_iteration_start",
        agent=agent,
        task_id=task_id or "",
    )


def emit_agent_iteration_end(
    team_dir: Path | str,
    agent: str,
    task_id: str | None,
    exit_code: int,
    duration_ms: int,
) -> None:
    ActivityLog(team_dir).emit(
        "agent_iteration_end",
        agent=agent,
        task_id=task_id or "",
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


def emit_agent_output(
    team_dir: Path | str,
    agent: str,
    task_id: str | None,
    lines: list[str],
) -> None:
    ActivityLog(team_dir).emit(
        "agent_output",
        agent=agent,
        task_id=task_id or "",
        output=lines,
    )


def emit_issue_created(
    team_dir: Path | str,
    issue_id: str,
    kind: str,
    title: str,
    labels: list[str],
    epic_id: str | None = None,
) -> None:
    ActivityLog(team_dir).emit(
        "issue_created",
        issue_id=issue_id,
        kind=kind,
        title=title,
        labels=labels,
        epic_id=epic_id or "",
    )


def emit_merge_executed(
    team_dir: Path | str,
    branch: str,
    target: str,
    epic_id: str | None = None,
    commit_hash: str | None = None,
) -> None:
    ActivityLog(team_dir).emit(
        "merge_executed",
        branch=branch,
        target=target,
        epic_id=epic_id or "",
        commit_hash=commit_hash or "",
    )


def emit_commit_pushed(
    team_dir: Path | str,
    branch: str,
    commit_hash: str,
    message: str,
    author: str,
) -> None:
    ActivityLog(team_dir).emit(
        "commit_pushed",
        branch=branch,
        commit_hash=commit_hash,
        message=message,
        author=author,
    )


def emit_reconciler_tick(
    team_dir: Path | str,
    actions: list[str],
    duration_ms: int,
) -> None:
    ActivityLog(team_dir).emit(
        "reconciler_tick",
        actions=actions,
        duration_ms=duration_ms,
    )


def emit_stuck_detected(
    team_dir: Path | str,
    issue_id: str,
    reason: str,
    auto_heal_action: str,
) -> None:
    ActivityLog(team_dir).emit(
        "stuck_detected",
        issue_id=issue_id,
        reason=reason,
        auto_heal_action=auto_heal_action,
    )
