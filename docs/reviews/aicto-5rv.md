# Review: aicto-5rv — Batch fetch audit + stale-data fallback

**Target:** `task/aicto-7uh` vs `epic/aicto-v6t`  
**Verdict:** APPROVED with notes for manager

---

## Observations

### 1. One-hunk merge conflict — manager must resolve (suggestion)

**File:** `dashboard/top.py:53-62`  
Dev 1 branched off `epic/aicto-v6t` at the plan-merge commit (`341c3d4`), before Dev 2 was merged in. Both Dev 1 and Dev 2 added module-level globals immediately after `_POOL`. The conflict is trivial: keep both blocks.

```
<<<<<<< HEAD (Dev 1)
_prev_data: dict[str, tuple] = {}
=======
_MIN_PANEL_H = 8
_MAX_TOP_ROWS = 6
_layout_heights: dict[str, int] = {}
_prev_console_size: tuple[int, int] = (0, 0)
>>>>>>> epic/aicto-v6t
```

Resolution: retain all five declarations. No logic overlap. Manager must resolve before or during `git merge --no-ff`.

---

### 2. xfail markers in Dev 3 tests not yet removed (suggestion)

**File:** `tests/test_top_render.py` (lives on `epic/aicto-v6t`, not on this branch)  
Dev 3 added 3 `@pytest.mark.xfail` tests for stale-row behaviour, marked pending Dev 1. The plan says "Remove xfail once [Dev 1] merged." Those markers should be removed after this branch lands in the epic — they will now XPASS and produce misleading test output. File a follow-up task to un-xfail them.

---

### 3. Redundant exception tuple (nit)

**File:** `dashboard/top.py:329`
```python
except (concurrent.futures.TimeoutError, Exception):
```
`Exception` already covers `TimeoutError`. Simplify to `except Exception:`. No behaviour change.

---

### 4. Double-import (nit)

**File:** `dashboard/top.py:34`  
`ThreadPoolExecutor` is already imported via `from concurrent.futures import ThreadPoolExecutor`. The new `import concurrent.futures` was added only to get `concurrent.futures.TimeoutError`. Cleaner to extend the existing from-import:
```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
```
Then reference `FuturesTimeoutError` (or just `Exception` per observation 3). No blocker.

---

## Summary

Core logic is correct and fully implements the plan spec (§Dev 1):
- `_prev_data` module-level cache with disk-eviction
- `fut.result(timeout=10.0)` per-team wall-clock guard
- Stale tagging via `{**r, "stale": True}` (non-mutating — cached copy stays clean)
- `~`-prefix + `dim` style consistently applied in all four panel functions
- `running.append(team_name)` correctly includes stale teams

No blockers. Manager should resolve the one-hunk conflict and file a follow-up for xfail cleanup.
