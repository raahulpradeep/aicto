# Plan: Eliminate UI jitter in top dashboard

**Epic:** aicto-v6t  
**File:** `dashboard/top.py`

## Goals

- Remove visible flicker and staggered appearance when the dashboard refreshes.
- Ensure every rendered frame is complete before it hits the terminal.
- Keep refresh latency at or below the current ~1s cycle.

## Non-goals

- Adding new panels or changing data sources.
- Altering the keyboard-quit path or Live session lifecycle.
- Changing the `bd` CLI or its query interface.

---

## Root-cause analysis

Reading the current `dashboard/top.py`:

### What's already correct
- `gather()` collects all team data via `_POOL` futures **before** calling `render()`.
- `render()` builds the entire `Layout` tree from a complete data snapshot.
- `live.update(layout)` followed by `live.refresh()` results in a single terminal write per cycle (because `Live.update()` defaults to `refresh=False`).

### Actual jitter sources

| # | Source | Location | Impact |
|---|--------|----------|--------|
| 1 | `Console().height` inside `render()` | `top.py:535` | Creates a fresh `Console` object every cycle. Rich may probe `COLUMNS`/`LINES` via a pty query, emitting control sequences mid-frame. |
| 2 | Dynamic panel height recalculation | `_panel_height()` + `render()` | Heights change whenever row counts change. Rich must emit a different number of lines, forcing a full-terminal reflow rather than an in-place update. |
| 3 | `live.update()` + `live.refresh()` separation | `main()` lines 614–624 | Two method calls leave a window where `live.renderable` is updated but not yet painted — any concurrent `auto_refresh` thread (even when `auto_refresh=False`, there are internal locking subtleties) could render a partial state. Collapsing to `live.update(..., refresh=True)` removes the gap. |
| 4 | No stale-data fallback | `gather()` / `_gather_team()` | A slow or erroring team returns `None`, producing blank rows. Should show prior cycle's data instead. |
| 5 | No per-team wall-clock timeout | `_gather_team()` | The 4 s subprocess timeout guards individual `bd` calls but `_gather_team` can still block for `3 × 4 s = 12 s` if all three queries time out. One slow team stalls the frame. |

---

## Implementation chunks

### Dev 1 — Batch fetch audit + stale-data fallback + team timeout

**Files touched:** `dashboard/top.py`

1. Audit the gather path and document (inline, ≤ 1 line comment) that the fetch-then-render separation is intentional.
2. Add a `_prev_data: dict[str, tuple]` module-level cache. In `gather()`, if `_gather_team` returns `None` **or** raises, look up the previous result for that team and mark it stale. Return stale rows tagged with a `stale=True` dict key.
3. Add an overall wall-clock timeout per team: wrap `fut.result()` in `gather()` with `timeout=10.0`; on `TimeoutError`, treat as stale.
4. In `_agent_panel`, `_inbox_panel`, `_open_panel`, `_closed_panel` — if a row is stale, dim the row or prefix the team name with `~` to signal cached data.

**Test:** manual run with a deliberately broken `bd` binary (rename it) — panels should show `~team` rows with prior data rather than going blank.

---

### Dev 2 — Atomic Live update + stable layout heights

**Files touched:** `dashboard/top.py`

#### 2a. Eliminate the rogue `Console()` probe

- Add `console: Console` parameter to `render()`.
- In `main()`, pass `live.console` (already exists inside the `Live` context).
- Remove the `Console().height` call inside `render()`; use `console.height` instead.

#### 2b. Stabilize panel heights

- Add `_MIN_PANEL_H = 8` and `_MAX_TOP_ROWS = 6` constants.
- Compute a **baseline height** at startup based on terminal size, clamped to the known minimums. Store in a module-level `_layout_heights: dict` that is only updated when the terminal actually resizes (compare `console.size` to the previous value).
- Keep `_panel_height()` but only re-run the height budget when the console size changes, not every render cycle.
- Result: layout heights stay constant across data-count fluctuations; only a true terminal resize triggers a recalculation.

#### 2c. Single atomic update call

- Replace the two-call sequence:
  ```python
  live.update(layout)
  live.refresh()
  ```
  with:
  ```python
  live.update(layout, refresh=True)
  ```
- This collapses set-renderable and paint into one locked operation inside Rich.

**Test:** `python dashboard/top.py` in a tmux pane for 60 s while running `cto start aicto`. No visible row-jump or flicker. Confirm `gather_ms` is not degraded vs. baseline.

---

### Dev 3 — Regression & polish

**Files touched:** `dashboard/top.py` (minor), `tests/test_top_render.py` (new)

1. **Regression test** (`tests/test_top_render.py`):
   - Import `render` and call it with synthetic agent/inbox/open/closed lists.
   - Assert the returned object is a `rich.layout.Layout`.
   - Assert stale rows appear with `~` prefix when `stale=True` is set.
   - Run with `uv run pytest tests/` (or `python -m pytest` if uv not configured for tests).

2. **Terminal compatibility check**:
   - Run in a standard macOS Terminal.app, in iTerm2, and in a tmux session.
   - Confirm no ghost lines or scrollback contamination.

3. **Edge-case validation**:
   - Zero teams running → empty-state panels render without crash.
   - All teams stale → all panels show stale rows, footer shows stale count.
   - Terminal resize mid-run → layout recomputes cleanly on next cycle.

4. **Refresh-rate sanity**:
   - Log `gather_ms` over 10 cycles. P99 should remain ≤ 2000 ms (same as before).
   - If stale-data path is hit, `gather_ms` should be near 0 for that team (no blocking).

---

## File inventory

| File | Change |
|------|--------|
| `dashboard/top.py` | ~80 lines changed, ~20 added |
| `tests/test_top_render.py` | New, ~60 lines |

---

## Test strategy

- Unit: `pytest tests/test_top_render.py` — render() contract, stale-row labelling.
- Manual smoke: run dashboard for 60 s, observe zero flicker in tmux + standard terminal.
- Latency: `gather_ms` displayed in footer; eyeball over 10 cycles.

---

## Risks & rollback

| Risk | Mitigation |
|------|-----------|
| `live.console` attribute changes across Rich versions | Pin Rich ≥ 13.0; `live.console` has been stable since Rich 10. |
| Stale-data cache grows unbounded if teams are removed | Evict entries whose team dir no longer exists at start of each `gather()` cycle. |
| Height-stability breaks on narrow terminals | Keep existing `deficit` shrink-from-bottom logic; only skip recalc when size is unchanged. |
| `live.update(refresh=True)` semantics differ on older Rich | Already `auto_refresh=False`; `refresh=True` on update is well-defined since Rich 12. |

Rollback is a one-commit revert on the task branch before any `kind:merge` issue is filed.
