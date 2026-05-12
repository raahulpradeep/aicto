"""Tests for §17 CTO mailbox inbox + §18 nudge CLI."""
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
from mailbox import Mailbox  # noqa: E402
from state_store import StateStore  # noqa: E402


CTO_INBOX_CLI = str(REPO_ROOT / "src" / "cto_inbox_cli.py")
NUDGE_CLI = str(REPO_ROOT / "src" / "nudge_cli.py")


def _make_team(tmp_path: Path) -> tuple[Path, StateStore, EventBus]:
    tdir = tmp_path / "team"
    (tdir / ".cto" / "state").mkdir(parents=True)
    db = tdir / ".cto" / "state" / "agent_state.db"
    store = StateStore(db)
    bus = EventBus(store, watch_dir=tdir / ".cto" / "state")
    return tdir, store, bus


def _run(cli: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, cli, *args],
        capture_output=True, text=True, timeout=10,
    )


def test_nudge_files_a_mailbox_row(tmp_path: Path):
    tdir, store, bus = _make_team(tmp_path)
    proc = _run(NUDGE_CLI, str(tdir), "spm", "dev-1", "stop refactoring metrics")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["recipient"] == "spm:dev-1"
    assert out["kind"] == "nudge"
    # Verify the row landed
    target = Mailbox(store, bus, "spm:dev-1", "developer")
    msg = target.listen()
    assert msg is not None
    assert msg.kind == "nudge"
    assert msg.payload["message"] == "stop refactoring metrics"
    assert msg.payload["from"] == "cto"


def test_cto_inbox_lists_cto_addressed_messages(tmp_path: Path):
    tdir, store, bus = _make_team(tmp_path)
    mgr = Mailbox(store, bus, "spm:manager", "manager")
    mgr.ask("question", {"question": "approve early scope cut?"}, recipient_role="cto")
    proc = _run(CTO_INBOX_CLI, str(tdir), "--json")
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)
    assert len(rows) == 1
    assert rows[0]["kind"] == "question"
    assert rows[0]["sender"] == "spm:manager"


def test_cto_inbox_unread_filter(tmp_path: Path):
    tdir, store, bus = _make_team(tmp_path)
    mgr = Mailbox(store, bus, "spm:manager", "manager")
    mid = mgr.ask("question", {"question": "?"}, recipient_role="cto")
    # First listing marks as read.
    _run(CTO_INBOX_CLI, str(tdir), "--json")
    # Now --unread should be empty.
    proc = _run(CTO_INBOX_CLI, str(tdir), "--json", "--unread")
    rows = json.loads(proc.stdout)
    assert rows == []


def test_cto_inbox_ack(tmp_path: Path):
    tdir, store, bus = _make_team(tmp_path)
    mgr = Mailbox(store, bus, "spm:manager", "manager")
    mid = mgr.ask("note", {"note": "fyi"}, recipient_role="cto")
    proc = _run(CTO_INBOX_CLI, str(tdir), "--ack", str(mid))
    assert proc.returncode == 0
    proc2 = _run(CTO_INBOX_CLI, str(tdir), "--json", "--unread")
    rows = json.loads(proc2.stdout)
    assert rows == []


def test_cto_inbox_reply(tmp_path: Path):
    tdir, store, bus = _make_team(tmp_path)
    mgr = Mailbox(store, bus, "spm:manager", "manager")
    mid = mgr.ask("question", {"question": "?"}, recipient_role="cto")
    proc = _run(
        CTO_INBOX_CLI, str(tdir),
        "--reply", str(mid), "--payload", json.dumps({"verdict": "ok"}),
    )
    assert proc.returncode == 0, proc.stderr
    reply = mgr.wait_reply(mid, timeout=1.0)
    # No event-bus topic fired (no event-bus emit from CLI), so the wait may
    # poll the DB; the row must still be 'replied' regardless.
    msg = mgr._get(mid)
    assert msg.status == "replied"
    assert msg.reply_payload == {"verdict": "ok"}


def test_read_at_migration_is_idempotent(tmp_path: Path):
    """Re-running the CLI on a DB whose `read_at` column already exists
    must not error."""
    tdir, _, _ = _make_team(tmp_path)
    proc1 = _run(CTO_INBOX_CLI, str(tdir), "--json")
    proc2 = _run(CTO_INBOX_CLI, str(tdir), "--json")
    assert proc1.returncode == 0 and proc2.returncode == 0
