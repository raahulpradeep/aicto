#!/usr/bin/env python3
"""Unified activity-feed reader for a team's `.cto/activity.jsonl`.

Used by `cto activity <team>` to tail what every agent in a team is doing —
tool calls, iteration boundaries, claims, mailbox sends/recvs, errors —
without `tail -f`-ing five separate tmux panes.

Schema (per line):
    ts, team, agent, role, slot, kind, summary, task_id, [extras]

Legacy `event=...` rows (from the older `_telemetry` writer and the
post-commit hook) are accepted; the `event` value substitutes for `kind`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_kind(row: dict) -> str:
    return row.get("kind") or row.get("event") or "—"


def _row_summary(row: dict) -> str:
    if "summary" in row and row["summary"]:
        return str(row["summary"])
    extras = row.get("extras") or {}
    bits = []
    for k in ("message", "msg", "branch", "commit_hash"):
        if k in row:
            bits.append(f"{k}={row[k]}")
        elif k in extras:
            bits.append(f"{k}={extras[k]}")
    return " ".join(bits)


def _fmt(row: dict) -> str:
    ts = row.get("ts", "")
    try:
        hhmmss = ts[11:19]
    except Exception:
        hhmmss = ts[:8]
    slot = row.get("slot") or row.get("agent", "—")
    if isinstance(slot, str) and ":" in slot:
        slot = slot.split(":", 1)[1]
    kind = _row_kind(row)[:12]
    summary = _row_summary(row)[:80]
    task = row.get("task_id") or (row.get("extras", {}) or {}).get("task_id")
    suffix = f" [task={task}]" if task else ""
    return f"{hhmmss}  {slot:<10}  {kind:<12}  {summary}{suffix}"


def _iter_lines(path: Path, follow: bool) -> Iterator[str]:
    """Yield lines from `path`. In follow mode, handle file rotation by
    re-opening when the inode changes."""
    if not follow:
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")
        return
    # follow mode
    while not path.exists():
        time.sleep(0.5)
    f = open(path, encoding="utf-8")
    inode = os.fstat(f.fileno()).st_ino
    try:
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
                continue
            time.sleep(0.5)
            try:
                if os.stat(path).st_ino != inode:
                    f.close()
                    f = open(path, encoding="utf-8")
                    inode = os.fstat(f.fileno()).st_ino
            except FileNotFoundError:
                pass
    finally:
        f.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Tail a team's unified activity feed.")
    ap.add_argument("team_dir", help="Path to <repo>/teams/<name>")
    ap.add_argument("-f", "--follow", action="store_true", help="Follow new rows")
    ap.add_argument("--agent", help="Filter by agent slot (e.g. dev-1, manager)")
    ap.add_argument("--kind", help="Filter by kind/event")
    ap.add_argument("--since", help="ISO timestamp; show rows at or after")
    ap.add_argument("--limit", type=int, default=0, help="Show at most N rows (0=no cap)")
    args = ap.parse_args(argv)

    tdir = Path(args.team_dir).resolve()
    log = tdir / ".cto" / "activity.jsonl"
    since = _parse_iso(args.since) if args.since else None

    matched = 0
    for line in _iter_lines(log, args.follow):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.agent:
            slot = row.get("slot") or ""
            if isinstance(row.get("agent"), str) and ":" in row["agent"]:
                slot = slot or row["agent"].split(":", 1)[1]
            if slot != args.agent:
                continue
        if args.kind and _row_kind(row) != args.kind:
            continue
        if since:
            ts = _parse_iso(row.get("ts", ""))
            if ts is None or ts < since:
                continue
        print(_fmt(row))
        matched += 1
        if args.limit and matched >= args.limit and not args.follow:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
