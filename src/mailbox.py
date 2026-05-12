"""§12: agent-to-agent in-process mailbox.

Routes coordination chatter (dev→reviewer questions, CTO nudges/FYIs) off bd
so it doesn't generate Dolt commits per round-trip. Durable lifecycle (claim,
close, dep, approval, merge) still lives in bd — the mailbox is **chatter,
not audit trail**.

Routing:
- `recipient_slot="team:slot"`  — specific agent.
- `recipient_role="developer"`  — any agent in the role pool; first claim wins.
- Both set: slot-addressed claim takes priority.

Wake-up: every send publishes one of two event-bus topics
(`mbox.recipient.slot.<slot>` or `mbox.recipient.role.<role>`) so the
recipient's persistent supervisor wakes within ~150 ms.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Message:
    id: int
    sender: str
    recipient_slot: Optional[str]
    recipient_role: Optional[str]
    kind: str
    payload: dict
    status: str
    claimed_by: Optional[str]
    reply_payload: Optional[dict]
    created_at: str
    claimed_at: Optional[str]
    replied_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        return cls(
            id=row["id"],
            sender=row["sender"],
            recipient_slot=row["recipient_slot"],
            recipient_role=row["recipient_role"],
            kind=row["kind"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            status=row["status"],
            claimed_by=row["claimed_by"],
            reply_payload=json.loads(row["reply_payload"]) if row["reply_payload"] else None,
            created_at=row["created_at"],
            claimed_at=row["claimed_at"],
            replied_at=row["replied_at"],
        )


class Mailbox:
    """Per-agent handle on the team's `messages` table."""

    def __init__(self, store: Any, bus: Any, agent_id: str, role: str):
        # store: StateStore — duck-typed; we only need .db_path.
        # bus: EventBus — we use .publish() and .subscribe().
        self.store = store
        self.bus = bus
        self.agent_id = agent_id
        self.role = role

    # ------------------------------------------------------------------ send

    def ask(
        self,
        kind: str,
        payload: dict,
        recipient_slot: Optional[str] = None,
        recipient_role: Optional[str] = None,
    ) -> int:
        if not (recipient_slot or recipient_role):
            raise ValueError("ask() requires recipient_slot or recipient_role")
        with sqlite3.connect(str(self.store.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "INSERT INTO messages (sender, recipient_slot, recipient_role, kind, payload, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (self.agent_id, recipient_slot, recipient_role, kind,
                 json.dumps(payload, default=str), _now()),
            )
            msg_id = int(cur.lastrowid)
            conn.commit()
        # Wake the recipient(s) via the event bus.
        if recipient_slot:
            self.bus.publish(f"mbox.recipient.slot.{recipient_slot}",
                             {"msg_id": msg_id, "kind": kind})
        if recipient_role:
            self.bus.publish(f"mbox.recipient.role.{recipient_role}",
                             {"msg_id": msg_id, "kind": kind})
        return msg_id

    # ---------------------------------------------------------------- listen

    def listen(self) -> Optional[Message]:
        """Atomically claim one pending message addressed to this agent (by slot
        or by role). Slot-addressed messages take priority."""
        with sqlite3.connect(str(self.store.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            # Slot-addressed first.
            for col, val in (("recipient_slot", self.agent_id),
                             ("recipient_role", self.role)):
                # Conditional UPDATE — guarantees only one listener wins.
                cur = conn.execute(
                    f"UPDATE messages SET status='claimed', claimed_by=?, claimed_at=?"
                    f" WHERE id = (SELECT id FROM messages WHERE {col}=? AND status='pending'"
                    f"             ORDER BY id LIMIT 1)"
                    f" RETURNING *",
                    (self.agent_id, _now(), val),
                )
                row = cur.fetchone()
                conn.commit()
                if row:
                    return Message.from_row(row)
        return None

    # ----------------------------------------------------------------- reply

    def reply(self, msg_id: int, payload: dict) -> None:
        with sqlite3.connect(str(self.store.db_path)) as conn:
            conn.execute(
                "UPDATE messages SET status='replied', reply_payload=?, replied_at=?"
                " WHERE id=?",
                (json.dumps(payload, default=str), _now(), msg_id),
            )
            conn.commit()
        self.bus.publish(f"mbox.reply.{msg_id}", {"msg_id": msg_id})

    def wait_reply(self, msg_id: int, timeout: float = 60.0) -> Optional[dict]:
        sub = self.bus.subscribe(f"mbox.reply.{msg_id}")
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                row = self._get(msg_id)
                if row and row.status == "replied":
                    return row.reply_payload
                remaining = max(0.1, deadline - time.time())
                sub.poll(timeout=min(remaining, 5.0))
            row = self._get(msg_id)
            return row.reply_payload if row and row.status == "replied" else None
        finally:
            sub.close()

    # -------------------------------------------------------------- inspect

    def _get(self, msg_id: int) -> Optional[Message]:
        with sqlite3.connect(str(self.store.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            return Message.from_row(row) if row else None

    def pending_for(
        self,
        *,
        slot: Optional[str] = None,
        role: Optional[str] = None,
    ) -> list[Message]:
        clauses = ["status='pending'"]
        params: list[Any] = []
        if slot:
            clauses.append("recipient_slot=?")
            params.append(slot)
        if role:
            clauses.append("recipient_role=?")
            params.append(role)
        sql = f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY id"
        with sqlite3.connect(str(self.store.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [Message.from_row(r) for r in rows]

    def expire_old(self, age_hours: int = 24) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - age_hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        with sqlite3.connect(str(self.store.db_path)) as conn:
            cur = conn.execute(
                "UPDATE messages SET status='expired'"
                " WHERE status IN ('pending', 'claimed') AND created_at < ?",
                (cutoff_iso,),
            )
            conn.commit()
            return cur.rowcount or 0
