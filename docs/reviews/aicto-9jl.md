# Review: aicto-9jl — Plan: Support configurable parent branch per epic

**Reviewer:** aicto:review-2  
**Plan:** plans/aicto-7vi.md @ task/aicto-u1e  
**Epic:** aicto-7vi  
**Round:** 2 (re-review after blocker fix)

---

## Round 1 issues — resolution status

### 1. BLOCKER — `epic_parent_branch()` runs `bd show` from wrong directory
**Status: RESOLVED**  
Function now takes `team_root` as first arg and `cd "$team_root"` before calling `bd show`. Call-site note in plan explicitly says to supply `team_root` (not `tdir`). Correct.

### 2. Suggestion — `show-ref` validates local refs only
**Status: RESOLVED**  
Chunk 2 item 6 now checks both `refs/heads/$merge_target` and `refs/remotes/origin/$merge_target` before dying. Matches suggestion exactly.

### 3. Suggestion — Storage: use bd structured field
**Status: ACCEPTABLY DEFERRED**  
Still uses grep-able free-text. Acceptable because extraction is consolidated into `epic_parent_branch()` helper — breakage is contained to one place.

### 4. Nit — Manager template `|| echo main` fires on command failure, not empty output
**Status: RESOLVED**  
Template snippet now uses `PARENT=${PARENT:-main}` idiom.

---

## Round 2 observations

No new blockers. Plan is structurally sound:
- Goals/non-goals are clear; nested epics explicitly out of scope.
- Backward compatible: `epic_parent_branch()` defaults to `"main"`.
- Branch existence check covers both local and remote refs.
- Dev breakdown (3 chunks, ~95 lines total) is appropriately scoped.
- Manual test strategy covers all significant paths.
- Risks table is thorough.

**Verdict: APPROVED — no further changes required.**
