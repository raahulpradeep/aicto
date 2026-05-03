# Review: aicto-73h — CLI flag + storage layer for parent-branch

**Target:** task/aicto-4z9  
**Epic:** aicto-7vi  
**Reviewer:** aicto:review-1  
**Date:** 2026-05-04  
**Verdict:** NO ARTIFACT

## Finding

Branch `task/aicto-4z9` does not exist. Dev issue `aicto-4z9` (Dev 1: CLI flag + storage layer for parent-branch) is still **open** with no assignee — the developer has not started this work.

The review issue `aicto-73h` was filed prematurely before any implementation exists.

## Context

Chunk 2 (`aicto-77j`: merge-time resolution) has been implemented and is pending its own review (`aicto-8xk`). That commit correctly adds `epic_parent_branch()` and updates `cmd_merge_epic` to read the `parent_branch` field from the epic description. However it relies on Chunk 1 having written that field — which is not yet done.

Chunk 1 must be implemented before the end-to-end feature is functional:
- `cmd_task` in `bin/cto` needs the `--parent-branch <branch>` option parser flag
- The value must be embedded in the bd description under a parseable `parent_branch: <value>` line
- `mcp/server.py` `task` tool needs the `parent_branch: Optional[str]` parameter

## Action

Review closed — no artifact to assess. Dev issue `aicto-4z9` remains open for a developer to claim.
