#!/usr/bin/env python3
"""Auto-merger daemon — drains ready `kind:merge role:manager` issues in parallel.

Runs as its own tmux window. Replaces the manager's serial-merge bottleneck:
sub-branch merges are pure git operations (no LLM judgment needed for the
happy path), so a script can drain them at near-disk-IO speed instead of one
per ~60s manager iteration.

Scope:
  - Handles `kind:merge` + `role:manager` + target in {breakdown, plan, code}.
  - SKIPS `target:epic` (CTO-only) and anything missing the expected description fields.
  - On any conflict / unexpected state: aborts the merge, reopens the upstream
    issue, closes the merge with a note, and lets the manager handle it.

Concurrency:
  - One merge per epic at a time (epic worktree is shared state).
  - Different epics processed in parallel via a thread pool.
  - Same atomic mkdir-claim as the manager: `.cto/locks/<merge-id>/`.

Usage: merger.py <team-dir>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Optional

POLL_SECS = 1.0
ALLOWED_TARGETS = ("target:breakdown", "target:plan", "target:code")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] merger: {msg}", flush=True)


def run(cmd: list[str], cwd: Path, timeout: float = 30.0, check: bool = False) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def bd_json(args: list[str], cwd: Path) -> list:
    rc, out, _ = run(["bd", *args, "--json"], cwd, timeout=10.0)
    if rc != 0:
        return []
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def parse_desc(desc: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (desc or "").splitlines():
        line = line.strip()
        m = re.match(r"^(epic|branch|idem)\s*:\s*(\S+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def list_ready_merges(team_dir: Path) -> list[dict]:
    """Return ready manager merges with allowed targets, oldest first."""
    rows = bd_json(["ready", "--label", "role:manager", "--json"], team_dir)
    out = []
    for r in rows:
        labels = r.get("labels") or []
        if "kind:merge" not in labels:
            continue
        if not any(t in labels for t in ALLOWED_TARGETS):
            continue
        if "target:epic" in labels:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("priority", 2))
    return out


def claim_merge(team_dir: Path, merge_id: str) -> bool:
    """Atomic mkdir-based claim. Same protocol as the manager."""
    locks = team_dir / ".cto" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_dir = locks / merge_id
    try:
        lock_dir.mkdir()
    except FileExistsError:
        return False
    (lock_dir / "owner").write_text("merger\n", encoding="utf-8")
    rc, _, err = run(
        ["bd", "update", merge_id, "--status", "in_progress", "--assignee", "merger"],
        team_dir, timeout=10.0,
    )
    if rc != 0:
        # Couldn't flip — release.
        try:
            (lock_dir / "owner").unlink()
            lock_dir.rmdir()
        except OSError:
            pass
        log(f"claim flip failed for {merge_id}: {err.strip()[:120]}")
        return False
    return True


def release_claim(team_dir: Path, merge_id: str) -> None:
    locks_dir = team_dir / ".cto" / "locks" / merge_id
    if locks_dir.is_dir():
        for child in locks_dir.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            locks_dir.rmdir()
        except OSError:
            pass


def reset_to_open(team_dir: Path, merge_id: str) -> None:
    run(["bd", "update", merge_id, "--status", "open", "--assignee", ""], team_dir, timeout=10.0)
    release_claim(team_dir, merge_id)


def reopen_upstream(team_dir: Path, branch: str, conflict_msg: str) -> None:
    """Best-effort reopen of the issue whose id matches the trailing branch segment."""
    upstream_id = branch.rsplit("/", 1)[-1]
    if not upstream_id:
        return
    run(["bd", "reopen", upstream_id], team_dir, timeout=10.0)
    run(["bd", "comment", upstream_id, f"merger: conflict on `{branch}` — {conflict_msg}"],
        team_dir, timeout=10.0)


def close_merge(team_dir: Path, merge_id: str, reason: str) -> None:
    run(["bd", "close", merge_id, "-r", reason], team_dir, timeout=10.0)
    release_claim(team_dir, merge_id)


def epic_worktree(team_dir: Path, epic_id: str) -> Path:
    return team_dir / ".cto" / "worktrees" / epic_id


def sub_worktree(team_dir: Path, sub_id: str) -> Path:
    return team_dir / ".cto" / "worktrees" / sub_id


def do_merge(team_dir: Path, merge: dict) -> str:
    """Execute one merge. Returns a short status string for logging."""
    merge_id = merge["id"]
    desc = merge.get("description", "")
    fields = parse_desc(desc)
    epic_id = fields.get("epic", "")
    branch = fields.get("branch", "")

    if not epic_id or not branch:
        # Don't auto-handle; reset and let the manager pick it up.
        reset_to_open(team_dir, merge_id)
        return f"{merge_id}: missing epic/branch in description"

    ewt = epic_worktree(team_dir, epic_id)
    if not ewt.is_dir():
        reset_to_open(team_dir, merge_id)
        return f"{merge_id}: epic worktree {ewt} missing"

    # Sanity-check the branch exists.
    rc, _, _ = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], ewt, timeout=10.0)
    if rc != 0:
        reset_to_open(team_dir, merge_id)
        return f"{merge_id}: branch {branch} not found"

    # Attempt the merge.
    rc, _, err = run(
        ["git", "-c", "commit.gpgsign=false", "merge", "--no-ff", branch,
         "-m", f"merge {branch}"],
        ewt, timeout=60.0,
    )
    if rc != 0:
        run(["git", "merge", "--abort"], ewt, timeout=10.0)
        msg = (err or "").strip().splitlines()[-1][:160] if err else "merge failed"
        reopen_upstream(team_dir, branch, msg)
        close_merge(team_dir, merge_id, f"conflict on {branch}; reopened upstream")
        return f"{merge_id}: conflict — reopened {branch.rsplit('/',1)[-1]}"

    # Prune the sub-worktree + sub-branch on success.
    sub_id = branch.rsplit("/", 1)[-1]
    swt = sub_worktree(team_dir, sub_id)
    if swt.is_dir():
        run(["git", "worktree", "remove", "--force", str(swt)], team_dir, timeout=20.0)
    run(["git", "branch", "-D", branch], team_dir, timeout=10.0)

    close_merge(team_dir, merge_id, f"merged into epic/{epic_id}")
    return f"{merge_id}: merged {branch} → epic/{epic_id}"


# ----------------------------------------------------------------------
# Concurrency: one in-flight merge per epic; different epics in parallel.
# ----------------------------------------------------------------------


_in_flight_epics: set[str] = set()
_in_flight_lock = threading.Lock()


def try_acquire_epic(epic_id: str) -> bool:
    with _in_flight_lock:
        if epic_id in _in_flight_epics:
            return False
        _in_flight_epics.add(epic_id)
        return True


def release_epic(epic_id: str) -> None:
    with _in_flight_lock:
        _in_flight_epics.discard(epic_id)


def process_one(team_dir: Path, merge: dict) -> str:
    epic_id = parse_desc(merge.get("description", "")).get("epic", "")
    if not epic_id:
        # Without an epic, processing serially is fine.
        epic_id = f"__no_epic__/{merge['id']}"
    if not try_acquire_epic(epic_id):
        return f"{merge['id']}: epic {epic_id} busy"
    if not claim_merge(team_dir, merge["id"]):
        release_epic(epic_id)
        return f"{merge['id']}: claim lost"
    try:
        return do_merge(team_dir, merge)
    finally:
        release_epic(epic_id)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: merger.py <team-dir>", file=sys.stderr)
        return 2
    team_dir = Path(sys.argv[1]).resolve()
    if not (team_dir / ".cto").is_dir():
        log(f"no .cto dir at {team_dir}; exiting")
        return 1

    log(f"merger up — watching {team_dir}")
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="merger-w")
    wake_helper = team_dir / ".cto" / "wake.py"

    try:
        while True:
            ready = list_ready_merges(team_dir)
            if ready:
                futs: list[Future] = []
                for m in ready:
                    futs.append(pool.submit(process_one, team_dir, m))
                for f in futs:
                    try:
                        log(f.result(timeout=120))
                    except Exception as exc:
                        log(f"worker exception: {exc}")
            # Sleep with bus-aware wake.
            if wake_helper.is_file() and os.access(wake_helper, os.X_OK):
                run(["python3", str(wake_helper), str(team_dir), "1"], team_dir, timeout=3.0)
            else:
                time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        log("merger shutting down")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
