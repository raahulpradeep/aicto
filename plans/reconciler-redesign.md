# aicto-reconciler-redesign.md

## Problem: The Reconciler is Brittle

The current reconciler is ~1200 lines of ad-hoc conditional logic that tries to track the entire workflow state for all epics. When it makes a wrong decision or misses an edge case, the workflow stalls or creates duplicate issues.

### Current Pain Points

| Pain | Why It Happens |
|------|---------------|
| **Single point of failure** | One `reconcile(state)` function decides everything for all epics |
| **Implicit state machine** | Workflow phases (breakdown→plan→dev→review→merge) are buried in if-statements |
| **Idempotency hacks** | `idem:` strings in descriptions prevent duplicates; fragile and error-prone |
| **Race conditions** | Reads all state → computes actions → executes; state changes during execution |
| **Silent failures** | bd CLI errors are logged but not surfaced; workflow just stalls |
| **No rollback** | Partial failures leave state inconsistent (upstream filed, downstream failed) |
| **Hard to test** | 800+ lines of epic-specific logic; impossible to unit test all edge cases |
| **Tight coupling** | Reconciler directly calls `bd` to file issues; can't swap backends |

---

## Proposed Architecture: Distributed State Machine + Agent-Owned Transitions

### Core Idea

Instead of one reconciler computing all transitions:

1. **Define the workflow as an explicit graph** (states + transitions with guards)
2. **Let agents file their own next steps** when they close an issue
3. **Reconciler shrinks to a health auditor** — detects stuck state and auto-heals
4. **Add preview mode** — show what would happen before doing it

### Workflow Graph (Explicit)

```
epic:created
    ↓ [manager writes breakdown]
breakdown:open → breakdown:in_progress → breakdown:closed
    ↓ [reconciler or agent files next]
approval:breakdown OR merge:breakdown
    ↓ [CTO approves OR manager merges]
plan:open → plan:in_progress → plan:closed
    ↓ [developer files next when closing]
dev:open → dev:in_progress → dev:closed
    ↓ [reviewer files next when closing]
review:open → review:in_progress → review:closed
    ↓ [approved → merge, changes-requested → re-review]
merge:code OR re-review
    ↓ [all devs merged]
merge:epic (ship)
```

### What Changes

| Before | After |
|--------|-------|
| Reconciler files ALL workflow issues | Agents file their own next step when closing |
| Reconciler tracks 8 phases per epic | Each epic is a state machine; transitions are explicit |
| Reconciler uses `idem:` hacks | State machine uses explicit state; no idempotency needed |
| Reconciler directly calls `bd` | Actions go through a queue; executed with retries and rollback |
| Reconciler is 1200 lines | Reconciler is ~200 lines (health audit only) |
| Hard to test | State machine is pure; trivial to unit test |

### Implementation Plan

#### Phase 1: Explicit State Machine

```python
# src/workflow.py
from enum import Enum, auto

class EpicState(Enum):
    CREATED = auto()
    BREAKDOWN_OPEN = auto()
    BREAKDOWN_DONE = auto()
    PLAN_OPEN = auto()
    PLAN_DONE = auto()
    DEV_IN_PROGRESS = auto()
    DEV_DONE = auto()
    REVIEW_IN_PROGRESS = auto()
    CHANGES_REQUESTED = auto()
    MERGE_READY = auto()
    SHIP_READY = auto()
    SHIPPED = auto()

TRANSITIONS = {
    EpicState.CREATED: [EpicState.BREAKDOWN_OPEN],
    EpicState.BREAKDOWN_DONE: [EpicState.PLAN_OPEN, EpicState.BREAKDOWN_REVIEW],
    EpicState.PLAN_DONE: [EpicState.DEV_IN_PROGRESS],
    EpicState.DEV_DONE: [EpicState.REVIEW_IN_PROGRESS],
    EpicState.CHANGES_REQUESTED: [EpicState.DEV_IN_PROGRESS],  # loop back
    EpicState.REVIEW_DONE: [EpicState.MERGE_READY],
    EpicState.MERGE_READY: [EpicState.SHIP_READY],
    EpicState.SHIP_READY: [EpicState.SHIPPED],
}

def compute_state(epic: Issue, children: list[Issue]) -> EpicState:
    """Pure function: derive state from issues. Testable."""
    # ... explicit logic, no conditionals scattered across 800 lines
```

