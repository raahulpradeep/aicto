"""Tests for dashboard/telemetry.py."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path

from dashboard.telemetry import ActivityLog


def test_emit_and_tail():
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / ".cto").mkdir()
        log = ActivityLog(tdir)
        log.emit("issue_created", issue_id="abc-123", kind="dev")
        log.emit("agent_iteration_start", agent="demo:dev-1", task_id="abc-123")

        events = log.tail(n=10)
        assert len(events) == 2
        assert events[0]["event"] == "issue_created"
        assert events[0]["issue_id"] == "abc-123"
        assert events[1]["event"] == "agent_iteration_start"


def test_tail_filter_by_event_type():
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / ".cto").mkdir()
        log = ActivityLog(tdir)
        log.emit("issue_created", issue_id="a")
        log.emit("agent_iteration_start", agent="x")
        log.emit("issue_created", issue_id="b")

        events = log.tail(n=10, event_types={"issue_created"})
        assert len(events) == 2
        assert all(e["event"] == "issue_created" for e in events)


def test_tail_since():
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / ".cto").mkdir()
        log = ActivityLog(tdir)
        log.emit("old_event")
        cutoff = dt.datetime.now(dt.timezone.utc)
        log.emit("new_event")

        events = log.tail(n=10, since=cutoff)
        assert len(events) == 1
        assert events[0]["event"] == "new_event"


def test_rotation():
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / ".cto").mkdir()
        log = ActivityLog(tdir)
        # Write an old event by manipulating the file directly
        old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
        with open(log.log_path, "w") as f:
            f.write(json.dumps({"ts": old_ts, "event": "ancient"}) + "\n")
        log.emit("fresh")
        log._rotate_if_needed()

        events = log.tail(n=10)
        assert len(events) == 1
        assert events[0]["event"] == "fresh"
