#!/usr/bin/env python3
"""§17: CTO mailbox inbox.

Lists mailbox messages where the CTO is the recipient (role='cto' or
slot='cto'). Tracks read state via a `read_at` column added lazily
(idempotent ALTER TABLE on first use). No bd / Dolt traffic.

Usage (from bin/cto):
    cto inbox <team> --mailbox [--unread] [--ack <id>]
                     [--reply <id> --payload <json>] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_read_at(conn: sqlite3.Connection) -> None:
    """Idempotently add a `read_at` column for tracking CTO ack state."""
    cur = conn.execute("PRAGMA table_info(messages)")
    cols = {r[1] for r in cur.fetchall()}
    if "read_at" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
        conn.commit()


def _open(team_dir: Path) -> sqlite3.Connection:
    db = team_dir / ".cto" / "state" / "agent_state.db"
    if not db.exists():
        print(json.dumps({"error": f"no mailbox state DB at {db}"}), file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _ensure_read_at(conn)
    return conn


def _rows_for_cto(conn: sqlite3.Connection, unread_only: bool) -> list[sqlite3.Row]:
    where = "(recipient_role='cto' OR recipient_slot='cto')"
    if unread_only:
        where += " AND read_at IS NULL"
    return list(conn.execute(
        f"SELECT * FROM messages WHERE {where} ORDER BY id DESC"
    ).fetchall())


def _print_rows(rows: list[sqlite3.Row], as_json: bool) -> None:
    if as_json:
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "sender": r["sender"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]) if r["payload"] else {},
                "status": r["status"],
                "reply_payload": json.loads(r["reply_payload"]) if r["reply_payload"] else None,
                "created_at": r["created_at"],
                "read_at": r["read_at"],
            })
        print(json.dumps(out, indent=2))
        return
    if not rows:
        print("(no CTO mailbox messages)")
        return
    for r in rows:
        payload = json.loads(r["payload"]) if r["payload"] else {}
        flag = " " if r["read_at"] else "*"
        print(f"[{flag}] #{r['id']:>4}  {r['sender']:<24}  {r['kind']:<12}  {r['created_at']}")
        msg = payload.get("message") or payload.get("question") or payload.get("note")
        if msg:
            print(f"      └─ {msg}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CTO mailbox inbox (§17).")
    ap.add_argument("team_dir", help="Path to <repo>/teams/<name>")
    ap.add_argument("--unread", action="store_true")
    ap.add_argument("--ack", type=int, help="Mark a single message read")
    ap.add_argument("--reply", type=int, help="Reply to a message id")
    ap.add_argument("--payload", help="JSON object string for --reply")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args(argv)

    tdir = Path(args.team_dir).resolve()
    conn = _open(tdir)

    if args.ack is not None:
        conn.execute("UPDATE messages SET read_at=? WHERE id=?", (_now(), args.ack))
        conn.commit()
        print(json.dumps({"ok": True, "ack": args.ack}))
        return 0

    if args.reply is not None:
        if not args.payload:
            print(json.dumps({"error": "--reply requires --payload"}), file=sys.stderr)
            return 2
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"--payload is not JSON: {e}"}), file=sys.stderr)
            return 2
        conn.execute(
            "UPDATE messages SET status='replied', reply_payload=?, replied_at=?, read_at=COALESCE(read_at, ?)"
            " WHERE id=?",
            (json.dumps(payload), _now(), _now(), args.reply),
        )
        conn.commit()
        print(json.dumps({"ok": True, "replied": args.reply}))
        return 0

    rows = _rows_for_cto(conn, args.unread)
    _print_rows(rows, args.as_json)

    # Default listing (no --unread) marks viewed rows as read.
    if not args.unread and rows:
        ids = [r["id"] for r in rows if r["read_at"] is None]
        if ids:
            qmarks = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE messages SET read_at=? WHERE id IN ({qmarks})",
                (_now(), *ids),
            )
            conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
