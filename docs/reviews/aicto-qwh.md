# Review aicto-qwh — Plan: Sync local main with remote main (aicto-6ud)

**Verdict: APPROVED** — plan is simple, safe, and correct for a pure git-sync operation.

---

## Observations

### 1. [suggestion] Dirty-tree mitigation is ambiguous — file:plans/aicto-6ud.md, Chunk A step 1

"stash or error and stop" conflates two very different actions. In a shared team workspace, auto-stashing can silently hide uncommitted changes under subsequent operations, making it hard to reason about what state the tree is in.

**Concrete ask:** Change to "error and stop only." If the tree is dirty, surface a clear error and let the developer (or manager) decide how to handle it. Never auto-stash.

### 2. [suggestion] No bd dolt sync step before git pull — file:plans/aicto-6ud.md, Chunk A steps

bd's Dolt store lives independently of git history, but the workspace CLAUDE.md session-close protocol always runs `bd dolt push` before `git push`. To be safe, the plan should call out a `bd dolt push` (push outstanding bd changes to remote) before executing the git pull, so no local bd commits are silently stranded if the pull fails mid-way.

**Concrete ask:** Add a step 0: "Run `bd dolt push` to ensure local bd state is synced to remote before touching the git tree."

### 3. [nit] No post-pull worktree health check — file:plans/aicto-6ud.md, Test Strategy

After rebasing `main`, the sub-worktrees (`epic/<id>`, `task/<id>`) still point to their own branches; they are unaffected by the pull. But it is worth logging `git worktree list` after the sync to confirm all worktrees are in a healthy state (no "prunable" entries).

**Concrete ask:** Add to Test Strategy: "Run `git worktree list` after pull and confirm no worktrees are listed as prunable."

---

## Summary

2 suggestions, 1 nit. No blockers. Core approach (pre-check → rebase pull → abort on conflict) is exactly right for this operation. The two suggestions narrow down ambiguity in the dirty-tree case and ensure bd state safety; neither is a correctness blocker for this simple sync.