#### Phase 2: Agent-Owned Transitions

When an agent closes an issue, it files the next step:

```python
# In developer agent prompt:
"""
When you close a dev issue, you MUST file the next review issue:

bd file "Review: {title}" \
  --label role:reviewer,kind:review,target:code \
  --description "upstream: {dev_id}\nepic: {epic_id}"
"""

# In reviewer agent prompt:
"""
When you approve a review, you MUST file the merge issue:

bd file "Merge: {title}" \
  --label role:manager,kind:merge,target:code \
  --description "upstream: {dev_id}\nbranch: task/{dev_id}"
"""
```

#### Phase 3: Health Auditor (New Reconciler)

```python
# src/health_auditor.py

class HealthAuditor:
    """Detects workflow violations and auto-heals."""

    def audit(self, state: State) -> list[HealAction]:
        violations = []

        # V1: Epic has no breakdown after 15min
        for epic in state.epics():
            if epic.state == EpicState.CREATED and epic.age > 900:
                violations.append(EscalateToCTO(epic, "No breakdown filed"))

        # V2: Dev closed but no review filed
        for dev in state.devs():
            if dev.closed and not dev.has_review_filed:
                violations.append(FileReview(dev))

        # V3: Review approved but no merge filed
        for review in state.reviews():
            if review.approved and not review.has_merge_filed:
                violations.append(FileMerge(review))

        # V4: Zombie in_progress (>15min no update)
        for issue in state.in_progress():
            if issue.age > 900:
                violations.append(ResetClaim(issue))

        return violations
```

#### Phase 4: Transactional Action Queue

```python
# src/action_queue.py

class ActionQueue:
    """All workflow changes go through here."""

    def submit(self, actions: list[Action]) -> Transaction:
        """Preview → confirm → execute with rollback."""
        tx = Transaction(actions)
        tx.preview()  # Show what would happen (for dashboard)
        tx.execute()  # Execute with retries
        # On failure: tx.rollback() undoes filed issues
        return tx
```

### Dashboard Integration

```
CTO Dashboard:
├── Epic Pipeline (visual state machine)
│   └── Each epic shows its current state + next transition
├── Violations Panel (what health auditor found)
│   └── "Dev #3 closed but no review filed → click to fix"
└── Action Preview (before executing)
    └── "This will file 2 issues + 1 dependency → Confirm?"
```

### Benefits

| Benefit | How |
|---------|-----|
| **No more brittleness** | State machine is explicit; easy to verify |
| **No idempotency hacks** | State is derived from actual issue state, not description strings |
| **Agents are autonomous** | They file their own next steps; reconciler doesn't micromanage |
| **Easy to test** | State transitions are pure functions with clear inputs/outputs |
| **Composable** | Can add new workflow phases by extending the graph |
| **Observable** | Dashboard shows exact state of every epic |
| **Recoverable** | Health auditor detects and fixes violations automatically |
| **Preview mode** | CTO can see what would happen before confirming |

### Migration Path

1. **Week 1**: Build explicit state machine alongside existing reconciler
2. **Week 2**: Update agent prompts to file next steps
3. **Week 3**: Reconciler shrinks to health auditor
4. **Week 4**: Add preview mode to dashboard
5. **Week 5**: Remove old reconciler; fully distributed

### Open Questions

1. Do agents have enough context to file correct next steps?
2. How do we handle race conditions when multiple agents try to file the same next step?
3. Should we use `bd` dependencies (`--blocks`) to prevent premature claiming?
4. How do we surface violations to the CTO dashboard?

---

## Alternative: Keep Centralized but Make It Robust

If distributed feels too risky, keep the reconciler but fix the brittleness:

1. **Pure state machine** — explicit states/transitions instead of ad-hoc conditionals
2. **Preview mode** — show computed actions before executing (CTO approval gate)
3. **Transaction wrapper** — execute actions atomically with rollback
4. **Better error handling** — surface bd CLI errors to dashboard, don't silently swallow
5. **Idempotency via state** — instead of `idem:` strings, check actual issue existence

This is lower-risk but keeps the central bottleneck.
