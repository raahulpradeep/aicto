"""Regression tests for dashboard/top.py panel builders and helpers.

Tests that do NOT depend on a live terminal or running tmux session —
everything is driven by synthetic data.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.panel import Panel

# ---------------------------------------------------------------------------
# Bootstrap: import top.py without executing __main__
# ---------------------------------------------------------------------------

_TOP_PY = Path(__file__).resolve().parent.parent / "dashboard" / "top.py"


def _load_top():
    spec = importlib.util.spec_from_file_location("top", _TOP_PY)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


top = _load_top()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def _issue(
    id: str = "test-abc",
    title: str = "Test issue",
    labels: list[str] | None = None,
    assignee: str = "",
    priority: int = 2,
    created_at: str = "2026-01-01T10:00:00Z",
    closed_at: str = "",
    started_at: str = "",
    stale: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": id,
        "title": title,
        "labels": labels or [],
        "assignee": assignee,
        "priority": priority,
        "created_at": created_at,
        "closed_at": closed_at,
        "started_at": started_at,
    }
    if stale:
        row["stale"] = True
    return row


def _agent_row(
    team: str = "myteam",
    window: str = "manager",
    issue: dict | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "agent": f"{team}:{window}",
        "team": team,
        "window": window,
        "issue": issue,
        "provider": "claude",
        "model": "claude-sonnet-4-6",
    }
    if stale:
        row["stale"] = True
    return row


# ---------------------------------------------------------------------------
# Panel builder contract tests
# ---------------------------------------------------------------------------


class TestAgentPanel:
    def test_empty_returns_panel(self):
        result = top._agent_panel([], [], NOW)
        assert isinstance(result, Panel)

    def test_with_data_returns_panel(self):
        agents = [_agent_row(issue=_issue(assignee="myteam:manager"))]
        result = top._agent_panel(agents, ["myteam"], NOW)
        assert isinstance(result, Panel)

    def test_multiple_teams(self):
        agents = [
            _agent_row(team="alpha", window="dev-1"),
            _agent_row(team="beta", window="manager"),
        ]
        result = top._agent_panel(agents, ["alpha", "beta"], NOW)
        assert isinstance(result, Panel)

    def test_idle_agent(self):
        agents = [_agent_row(issue=None)]
        result = top._agent_panel(agents, ["myteam"], NOW)
        assert isinstance(result, Panel)

    def test_working_agent(self):
        issue = _issue(
            assignee="myteam:dev-1",
            started_at="2026-01-01T11:30:00Z",
        )
        agents = [_agent_row(issue=issue)]
        result = top._agent_panel(agents, ["myteam"], NOW)
        assert isinstance(result, Panel)


class TestInboxPanel:
    def test_empty_returns_panel(self):
        result = top._inbox_panel([])
        assert isinstance(result, Panel)

    def test_with_data_returns_panel(self):
        inbox = [{"team": "myteam", **_issue(labels=["role:cto", "kind:approval"])}]
        result = top._inbox_panel(inbox)
        assert isinstance(result, Panel)


class TestOpenPanel:
    def test_empty_returns_panel(self):
        result = top._open_panel([], NOW)
        assert isinstance(result, Panel)

    def test_with_data_returns_panel(self):
        tasks = [{"team": "myteam", **_issue(labels=["role:developer", "kind:dev"])}]
        result = top._open_panel(tasks, NOW)
        assert isinstance(result, Panel)

    def test_many_teams(self):
        tasks = [{"team": f"team{i}", **_issue(id=f"t-{i}")} for i in range(20)]
        result = top._open_panel(tasks, NOW)
        assert isinstance(result, Panel)

    def test_very_long_title_truncated(self):
        long = "A" * 200
        tasks = [{"team": "t", **_issue(title=long)}]
        result = top._open_panel(tasks, NOW)
        assert isinstance(result, Panel)

    def test_unicode_in_titles(self):
        tasks = [{"team": "t", **_issue(title="日本語 テスト 🚀")}]
        result = top._open_panel(tasks, NOW)
        assert isinstance(result, Panel)


class TestClosedPanel:
    def test_empty_returns_panel(self):
        result = top._closed_panel([])
        assert isinstance(result, Panel)

    def test_with_data_returns_panel(self):
        closed = [
            {
                "team": "myteam",
                **_issue(
                    labels=["role:developer", "verdict:approved"],
                    closed_at="2026-01-01T11:00:00Z",
                ),
            }
        ]
        result = top._closed_panel(closed)
        assert isinstance(result, Panel)

    def test_many_rows(self):
        closed = [
            {"team": "t", **_issue(id=f"t-{i}", closed_at="2026-01-01T11:00:00Z")}
            for i in range(50)
        ]
        result = top._closed_panel(closed)
        assert isinstance(result, Panel)


# ---------------------------------------------------------------------------
# Stale-data tests
# ---------------------------------------------------------------------------


class TestStaleRows:
    def _render_to_text(self, **kwargs) -> str:
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        c = Console(file=buf, width=120, no_color=True, highlight=False)
        panel = top._agent_panel(**kwargs)
        c.print(panel)
        return buf.getvalue()

    def test_stale_agent_shows_tilde_prefix(self):
        agents = [_agent_row(team="myteam", stale=True)]
        text = self._render_to_text(agents=agents, running=["myteam"], now=NOW)
        assert "~myteam" in text

    def test_stale_open_task_shows_tilde_prefix(self):
        row = {"team": "myteam", "stale": True, **_issue()}
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        c = Console(file=buf, width=120, no_color=True, highlight=False)
        panel = top._open_panel([row], NOW)
        c.print(panel)
        text = buf.getvalue()
        assert "~myteam" in text


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestHumanDuration:
    def test_seconds(self):
        assert top._human_duration(45) == "45s"

    def test_minutes(self):
        assert top._human_duration(90) == "1m 30s"

    def test_minutes_no_seconds(self):
        assert top._human_duration(600) == "10m"

    def test_hours(self):
        assert top._human_duration(3661) == "1h 1m"

    def test_days(self):
        assert top._human_duration(86400) == "1d"

    def test_negative(self):
        assert top._human_duration(-1) == "—"

    def test_zero(self):
        assert top._human_duration(0) == "0s"


class TestKindLabel:
    def test_extracts_kind(self):
        assert top._kind(["role:developer", "kind:dev"]) == "dev"

    def test_no_kind_returns_dash(self):
        assert top._kind(["role:developer"]) == "—"

    def test_empty_labels(self):
        assert top._kind([]) == "—"

    def test_none_labels(self):
        assert top._kind(None) == "—"


class TestClassifyClosed:
    def test_cto_approval(self):
        issue = {"labels": ["verdict:approved", "role:cto"]}
        assert top._classify_closed(issue) == "CTO"

    def test_cto_rejection(self):
        issue = {"labels": ["verdict:rejected"]}
        assert top._classify_closed(issue) == "CTO"

    def test_role_developer(self):
        issue = {"labels": ["role:developer", "kind:dev"]}
        assert top._classify_closed(issue) == "developer"

    def test_no_labels(self):
        issue = {"labels": []}
        assert top._classify_closed(issue) == "—"


class TestTruncate:
    def test_short_string_unchanged(self):
        assert top._truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        assert top._truncate("hello", 5) == "hello"

    def test_long_string_truncated(self):
        result = top._truncate("hello world", 8)
        assert len(result) == 8
        assert result.endswith("…")


class TestParseIso:
    def test_valid_iso(self):
        result = top._parse_iso("2026-01-01T12:00:00Z")
        assert result is not None
        assert result.year == 2026

    def test_empty_string(self):
        assert top._parse_iso("") is None

    def test_none_string(self):
        assert top._parse_iso(None) is None


class TestIsStale:
    """_is_meta should not be confused with stale; separate concerns."""

    def test_meta_label_detected(self):
        assert top._is_meta(["kind:status-digest"]) is True

    def test_non_meta_not_detected(self):
        assert top._is_meta(["kind:dev", "role:developer"]) is False

    def test_empty(self):
        assert top._is_meta([]) is False
