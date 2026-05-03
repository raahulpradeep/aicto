# Review: aicto-9jl — Plan: Support configurable parent branch per epic

**Reviewer:** aicto:review-2  
**Plan:** plans/aicto-7vi.md @ task/aicto-u1e  
**Epic:** aicto-7vi

---

## Observations

### 1. BLOCKER — `epic_parent_branch()` runs `bd show` from wrong directory

**File:** plans/aicto-7vi.md — Chunk 2, helper function  
**Severity:** Blocker

The proposed helper runs:
```bash
desc=$(cd "$tdir" && bd show "$epic_id" --json 2>/dev/null ...)
```

`$tdir` is the epic worktree path (e.g. `.cto/worktrees/aicto-7vi/`). The team CLAUDE.md is explicit: "bd lives in the **main worktree only**. All bd commands run from `/…/teams/aicto`." Running `bd` from a sub-worktree will fail to find the `.beads` database.

**Fix:** Pass the team's main worktree root as a third argument (or resolve it via `git -C "$tdir" rev-parse --show-toplevel` if that reliably returns the main tree), and `cd` into that before calling `bd`:
```bash
epic_parent_branch() {
  local team_root="$1" epic_id="$2"
  local desc
  desc=$(cd "$team_root" && bd show "$epic_id" --json 2>/dev/null | ...)
  ...
}
```
The call site in `cmd_merge_epic` must supply `team_root` (the repo root, not `tdir`).

---

### 2. Suggestion — `show-ref` validates local refs only

**File:** plans/aicto-7vi.md — Chunk 2, item 6  
**Severity:** Suggestion

`git show-ref --verify --quiet refs/heads/$merge_target` will die if the target branch hasn't been fetched locally, even if it exists on the remote. Consider checking remote refs too:
```bash
git -C "$tdir" show-ref --verify --quiet "refs/heads/$merge_target" \
  || git -C "$tdir" show-ref --verify --quiet "refs/remotes/origin/$merge_target" \
  || die "merge-epic: target branch '$merge_target' not found"
```
Or simply `git fetch origin "$merge_target" 2>/dev/null; git show-ref ...`.

---

### 3. Suggestion — Storage: use bd structured field instead of free-text embedding

**File:** plans/aicto-7vi.md — Chunk 1, "Storage format"  
**Severity:** Suggestion

The plan embeds `parent_branch:` as grep-able free text in the description body. The bd `create` tool exposes a `--design` / `design` field that is structured and separate from the description. Storing `parent_branch: <value>` in the `design` or `notes` field would be more robust and less prone to breakage if a CTO edits the description. If the grep-based extraction is consolidated into one `epic_parent_branch()` helper, the risk is contained — acceptable if you keep it that way.

---

### 4. Nit — Manager template: `|| echo main` fires on command failure, not empty output

**File:** plans/aicto-7vi.md — Chunk 3, templates/manager.md snippet  
**Severity:** Nit

```bash
PARENT=$(bd show <epic-id> ... | grep parent_branch | awk '{print $2}' || echo main)
```
`|| echo main` only fires if the pipeline returns non-zero exit code; `grep` returning empty string exits 0. The shell will set `PARENT=""`. The correct idiom is:
```bash
PARENT=$(bd show <epic-id> ... | grep parent_branch | awk '{print $2}')
PARENT=${PARENT:-main}
```

---

## Summary

1 blocker, 2 suggestions, 1 nit. The plan is structurally sound and covers the required scope well — backward compatibility, branch existence validation, template updates, and a clear dev breakdown are all present. Address the `bd show` worktree-context blocker and optionally the remote-ref check, then this plan is ready to proceed.
