# Breakdown: Support configurable parent branch per epic

**Epic:** aicto-7vi
**CTO Intent:** Allow an optional `--parent-branch` flag when filing epics so the final merge targets that branch instead of `main`.

## Plan tasks

### Plan 1: CLI + storage layer
- Add `--parent-branch <branch>` flag to `bin/cto task` (epic creation path).
- Store the value in bd metadata (via `--set-metadata parent_branch=<branch>` or bd notes/design field).
- Add `--parent-branch` support to the `mcp__cto__task` MCP tool so it mirrors the CLI.

### Plan 2: Merge-time resolution
- Modify `cto merge-epic` (and `mcp__cto__merge_epic`) to read the `parent_branch` metadata from the epic issue.
- If present, merge `epic/<id>` into `<parent_branch>` instead of `main`.
- If absent, keep existing behaviour (merge into `main`).
- Ensure the epic worktree cleanup still works correctly with a non-main target.

### Plan 3: Visibility + prompt awareness
- Update `cto inbox` and `cto status` to surface the parent branch when set (e.g. "target: feature-x").
- Update manager and developer prompt templates so worktrees and branch bases reference the parent branch when available.

## Proposed dev tasks (per plan area)

- **Dev 1 (CLI + storage):** Implement the flag parsing, metadata storage, and MCP tool update. ~1 dev task.
- **Dev 2 (Merge logic):** Implement merge-target resolution in `cto merge-epic` / `mcp__cto__merge_epic`. ~1 dev task.
- **Dev 3 (Visibility):** Update `cto inbox`, `cto status`, and agent prompt templates. ~1 dev task.

## Reviewers and risks

- **Reviewer:** team reviewer (1 reviewer configured).
- **Risk 1:** Merge into a non-existent branch — need validation that the target branch exists at merge time.
- **Risk 2:** Manager/developer prompts currently hard-code `main` in several places — must audit all references.
- **Risk 3:** If the parent branch itself is another epic branch, nested epic merges could get complex. Recommend keeping scope to one level for now.
