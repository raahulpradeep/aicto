# Review aicto-gqc — Dev 2: Atomic Live update + stable layout heights

Reviewer: aicto:review-2  
Verdict: **APPROVED** (with suggestions)

## Observations

### 1. `_MAX_TOP_ROWS = 6` — unused constant [suggestion]
**File**: `dashboard/top.py:53`  
Introduced in this commit but referenced nowhere. Delete it — dead code in a diff is confusing and suggests either a half-finished idea or a forgotten cleanup.

### 2. `_panel_height()` — now orphaned [suggestion]
**File**: `dashboard/top.py:507`  
`_compute_layout_heights` took over its role; `_panel_height` is no longer called anywhere. Remove the function. A future reader will wonder why it exists and look for callers that aren't there.

### 3. Global mutable state in `render()` [nit]
**File**: `dashboard/top.py:521–527`  
`_layout_heights` and `_prev_console_size` are module-level globals mutated inside `render()`. For a single-process CLI tool this is fine, but the `global` declaration makes the side-effect explicit, which is good. No change required.

### 4. Core fixes are correct [positive]
- Passing `console=live.console` instead of creating a bare `Console()` probe each cycle is the right fix — the live instance reflects the actual terminal state.
- `live.update(..., refresh=True)` replacing separate `live.refresh()` is the correct atomic Rich API pattern; eliminates the partial-render window.
- Caching heights by terminal size and only recomputing on resize cleanly solves the row-jump flicker without over-engineering.

## Summary

Two dead-code items (suggestions, not blockers) should be cleaned up; the core logic is sound and the stated goals are met.
