"""Tests for the explicit workflow state machine (workflow.py).

Run with:  python -m pytest tests/test_workflow.py -v
"""
from __future__ import annotations

import pytest

from workflow import (
    EpicState,
    Issue,
    State,
    TRANSITIONS,
    allowed_next_states,
    compute_state,
    is_transition_allowed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue(
    id: str,
    title: str = "",
    kind: str = "",
    target: str = "",
    role: str = "",
    status: str = "open",
    description: str = "",
    labels: tuple[str, ...] = (),
    close_reason: str = "",
    issue_type: str = "",
) -> Issue:
    if kind:
        labels = (*labels, f"kind:{kind}")
    if target:
        labels = (*labels, f"target:{target}")
    if role:
        labels = (*labels, f"role:{role}")
    return Issue(
        id=id,
        title=title or id,
        description=description,
        status=status,
        labels=labels,
        close_reason=close_reason,
        issue_type=issue_type,
    )


def _epic(id: str, **kwargs) -> Issue:
    return _issue(id, kind="epic", issue_type="epic", **kwargs)


# ---------------------------------------------------------------------------
# Transition graph
# ---------------------------------------------------------------------------

class TestTransitionGraph:
    def test_all_states_have_entry(self) -> None:
        for state in EpicState:
            if state == EpicState.UNKNOWN:
                continue
            assert state in TRANSITIONS

    def test_created_leads_to_breakdown(self) -> None:
        assert EpicState.BREAKDOWN_OPEN in allowed_next_states(EpicState.CREATED)

    def test_shipped_is_terminal(self) -> None:
        assert allowed_next_states(EpicState.SHIPPED) == []

    def test_changes_requested_loops_back(self) -> None:
        assert EpicState.DEV_IN_PROGRESS in allowed_next_states(EpicState.CHANGES_REQUESTED)

    def test_is_transition_allowed_positive(self) -> None:
        assert is_transition_allowed(EpicState.CREATED, EpicState.BREAKDOWN_OPEN)

    def test_is_transition_allowed_negative(self) -> None:
        assert not is_transition_allowed(EpicState.CREATED, EpicState.SHIPPED)


# ---------------------------------------------------------------------------
# compute_state — all states
# ---------------------------------------------------------------------------

class TestComputeState:
    def test_created_no_breakdown(self) -> None:
        epic = _epic("e1")
        state = State(issues=(epic,))
        assert compute_state(epic, [], state) == EpicState.CREATED

    def test_breakdown_open(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", description="epic: e1")
        state = State(issues=(epic, bd))
        assert compute_state(epic, [bd], state) == EpicState.BREAKDOWN_OPEN

    def test_breakdown_done(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", description="epic: e1", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", description="epic: e1", status="closed")
        state = State(issues=(epic, bd, bd_merge))
        assert compute_state(epic, [bd, bd_merge], state) == EpicState.BREAKDOWN_DONE

    def test_plan_open(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed", description="epic: e1")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed", description="epic: e1")
        plan = _issue("p1", kind="plan", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan))
        assert compute_state(epic, [bd, bd_merge, plan], state) == EpicState.PLAN_OPEN

    def test_plan_done(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed", description="epic: e1")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed", description="epic: e1")
        plan = _issue("p1", kind="plan", description="epic: e1")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge], state) == EpicState.PLAN_DONE

    def test_dev_in_progress(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev], state) == EpicState.DEV_IN_PROGRESS

    def test_dev_done(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev], state) == EpicState.DEV_DONE

    def test_review_in_progress(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review], state) == EpicState.REVIEW_IN_PROGRESS

    def test_changes_requested(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", status="closed",
                        close_reason="changes-requested", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review], state) == EpicState.CHANGES_REQUESTED

    def test_merge_ready(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", status="closed",
                        description="epic: e1\napproved")
        code_merge = _issue("m3", kind="merge", target="code", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review, code_merge], state) == EpicState.MERGE_READY

    def test_ship_ready(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", status="closed")
        code_merge = _issue("m3", kind="merge", target="code", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review, code_merge], state) == EpicState.SHIP_READY

    def test_shipped(self) -> None:
        epic = _epic("e1", status="closed")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", status="closed")
        code_merge = _issue("m3", kind="merge", target="code", status="closed")
        epic_merge = _issue("m4", kind="merge", target="epic", status="closed")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge], state) == EpicState.SHIPPED

    def test_ops_epic_no_dev(self) -> None:
        epic = _epic("e1", labels=("class:ops",))
        state = State(issues=(epic,))
        assert compute_state(epic, [], state) == EpicState.DEV_IN_PROGRESS

    def test_ops_epic_dev_closed(self) -> None:
        epic = _epic("e1", labels=("class:ops",))
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        state = State(issues=(epic, dev))
        assert compute_state(epic, [dev], state) == EpicState.SHIPPED

    def test_ops_epic_dev_open(self) -> None:
        epic = _epic("e1", labels=("class:ops",))
        dev = _issue("d1", kind="dev", description="epic: e1")
        state = State(issues=(epic, dev))
        assert compute_state(epic, [dev], state) == EpicState.DEV_IN_PROGRESS

    def test_needs_re_review_keeps_dev_in_progress(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed", labels=("needs-re-review",))
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev], state) == EpicState.DEV_IN_PROGRESS


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_epic_with_open_code_merge(self) -> None:
        """If a code merge is open, we're in MERGE_READY even if reviews closed."""
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", status="closed")
        code_merge = _issue("m3", kind="merge", target="code", status="open")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review, code_merge], state) == EpicState.MERGE_READY

    def test_not_enough_code_merges(self) -> None:
        """2 devs but only 1 code merge closed → still MERGE_READY."""
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev1 = _issue("d1", kind="dev", status="closed")
        dev2 = _issue("d2", kind="dev", status="closed")
        review1 = _issue("r1", kind="review", target="code", status="closed")
        review2 = _issue("r2", kind="review", target="code", status="closed")
        code_merge = _issue("m3", kind="merge", target="code", status="closed")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev1, dev2, review1, review2, code_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev1, dev2, review1, review2, code_merge], state) == EpicState.MERGE_READY

    def test_open_epic_merge(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        plan = _issue("p1", kind="plan")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed")
        dev = _issue("d1", kind="dev", status="closed")
        review = _issue("r1", kind="review", target="code", status="closed")
        code_merge = _issue("m3", kind="merge", target="code", status="closed")
        epic_merge = _issue("m4", kind="merge", target="epic", status="open")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge))
        assert compute_state(epic, [bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge], state) == EpicState.SHIP_READY

    def test_bypass_cto_no_special_handling(self) -> None:
        """compute_state is purely structural; bypass-cto is handled by health auditor / prompts."""
        epic = _epic("e1", labels=("class:bypass-cto",))
        bd = _issue("bd1", kind="breakdown", status="closed")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed")
        state = State(issues=(epic, bd, bd_merge))
        assert compute_state(epic, [bd, bd_merge], state) == EpicState.BREAKDOWN_DONE


