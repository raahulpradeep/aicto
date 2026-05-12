"""Tests for the §12 mailbox subsystem."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from event_bus import EventBus  # noqa: E402
from mailbox import Mailbox, Message  # noqa: E402
from state_store import StateStore  # noqa: E402


@pytest.fixture
def store_bus(tmp_path: Path):
    db = tmp_path / "state.db"
    watch = tmp_path / "watch"
    watch.mkdir()
    store = StateStore(db)
    bus = EventBus(store, watch_dir=watch)
    return store, bus


def test_schema_migration_creates_messages_table(store_bus):
    import sqlite3
    store, _ = store_bus
    with sqlite3.connect(str(store.db_path)) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in cur.fetchall()}
    assert "messages" in names


def test_ask_and_listen_role_addressed(store_bus):
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    reviewer = Mailbox(store, bus, "t:review-1", "reviewer")
    msg_id = sender.ask("question", {"q": "?"}, recipient_role="reviewer")
    msg = reviewer.listen()
    assert msg is not None
    assert msg.id == msg_id
    assert msg.kind == "question"
    assert msg.payload == {"q": "?"}
    assert msg.sender == "t:dev-1"


def test_ask_and_listen_slot_addressed(store_bus):
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    target = Mailbox(store, bus, "t:dev-2", "developer")
    other = Mailbox(store, bus, "t:dev-3", "developer")
    msg_id = sender.ask("nudge", {"m": "hi"}, recipient_slot="t:dev-2")
    # Other dev in same role should NOT get it (slot-addressed)
    assert other.listen() is None
    msg = target.listen()
    assert msg is not None and msg.id == msg_id


def test_listen_is_atomic_under_race(store_bus):
    """Two reviewers compete; only one wins the claim."""
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    r1 = Mailbox(store, bus, "t:review-1", "reviewer")
    r2 = Mailbox(store, bus, "t:review-2", "reviewer")
    sender.ask("question", {}, recipient_role="reviewer")
    a = r1.listen()
    b = r2.listen()
    # Exactly one of them gets the message.
    assert (a is None) != (b is None)


def test_slot_takes_priority_over_role(store_bus):
    """Both slot- and role-addressed pending: a listener with matching slot
    claims the slot-addressed one first."""
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    target = Mailbox(store, bus, "t:review-1", "reviewer")
    role_msg = sender.ask("question", {"r": "role"}, recipient_role="reviewer")
    slot_msg = sender.ask("question", {"r": "slot"}, recipient_slot="t:review-1")
    first = target.listen()
    assert first.id == slot_msg
    second = target.listen()
    assert second.id == role_msg


def test_reply_and_wait_reply(store_bus):
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    reviewer = Mailbox(store, bus, "t:review-1", "reviewer")
    msg_id = sender.ask("question", {"q": "?"}, recipient_role="reviewer")
    msg = reviewer.listen()
    reviewer.reply(msg.id, {"verdict": "ack"})
    reply = sender.wait_reply(msg_id, timeout=2.0)
    assert reply == {"verdict": "ack"}


def test_wait_reply_timeout(store_bus):
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    msg_id = sender.ask("question", {}, recipient_role="reviewer")
    reply = sender.wait_reply(msg_id, timeout=0.5)
    assert reply is None


def test_pending_for_filters(store_bus):
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    sender.ask("q1", {}, recipient_role="reviewer")
    sender.ask("q2", {}, recipient_slot="t:review-1")
    by_role = sender.pending_for(role="reviewer")
    assert len(by_role) == 1
    by_slot = sender.pending_for(slot="t:review-1")
    assert len(by_slot) == 1


def test_expire_old(store_bus):
    """expire_old marks rows older than the cutoff as expired."""
    import sqlite3
    store, bus = store_bus
    sender = Mailbox(store, bus, "t:dev-1", "developer")
    mid = sender.ask("q", {}, recipient_role="reviewer")
    # Backdate the row by 48h.
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "UPDATE messages SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (mid,),
        )
        conn.commit()
    n = sender.expire_old(age_hours=24)
    assert n == 1
    msg = sender._get(mid)
    assert msg.status == "expired"


def test_cli_no_bd_no_dolt(tmp_path: Path):
    """Regression guard: the mailbox CLI must never shell out to `bd` or `dolt`.

    We stub both with shims on PATH that exit non-zero and write to a sentinel.
    If the CLI invokes them, the sentinel will be non-empty.
    """
    shimdir = tmp_path / "shim"
    shimdir.mkdir()
    sentinel = tmp_path / "called"
    for name in ("bd", "dolt"):
        shim = shimdir / name
        shim.write_text(
            f"#!/bin/sh\necho '{name}' >> '{sentinel}'\nexit 1\n"
        )
        shim.chmod(0o755)

    tdir = tmp_path / "team"
    (tdir / ".cto" / "state").mkdir(parents=True)

    env = os.environ.copy()
    env["PATH"] = f"{shimdir}:{env['PATH']}"
    env["AICTO_AGENT_ID"] = "t:dev-1"
    env["AICTO_ROLE"] = "developer"
    env["AICTO_TEAM_DIR"] = str(tdir)

    cli = str(REPO_ROOT / "src" / "mailbox_cli.py")
    proc = subprocess.run(
        [sys.executable, cli, "ask",
         "--recipient-role", "reviewer", "--kind", "question",
         "--payload", json.dumps({"q": "?"})],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert not sentinel.exists() or sentinel.read_text() == "", \
        f"CLI invoked bd/dolt: {sentinel.read_text()!r}"
    out = json.loads(proc.stdout)
    assert "msg_id" in out
