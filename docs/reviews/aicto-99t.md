# Review aicto-99t: Regression tests + polish

**Reviewer:** aicto:review-2  
**Date:** 2026-05-04  
**Branch under review:** task/aicto-xgq  
**Epic base:** epic/aicto-v6t  

## Verdict: CHANGES REQUESTED

## Observations

### 1. [BLOCKER] No commits on task branch

`git log epic/aicto-v6t...HEAD` returns empty. Branch `task/aicto-xgq` is
identical to `epic/aicto-v6t` (both at `341c3d4`). The developer has not
committed any work.

**Required deliverables per plans/aicto-v6t.md §Dev 3:**
- `tests/test_top_render.py` (new, ~60 lines): render() contract tests,
  stale-row `~` prefix assertion.
- Minor polish to `dashboard/top.py` as needed (edge-case validation noted
  in plan: zero teams, all-stale, terminal resize).

**Concrete ask:** Commit at minimum `tests/test_top_render.py` with:
  - A test that calls `render()` with synthetic data and asserts return type
    is `rich.layout.Layout`.
  - A test that verifies stale rows render with `~` prefix when `stale=True`.
  - Any `dashboard/top.py` changes needed to support the above.
  - Confirm `uv run pytest tests/` passes before closing the dev issue.
