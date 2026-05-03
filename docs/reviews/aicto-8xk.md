# Review aicto-8xk — Merge-time resolution for parent-branch (task/aicto-77j)

Reviewer: aicto:review-1  
Verdict: **APPROVED** — no blockers

---

## Observations

### 1. Implementation completeness — PASS

All 7 points from Plan §Chunk 2 are implemented correctly:
- `epic_parent_branch()` helper added before `cmd_merge_epic`.
- `merge_target` variable derived via helper, replacing every hard-coded `"main"`.
- Branch-existence validation (local + remote) fires before the worktree-head check.
- Merge commit message updated to `"merge $epic_branch into $merge_target"`.
- Epic close-reason updated to `"epic merged into $merge_target by CTO"`.
- Worktree-prune ancestor check uses `epic_parent_branch` correctly.
- `mcp/server.py` docstring updated.

### 2. Nit — unvalidated first character in extracted branch name

**Severity:** nit  
**File:** `bin/cto`, `epic_parent_branch()`

The regex `[A-Za-z0-9._/-]+` allows branch names beginning with `-`. If a description were crafted with `parent_branch: --some-option`, `git merge-base` or `git show-ref` could interpret it as a flag. In practice the CTO authors epic descriptions and this is extremely low risk, but a `[A-Za-z0-9]` anchor on the first character would close the gap.

### 3. Nit — `bd show` subprocess per epic during worktree prune

**Severity:** nit  
**File:** `bin/cto`, `cmd_worktrees` prune block

Each epic branch in the worktree list spawns a `bd show + jq` subprocess to resolve its parent target. Acceptable at typical team scale (< 20 epics), but worth a note if prune ever gets called in a hot loop.

### 4. Worktree contains stale review artifact — cosmetic

**Severity:** nit (not dev's fault)

Commit `4a74810` (tip of `task/aicto-77j`) adds `docs/reviews/aicto-73h.md` — residue from this worktree being reused for an earlier review pass. No code affected; the merge will carry this file into the epic branch, which is harmless. The supervisor should prune/recycle worktrees between review tasks to keep branches clean.

---

## Summary

Correct, minimal, and faithful to the plan. The two nits are genuine but not blockers.
