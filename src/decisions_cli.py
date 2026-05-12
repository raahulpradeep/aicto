#!/usr/bin/env python3
"""Browse a team's committed decision log.

Convention (per improvement.md §15): agents write
`docs/decisions/<epic-id>/decision-NNN.md` (zero-padded, scan dir for next id)
when making a non-obvious decision. Files are committed on the agent's branch
and merge with the epic — no separate audit DB.

Usage:
    cto decisions <team> [--epic <id>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


def _iter_entries(decisions_dir: Path, epic_filter: Optional[str]) -> list[Path]:
    if not decisions_dir.is_dir():
        return []
    out: list[Path] = []
    for epic_dir in sorted(decisions_dir.iterdir()):
        if not epic_dir.is_dir():
            continue
        if epic_filter and epic_dir.name != epic_filter:
            continue
        for entry in sorted(epic_dir.glob("decision-*.md")):
            out.append(entry)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Browse a team's decision log.")
    ap.add_argument("team_dir", help="Path to <repo>/teams/<name>")
    ap.add_argument("--epic", help="Only show entries under docs/decisions/<epic>/")
    args = ap.parse_args(argv)

    tdir = Path(args.team_dir).resolve()
    entries = _iter_entries(tdir / "docs" / "decisions", args.epic)
    if not entries:
        if args.epic:
            print(f"no decisions for epic {args.epic}", file=sys.stderr)
        else:
            print("no decisions logged yet", file=sys.stderr)
        return 0

    current_epic = None
    for entry in entries:
        epic = entry.parent.name
        if epic != current_epic:
            current_epic = epic
            print(f"\n===== {epic} =====")
        print(f"\n--- {entry.name} ({entry.stat().st_mtime:.0f}) ---")
        try:
            print(entry.read_text())
        except OSError as e:
            print(f"  [error reading: {e}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
