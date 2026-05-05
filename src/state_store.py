"""SQLite-backed state persistence for persistent agent processes.

Each agent process is identified by (team, role, slot).
State is stored as JSON so schemas can evolve without migrations.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class AgentState:
    """Mutable working state for a single agent process."""

    team: str
    role: str
    slot: str
    status: str = "idle"  # idle | running | crashed | stopped
    current_task_id: Optional[str] = None
    iteration_count: int = 0
    model: str = "sonnet"
    agent_provider: str = "claude"
    permission_mode: str = "bypassPermissions"
    team_dir: str = ""
    role_prompt_path: str = ""
    # Accumulated context window contents (synthetic, not raw LLM state)
    accumulated_context: dict[str, Any] = field(default_factory=dict)
    # Agent scratchpad for notes across iterations
    scratchpad: str = ""
    # Last event the agent successfully processed
    last_event_id: int = 0
    # Timestamp of last run iteration
    last_run_at: Optional[str] = None
    # Crash recovery: number of consecutive crashes
    crash_count: int = 0
    # Extra fields for forward compatibility
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_id(self) -> str:
        return f"{self.team}:{self.slot}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentState:
        # Filter to known fields, stuff the rest into extras
        known = {f.name for f in cls.__dataclass_fields__.values()}
        core = {k: v for k, v in data.items() if k in known}
        extras = {k: v for k, v in data.items() if k not in known}
        core.setdefault("extras", extras)
        return cls(**core)


class StateStore:
    """Thread-safe SQLite store for agent state."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS agent_state (
        team       TEXT NOT NULL,
        role       TEXT NOT NULL,
        slot       TEXT NOT NULL,
        data       TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (team, role, slot)
    );

    CREATE TABLE IF NOT EXISTS event_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        topic      TEXT NOT NULL,
        payload    TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_event_log_topic ON event_log(topic);
    CREATE INDEX IF NOT EXISTS idx_event_log_created ON event_log(created_at);
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(self.SCHEMA)

    # ------------------------------------------------------------------
    # Agent state
    # ------------------------------------------------------------------

    def save(self, state: AgentState) -> None:
        """Upsert agent state atomically."""
        now = datetime.now(timezone.utc).isoformat()
        data = json.dumps(state.to_dict(), separators=(",", ":"), default=str)
        c = self._conn()
        c.execute(
            """
            INSERT INTO agent_state (team, role, slot, data, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(team, role, slot) DO UPDATE SET
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (state.team, state.role, state.slot, data, now),
        )
        c.commit()

    def load(self, team: str, role: str, slot: str) -> Optional[AgentState]:
        """Load agent state, or None if never saved."""
        row = self._conn().execute(
            "SELECT data FROM agent_state WHERE team=? AND role=? AND slot=?",
            (team, role, slot),
        ).fetchone()
        if row is None:
            return None
        return AgentState.from_dict(json.loads(row["data"]))

    def load_or_create(
        self,
        team: str,
        role: str,
        slot: str,
        defaults: Optional[dict[str, Any]] = None,
    ) -> AgentState:
        """Load existing state or create a fresh record with defaults."""
        existing = self.load(team, role, slot)
        if existing:
            return existing
        state = AgentState(team=team, role=role, slot=slot)
        if defaults:
            for k, v in defaults.items():
                if hasattr(state, k):
                    setattr(state, k, v)
                else:
                    state.extras[k] = v
        self.save(state)
        return state

    def list_agents(self, team: Optional[str] = None) -> list[AgentState]:
        """List all persisted agents, optionally filtered by team."""
        c = self._conn()
        if team:
            rows = c.execute(
                "SELECT data FROM agent_state WHERE team=?", (team,)
            ).fetchall()
        else:
            rows = c.execute("SELECT data FROM agent_state").fetchall()
        return [AgentState.from_dict(json.loads(r["data"])) for r in rows]

    def delete(self, team: str, role: str, slot: str) -> bool:
        """Delete an agent state. Returns True if it existed."""
        c = self._conn()
        cur = c.execute(
            "DELETE FROM agent_state WHERE team=? AND role=? AND slot=?",
            (team, role, slot),
        )
        c.commit()
        return cur.rowcount > 0

    def cleanup_old(self, max_age_days: int = 7) -> int:
        """Remove agent states not updated in N days. Returns count removed."""
        cutoff = datetime.now(timezone.utc).isoformat()
        # SQLite can't do date math in pure SQL without extensions, so
        # we fetch and filter in Python.  For large fleets this could
        # be optimised with a strftime comparison.
        all_agents = self.list_agents()
        removed = 0
        for a in all_agents:
            if a.last_run_at is None:
                continue
            try:
                last = datetime.fromisoformat(a.last_run_at)
                age = datetime.now(timezone.utc) - last
                if age.days > max_age_days:
                    self.delete(a.team, a.role, a.slot)
                    removed += 1
            except ValueError:
                continue
        return removed

    # ------------------------------------------------------------------
    # Generic event log (used by EventBus for durability)
    # ------------------------------------------------------------------

    def append_event(self, topic: str, payload: dict[str, Any]) -> int:
        """Append an event to the log. Returns the assigned event id."""
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, separators=(",", ":"), default=str)
        c = self._conn()
        cur = c.execute(
            "INSERT INTO event_log (topic, payload, created_at) VALUES (?, ?, ?)",
            (topic, payload_json, now),
        )
        c.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def events_since(self, topic: str, last_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch events for a topic newer than last_id."""
        rows = self._conn().execute(
            """
            SELECT id, payload, created_at
            FROM event_log
            WHERE topic = ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (topic, last_id, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def latest_event_id(self, topic: str) -> int:
        """Return the highest event id for a topic, or 0."""
        row = self._conn().execute(
            "SELECT MAX(id) FROM event_log WHERE topic = ?", (topic,)
        ).fetchone()
        return row[0] or 0