# ---------------------------------------------------------------------------
# Integration: full epic lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_lifecycle_from_created_to_shipped(self) -> None:
        """Simulate every phase transition in order."""
        epic = _epic("e1")
        state = State(issues=(epic,))
        children = []

        # CREATED
        assert compute_state(epic, children, state) == EpicState.CREATED

        # File breakdown
        bd = _issue("bd1", kind="breakdown", description="epic: e1")
        state = State(issues=(epic, bd))
        children = [bd]
        assert compute_state(epic, children, state) == EpicState.BREAKDOWN_OPEN

        # Close breakdown + merge
        bd = _issue("bd1", kind="breakdown", status="closed", description="epic: e1")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge))
        children = [bd, bd_merge]
        assert compute_state(epic, children, state) == EpicState.BREAKDOWN_DONE

        # File plan
        plan = _issue("p1", kind="plan", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan))
        children = [bd, bd_merge, plan]
        assert compute_state(epic, children, state) == EpicState.PLAN_OPEN

        # Close plan + merge
        plan = _issue("p1", kind="plan", status="closed", description="epic: e1")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge))
        children = [bd, bd_merge, plan, plan_merge]
        assert compute_state(epic, children, state) == EpicState.PLAN_DONE

        # File dev
        dev = _issue("d1", kind="dev", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev))
        children = [bd, bd_merge, plan, plan_merge, dev]
        assert compute_state(epic, children, state) == EpicState.DEV_IN_PROGRESS

        # Close dev
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev))
        children = [bd, bd_merge, plan, plan_merge, dev]
        assert compute_state(epic, children, state) == EpicState.DEV_DONE

        # File review
        review = _issue("r1", kind="review", target="code", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review))
        children = [bd, bd_merge, plan, plan_merge, dev, review]
        assert compute_state(epic, children, state) == EpicState.REVIEW_IN_PROGRESS

        # Close review approved
        review = _issue("r1", kind="review", target="code", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review))
        children = [bd, bd_merge, plan, plan_merge, dev, review]
        assert compute_state(epic, children, state) == EpicState.MERGE_READY  # review approved, waiting for merge filing

        # File code merge
        code_merge = _issue("m3", kind="merge", target="code", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge))
        children = [bd, bd_merge, plan, plan_merge, dev, review, code_merge]
        assert compute_state(epic, children, state) == EpicState.MERGE_READY

        # Close code merge
        code_merge = _issue("m3", kind="merge", target="code", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge))
        children = [bd, bd_merge, plan, plan_merge, dev, review, code_merge]
        assert compute_state(epic, children, state) == EpicState.SHIP_READY

        # File epic merge
        epic_merge = _issue("m4", kind="merge", target="epic", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge))
        children = [bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge]
        assert compute_state(epic, children, state) == EpicState.SHIP_READY  # merge open

        # Close epic merge + epic itself
        epic_merge = _issue("m4", kind="merge", target="epic", status="closed")
        epic = _epic("e1", status="closed")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge))
        children = [bd, bd_merge, plan, plan_merge, dev, review, code_merge, epic_merge]
        assert compute_state(epic, children, state) == EpicState.SHIPPED
