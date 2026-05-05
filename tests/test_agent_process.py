"""Tests for persistent agent process Phase 1.

Run with:  uv run pytest tests/test_agent_process.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Generator

import pytest

from agent_process import AgentConfig, AgentProcess
from event_bus import EventBus, Subscription
from state_store import AgentState, StateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_team_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as td:
        team_dir = Path(td) / "test-team"
        team_dir.mkdir()
        # Scaffold minimal team structure
        (team_dir / ".cto").mkdir()
        (team_dir / ".cto" / "state").mkdir()
        (team_dir / ".cto" / "logs").mkdir()
        (team_dir / ".cto" / "locks").mkdir()
        (team_dir / ".beads").mkdir()
        yield team_dir


@pytest.fixture
def store(tmp_team_dir: Path) -> StateStore:
    db = tmp_team_dir / ".cto" / "state" / "agent_state.db"
    return StateStore(db)


@pytest.fixture
def bus(store: StateStore, tmp_team_dir: Path) -> EventBus:
    return EventBus(store, watch_dir=tmp_team_dir / ".cto" / "state")


@pytest.fixture
def role_prompt(tmp_team_dir: Path) -> Path:
    p = tmp_team_dir / ".cto" / "prompts" / "dev-1.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Developer prompt\nYou are a developer.")
    return p


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------

class TestStateStore:
    def test_save_and_load(self, store: StateStore) -> None:
        state = AgentState(team="test-team", role="developer", slot="dev-1")
        state.iteration_count = 5
        state.scratchpad = "note"
        store.save(state)

        loaded = store.load("test-team", "developer", "dev-1")
        assert loaded is not None
        assert loaded.iteration_count == 5
        assert loaded.scratchpad == "note"

    def test_load_missing_returns_none(self, store: StateStore) -> None:
        assert store.load("x", "y", "z") is None

    def test_load_or_create_creates_defaults(self, store: StateStore) -> None:
        state = store.load_or_create("test-team", "developer", "dev-1", defaults={"model": "opus"})
        assert state.model == "opus"
        assert state.role == "developer"

    def test_list_agents(self, store: StateStore) -> None:
        store.save(AgentState(team="t", role="developer", slot="d1"))
        store.save(AgentState(team="t", role="reviewer", slot="r1"))
        store.save(AgentState(team="u", role="developer", slot="d1"))
        assert len(store.list_agents()) == 3
        assert len(store.list_agents(team="t")) == 2

    def test_delete(self, store: StateStore) -> None:
        store.save(AgentState(team="t", role="developer", slot="d1"))
        assert store.delete("t", "developer", "d1") is True
        assert store.load("t", "developer", "d1") is None
        assert store.delete("t", "developer", "d1") is False

    def test_event_log(self, store: StateStore) -> None:
        e1 = store.append_event("team.t.developer", {"kind": "task_ready", "task_id": "abc"})
        e2 = store.append_event("team.t.developer", {"kind": "task_ready", "task_id": "def"})
        assert e2 > e1

        rows = store.events_since("team.t.developer", e1)
        assert len(rows) == 1
        assert rows[0]["payload"]["task_id"] == "def"

        assert store.latest_event_id("team.t.developer") == e2
        assert store.latest_event_id("nonexistent") == 0

    def test_forward_compat_extras(self, store: StateStore) -> None:
        state = AgentState(team="t", role="r", slot="s")
        state.extras["future_field"] = 123
        store.save(state)
        loaded = store.load("t", "r", "s")
        assert loaded is not None
        assert loaded.extras.get("future_field") == 123


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_publish_and_poll(self, bus: EventBus) -> None:
        sub = bus.subscribe("team.t.developer")
        bus.publish("team.t.developer", {"kind": "task_ready", "id": "t1"})

        events = sub.poll(timeout=1.0)
        assert len(events) == 1
        assert events[0].payload["id"] == "t1"
        sub.close()

    def test_poll_no_events(self, bus: EventBus) -> None:
        sub = bus.subscribe("team.t.developer")
        events = sub.poll(timeout=0.2)
        assert events == []
        sub.close()

    def test_multiple_subscribers_same_topic(self, bus: EventBus) -> None:
        sub1 = bus.subscribe("team.t.developer")
        sub2 = bus.subscribe("team.t.developer")

        bus.publish("team.t.developer", {"kind": "x"})
        time.sleep(0.1)

        ev1 = sub1.poll(timeout=1.0)
        ev2 = sub2.poll(timeout=1.0)
        assert len(ev1) == 1
        assert len(ev2) == 1
        sub1.close()
        sub2.close()

    def test_subscription_context_manager(self, bus: EventBus) -> None:
        with bus.subscribe("team.t.developer") as sub:
            bus.publish("team.t.developer", {"kind": "y"})
            events = sub.poll(timeout=1.0)
            assert len(events) == 1

    def test_events_since_with_cursor(self, store: StateStore) -> None:
        e1 = store.append_event("topic.a", {"n": 1})
        e2 = store.append_event("topic.a", {"n": 2})
        e3 = store.append_event("topic.a", {"n": 3})

        rows = store.events_since("topic.a", e1)
        assert [r["payload"]["n"] for r in rows] == [2, 3]

        rows = store.events_since("topic.a", e3)
        assert rows == []


# ---------------------------------------------------------------------------
# AgentProcess (mocked CLI)
# ---------------------------------------------------------------------------

class _FakeAgentProcess(AgentProcess):
    """Subclass that replaces CLI invocation with a fake for testing."""

    def __init__(self, config: AgentConfig, fake_rc: int = 0, fake_delay: float = 0.0):
        super().__init__(config)
        self.fake_rc = fake_rc
        self.fake_delay = fake_delay
        self.invocations: list[tuple[str, str | None]] = []
        self._bd_db: dict[str, dict[str, Any]] = {}
        self._next_bd_id = 100

    def _invoke_cli(self, starter: str, task_id: str | None = None) -> int:
        self.invocations.append((starter, task_id))
        if self.fake_delay:
            time.sleep(self.fake_delay)
        return self.fake_rc

    def _bd(self, args: list[str], check: bool = True) -> str:
        # Minimal mock of bd CLI behaviour for testing
        cmd = args[0] if args else ""
        if cmd == "ready":
            # Return seeded tasks if available, otherwise auto-generate
            seeded = self._bd_db.get("ready", [])
            if seeded:
                # Consume one seed so subsequent calls don't return the same
                task = seeded.pop(0)
                return json.dumps([task])
            task = {
                "id": f"task-{self._next_bd_id}",
                "title": "Fake task",
                "priority": 2,
                "labels": ["role:developer"],
            }
            self._next_bd_id += 1
            return json.dumps([task])
        if cmd == "show":
            tid = args[1]
            return json.dumps([{"id": tid, "status": "open", "title": "T", "description": "D"}])
        if cmd == "list":
            return json.dumps([])
        if cmd == "update":
            # Return something truthy so _atomic_claim doesn't bail
            return "updated"
        return ""

    def _bd_show_status(self, task_id: str) -> str:
        return "closed"  # pretend agent closed it so crash_count stays 0


class TestAgentProcess:
    def test_state_persists_across_instantiation(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
            idle_sleep=0.05,
        )
        proc1 = _FakeAgentProcess(config)
        proc1.run = lambda: None  # don't actually loop
        proc1.state.iteration_count = 3
        proc1.state.scratchpad = "hello"
        proc1.store.save(proc1.state)

        proc2 = _FakeAgentProcess(config)
        assert proc2.state.iteration_count == 3
        assert proc2.state.scratchpad == "hello"

    def test_context_header_contains_scratchpad(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
        )
        proc = _FakeAgentProcess(config)
        proc.state.scratchpad = "use tabs"
        proc.state.iteration_count = 7
        header = proc._build_context_header("t1", "summary")
        assert "use tabs" in header
        assert "iteration: 7" in header
        assert "agent_id: test-team:dev-1" in header

    def test_legacy_poll_claims_and_executes(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
            idle_sleep=0.05,
        )
        proc = _FakeAgentProcess(config)
        # Seed one ready task in the mock bd db
        proc._bd_db["ready"] = [{"id": "task-99", "priority": 1, "title": "X", "labels": ["role:developer"]}]

        # Run a single iteration by manually invoking the poll path
        sub = proc.bus.subscribe("team.test-team.developer")
        proc._legacy_poll_and_work()
        sub.close()

        # Should have invoked CLI once with the fake task
        assert len(proc.invocations) == 1
        assert proc.invocations[0][1] == "task-99"  # task_id passed

    def test_crash_recovery_resets_zombie_claims(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
        )
        proc = _FakeAgentProcess(config)
        proc.state.status = "crashed"
        proc.state.crash_count = 2
        # Create a stale lock dir
        lock = tmp_team_dir / ".cto" / "locks" / "task-old"
        lock.mkdir()
        (lock / "owner").write_text("test-team:dev-1")

        proc._recover_from_crash()

        assert not lock.exists()
        assert proc.state.status == "running"

    def test_stop_sets_flag(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
        )
        proc = _FakeAgentProcess(config)
        proc.stop()
        assert proc._stop_event.is_set()
        assert proc.state.status == "stopped"

    def test_iteration_count_increments(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
        )
        proc = _FakeAgentProcess(config)
        proc.state.iteration_count = 0
        proc._do_task("task-1")
        assert proc.state.iteration_count == 1
        proc._do_task("task-2")
        assert proc.state.iteration_count == 2

    def test_event_driven_work(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
            idle_sleep=0.05,
        )
        proc = _FakeAgentProcess(config)
        sub = proc.bus.subscribe("team.test-team.developer")

        # Publish a task_ready event
        proc.bus.publish("team.test-team.developer", {"kind": "task_ready", "task_id": "evt-1"})

        # Manually run one iteration via the event path
        proc._iteration(sub)
        sub.close()

        assert len(proc.invocations) == 1
        assert proc.invocations[0][1] == "evt-1"

    def test_manager_pass_does_not_claim(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="manager", slot="manager",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
        )
        proc = _FakeAgentProcess(config)
        proc._do_manager_pass()
        assert len(proc.invocations) == 1
        # Manager pass has no task_id
        assert proc.invocations[0][1] is None


# ---------------------------------------------------------------------------
# Integration-style: run loop briefly then stop
# ---------------------------------------------------------------------------

class TestAgentProcessLoop:
    def test_run_and_stop(self, tmp_team_dir: Path, role_prompt: Path) -> None:
        config = AgentConfig(
            team="test-team", role="developer", slot="dev-1",
            team_dir=tmp_team_dir, role_prompt_path=role_prompt,
            idle_sleep=0.05,
        )
        proc = _FakeAgentProcess(config)

        # Stop after a short delay
        def stopper() -> None:
            time.sleep(0.3)
            proc.stop()

        t = threading.Thread(target=stopper, daemon=True)
        t.start()
        proc.run()
        t.join(timeout=2)

        # Should have done a few idle iterations (each ~50ms)
        assert proc.state.iteration_count >= 1
        assert not proc.is_running()
