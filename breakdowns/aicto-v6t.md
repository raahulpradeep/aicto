# Breakdown: Eliminate UI jitter in top dashboard

**Epic:** aicto-v6t — Eliminate UI jitter in top dashboard — smooth refresh across all panels

## CTO intent

The `top` dashboard (`dashboard/top.py`) has a jarring refresh: rows flicker, teams appear staggered, and panels visibly rebuild on each cycle. The display should update atomically — either the full new frame or the previous frame, never a half-populated state.

## Plan tasks

### Plan 1: Atomic frame rendering

Investigate and fix the refresh path in `dashboard/top.py` so that:

1. All per-team data is fetched (in parallel) **before** any render update.
2. The previous Rich `Live` frame stays on screen while the next frame is assembled.
3. A single `live.update()` call commits the complete new frame atomically.

## Dev tasks (rough chunks, refined after plan approval)

### Dev 1: Batch data fetch before render

- Audit the current fetch-render loop in `dashboard/top.py`.
- Refactor so all team data is collected into a complete snapshot before touching the `Live` display.
- Verify no intermediate `live.update()` calls happen during fetching.

### Dev 2: Atomic `Live` update

- Ensure the Rich `Layout` / panel tree is fully built from the snapshot before calling `live.update()`.
- If the current code mutates shared layout objects while fetching, switch to building a new layout each cycle and swapping it in one call.
- Test with 3+ teams to confirm no staggered appearance.

### Dev 3: Regression & polish

- Confirm refresh rate is not degraded.
- Confirm terminal compatibility (standard terminals, tmux, etc.).
- Edge cases: team data fetch failure mid-cycle should show stale data, not blank rows.

## Reviewers

- Reviewer 1 (aicto reviewer pool): review the plan, then review each dev branch.

## Risks

- Rich `Live` internals may not support truly atomic swaps if layout objects are mutated in place — may need to construct a fresh layout per cycle.
- Parallel fetching could mask slow teams; need a timeout so one slow team doesn't hold the whole frame.
