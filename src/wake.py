#!/usr/bin/env python3
"""Interruptible sleep — wake on bd state change or event-bus activity.

Usage: wake.py <team-dir> <max-seconds>

Replaces dumb `sleep N` calls in supervisor loops. Exits within ~150ms of a
bd issue change or new event-bus event, OR after `max-seconds` elapsed.
Always exits 0; the caller continues regardless of the wake reason.

Cheap implementation: stat() the bd journal + the event_bus sqlite WAL
every 150ms. No external deps.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

POLL = 0.15  # 150ms — fast enough to feel real-time


def _watch_paths(team_dir: Path) -> list[Path]:
    paths = []
    for rel in (
        ".beads/issues.jsonl",
        ".beads/issues.db",
        ".beads/issues.db-wal",
        ".cto/agent_state.db",
        ".cto/agent_state.db-wal",
        ".cto/activity.jsonl",
    ):
        p = team_dir / rel
        if p.exists():
            paths.append(p)
    return paths


def _snapshot(paths: list[Path]) -> tuple:
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), 0, 0))
    return tuple(out)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: wake.py <team-dir> <max-seconds>", file=sys.stderr)
        return 2
    team_dir = Path(sys.argv[1])
    try:
        max_secs = float(sys.argv[2])
    except ValueError:
        return 2
    if max_secs <= 0:
        return 0

    paths = _watch_paths(team_dir)
    if not paths:
        time.sleep(max_secs)
        return 0

    baseline = _snapshot(paths)
    deadline = time.monotonic() + max_secs
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        time.sleep(min(POLL, remaining))
        # Re-snapshot only the paths we have (don't re-glob every tick).
        if _snapshot(paths) != baseline:
            return 0


if __name__ == "__main__":
    sys.exit(main())
