# Epic: Sync main with latest code and push to remote (aicto-0g8)

## CTO Intent

Update the team's local `main` branch with the latest committed code across all worktrees/epics that are ready, then push everything to the GitHub remote so the remote reflects the current state. End with `git status` showing "up to date with origin/main". No force-push; resolve divergence cleanly. Honor existing gitignore.

## Plan Tasks

### Plan 1: Audit and sync local state

Inventory all worktrees and branches. Identify which epic branches have been fully merged into main vs. which are still in flight. Check for any uncommitted or unstaged changes. Determine if origin/main is ahead, behind, or diverged from local main.

### Plan 2: Push main to remote

After confirming main is clean and contains all merged epic work, push to origin. If diverged, rebase local main onto origin/main first. Verify final state.

## Dev Chunks

- **Chunk 1 (audit):** Run `git worktree list`, `git branch -v`, `git status`, `git log --oneline main..origin/main` and `origin/main..main`. Produce a summary of what's merged, what's in flight, and whether there's divergence. Clean up any stale worktrees for already-merged epics.
- **Chunk 2 (push):** If main is clean, run `git push origin main`. If diverged, `git pull --rebase origin main` first. Verify with `git status` showing up to date.

## Reviewers

- Reviewer 1 (plan review + code review)

## Risks

- origin/main may have diverged if someone pushed externally; rebase needed.
- Stale worktrees referencing deleted branches could cause confusion; prune first.
- If any epic worktree has uncommitted changes, those would be lost if pruned carelessly.
