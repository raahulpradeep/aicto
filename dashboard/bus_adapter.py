"""Event bus adapter for the dashboard.

Bridges the src/event_bus.py EventBus to the TUI dashboard widgets.
Instead of polling bd every second, we subscribe to event bus topics
and push updates to reactive widgets.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

# The event_bus and state_store live in src/ which is on sys.path when
# ctodashboard.py runs.
try:
    from event_bus import EventBus, Subscription
    from state_store import StateStore
    _HAS_BUS = True
except Exception:
    _HAS_BUS = False

TEAMS_DIR = Path(__file__).resolve().parent.parent / "teams"


class DashboardEventAdapter:
    """Subscribes to all team event buses and forwards events to callbacks.

    Falls back to file-based JSONL tailing if the SQLite event bus is
    not available (e.g., older teams without agent_state.db).
    """

    def __init__(self, callback: Callable[[dict[str, Any]], None]):
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._running = False
        self._bus_subs: list[Any] = []
        self._file_offsets: dict[Path, int] = {}
        self._last_file_poll = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        for sub in self._bus_subs:
            try:
                sub.close()
            except Exception:
                pass
        self._bus_subs.clear()

    # ------------------------------------------------------------------
    # Main loop: hybrid SQLite bus + JSONL file fallback
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Poll SQLite buses (fast) and JSONL files (fallback) every 100ms."""
        while self._running:
            t0 = time.monotonic()
            self._poll_sqlite_buses()
            self._poll_jsonl_files()
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, 0.1 - elapsed)
            time.sleep(sleep_for)

    # ------------------------------------------------------------------
    # SQLite event bus (Phase 1-2 backend)
    # ------------------------------------------------------------------

    def _poll_sqlite_buses(self) -> None:
        if not _HAS_BUS:
            return
        for team_dir in self._team_dirs():
            db_path = team_dir / ".cto" / "agent_state.db"
            if not db_path.exists():
                continue
            try:
                store = StateStore(str(db_path))
                bus = EventBus(store, watch_dir=team_dir / ".cto")
                # Grab all events since we last looked. We use a dummy
                # subscription that starts from the latest id so we only
                # get NEW events.
                sub = bus.subscribe("#")  # wildcard-ish: poll all topics
                # Actually EventBus.subscribe takes a topic string.
                # We iterate known topics.
                for topic in (
                    "agent.lifecycle",
                    "issue.state",
                    "epic.pipeline",
                    "ci.status",
                    "token.usage",
                    "cto.decision",
                ):
                    sub = bus.subscribe(topic)
                    events = sub.poll(timeout=0.0, batch_size=50)
                    for ev in events:
                        self._notify({
                            "ts": ev.created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "topic": ev.topic,
                            "team": team_dir.name,
                            **ev.payload,
                        })
                    sub.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # JSONL file fallback (legacy teams / supervisor telemetry)
    # ------------------------------------------------------------------

    def _poll_jsonl_files(self) -> None:
        for team_dir in self._team_dirs():
            log_path = team_dir / ".cto" / "activity.jsonl"
            if not log_path.exists():
                continue
            try:
                offset = self._file_offsets.get(log_path, 0)
                with open(log_path, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    lines = f.readlines()
                    new_offset = f.tell()
                if lines:
                    self._file_offsets[log_path] = new_offset
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ev["team"] = team_dir.name
                        self._notify(ev)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _team_dirs(self) -> list[Path]:
        if not TEAMS_DIR.is_dir():
            return []
        return [
            p for p in sorted(TEAMS_DIR.iterdir())
            if p.is_dir() and (p / ".cto").is_dir()
        ]

    def _notify(self, event: dict[str, Any]) -> None:
        try:
            self.callback(event)
        except Exception:
            pass

    def _init_file_offsets(self) -> None:
        """Seek to end of existing JSONL files so we only see NEW events."""
        for team_dir in self._team_dirs():
            log_path = team_dir / ".cto" / "activity.jsonl"
            if log_path.exists():
                try:
                    self._file_offsets[log_path] = log_path.stat().st_size
                except Exception:
                    self._file_offsets[log_path] = 0


class _BatchingCallback:
    """Batches rapid-fire events into a single callback every 50ms.

    This prevents 5 events that arrive within 20ms from triggering 5
    separate UI re-renders.
    """

    def __init__(self, callback: Callable[[list[dict[str, Any]]], None]):
        self.callback = callback
        self._pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(event)
            if self._timer is None:
                self._timer = threading.Timer(0.05, self._flush)
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            batch = self._pending[:]
            self._pending.clear()
            self._timer = None
        if batch:
            self.callback(batch)


def make_batched_adapter(callback: Callable[[list[dict[str, Any]]], None]) -> DashboardEventAdapter:
    """Create an adapter that batches events before calling your callback."""
    batcher = _BatchingCallback(callback)
    return DashboardEventAdapter(batcher)
