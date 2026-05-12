"""Sanity tests for the unified activity feed CLI (§14)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "src" / "activity_cli.py"


def _write_log(tmp_path: Path, rows: list[dict]) -> Path:
    log_dir = tmp_path / ".cto"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "activity.jsonl"
    with open(log, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return log


def _run(tdir: Path, *extra: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(CLI), str(tdir), *extra],
        capture_output=True, text=True, timeout=10,
    )
    return proc.returncode, proc.stdout


def test_basic_read(tmp_path: Path) -> None:
    _write_log(tmp_path, [
        {"ts": "2026-05-12T10:00:00Z", "slot": "dev-1", "kind": "claim", "summary": "claimed spm-abc", "task_id": "spm-abc"},
        {"ts": "2026-05-12T10:00:05Z", "slot": "manager", "kind": "merge", "summary": "merged breakdown"},
    ])
    rc, out = _run(tmp_path)
    assert rc == 0
    assert "dev-1" in out and "manager" in out
    assert "task=spm-abc" in out


def test_agent_filter(tmp_path: Path) -> None:
    _write_log(tmp_path, [
        {"ts": "2026-05-12T10:00:00Z", "slot": "dev-1", "kind": "claim", "summary": "a"},
        {"ts": "2026-05-12T10:00:05Z", "slot": "dev-2", "kind": "claim", "summary": "b"},
    ])
    rc, out = _run(tmp_path, "--agent", "dev-1")
    assert rc == 0
    assert "dev-1" in out and "dev-2" not in out


def test_kind_filter(tmp_path: Path) -> None:
    _write_log(tmp_path, [
        {"ts": "2026-05-12T10:00:00Z", "slot": "dev-1", "kind": "claim", "summary": "a"},
        {"ts": "2026-05-12T10:00:05Z", "slot": "dev-1", "kind": "merge", "summary": "b"},
    ])
    rc, out = _run(tmp_path, "--kind", "merge")
    assert rc == 0
    assert "merge" in out and "claim" not in out.split("\n")[0]


def test_since_filter(tmp_path: Path) -> None:
    _write_log(tmp_path, [
        {"ts": "2026-05-12T09:00:00Z", "slot": "dev-1", "kind": "claim", "summary": "old"},
        {"ts": "2026-05-12T11:00:00Z", "slot": "dev-1", "kind": "claim", "summary": "new"},
    ])
    rc, out = _run(tmp_path, "--since", "2026-05-12T10:00:00Z")
    assert rc == 0
    assert "new" in out and "old" not in out


def test_legacy_event_key_accepted(tmp_path: Path) -> None:
    """Legacy `event=` rows (from _telemetry and post-commit hook) parse cleanly."""
    _write_log(tmp_path, [
        {"ts": "2026-05-12T10:00:00Z", "event": "commit_pushed", "branch": "main",
         "commit_hash": "abc123", "message": "x", "author": "y"},
    ])
    rc, out = _run(tmp_path)
    assert rc == 0
    assert "commit_pushe" in out  # kind column truncates to 12 chars


def test_malformed_lines_ignored(tmp_path: Path) -> None:
    log_dir = tmp_path / ".cto"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "activity.jsonl"
    log.write_text(
        json.dumps({"ts": "2026-05-12T10:00:00Z", "slot": "dev-1", "kind": "claim", "summary": "ok"}) + "\n"
        "{ this is not json\n"
        + json.dumps({"ts": "2026-05-12T10:00:05Z", "slot": "dev-2", "kind": "claim", "summary": "ok2"}) + "\n"
    )
    rc, out = _run(tmp_path)
    assert rc == 0
    assert "ok" in out and "ok2" in out


def test_missing_file_returns_zero(tmp_path: Path) -> None:
    rc, out = _run(tmp_path)
    assert rc == 0
    assert out == ""
