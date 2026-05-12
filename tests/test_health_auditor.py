"""Tests for the health auditor (health_auditor.py).

Run with:  python -m pytest tests/test_health_auditor.py -v
"""
from __future__ import annotations

import datetime
from typing import Any

import pytest

from health_auditor import HealthAuditor, HealAction, HealKind
from workflow import Issue, State


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
    created_at: str = "",
    updated_at: str = "",
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
        created_at=created_at,
        updated_at=updated_at,
    )


def _epic(id: str, **kwargs: Any) -> Issue:
    labels = kwargs.pop("labels", ())
    return _issue(id, kind="epic", issue_type="epic", role="manager", labels=labels, **kwargs)


# ---------------------------------------------------------------------------
# HealthAuditor.audit
# ---------------------------------------------------------------------------

class TestHealthAuditor:
    def test_empty_state(self) -> None:
        auditor = HealthAuditor()
        actions = auditor.audit(State(issues=()))
        assert actions == []

    def test_orphaned_epic_after_15min(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        epic = _epic("e1", created_at=old)
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.ESCALATE_CTO and a.issue_id == "e1" for a in actions)

    def test_orphaned_epic_within_grace_period(self) -> None:
        recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
        epic = _epic("e1", created_at=recent)
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.ESCALATE_CTO for a in actions)

    def test_epic_with_children_not_orphaned(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        epic = _epic("e1", created_at=old)
        child = _issue("c1", kind="breakdown", description="epic: e1")
        state = State(issues=(epic, child))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.ESCALATE_CTO for a in actions)

    def test_zombie_detection(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        issue = _issue("i1", kind="dev", status="in_progress", updated_at=old)
        state = State(issues=(issue,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.RESET_CLAIM and a.issue_id == "i1" for a in actions)

    def test_no_zombie_within_threshold(self) -> None:
        recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
        issue = _issue("i1", kind="dev", status="in_progress", updated_at=recent)
        state = State(issues=(issue,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.RESET_CLAIM for a in actions)

    def test_missing_kind_epic_label(self) -> None:
        epic = _issue("e1", issue_type="epic", labels=())  # no kind:epic
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.ADD_LABEL and a.label == "kind:epic" for a in actions)

    def test_missing_role_manager_label(self) -> None:
        epic = _issue("e1", kind="epic", issue_type="epic", labels=())  # no role:manager
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.ADD_LABEL and a.label == "role:manager" for a in actions)

    def test_missing_role_on_dev(self) -> None:
        dev = _issue("d1", kind="dev", labels=())
        state = State(issues=(dev,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.ADD_LABEL and a.label == "role:developer" for a in actions)

    def test_missing_role_on_review(self) -> None:
        review = _issue("r1", kind="review", labels=())
        state = State(issues=(review,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.ADD_LABEL and a.label == "role:reviewer" for a in actions)

    def test_review_loop_escalation(self) -> None:
        dev = _issue("d1", kind="dev", status="closed")
        reviews = [
            _issue(f"r{i}", kind="review", target="code", status="closed",
                   description=f"epic: e1\nupstream: d1")
            for i in range(4)
        ]
        state = State(issues=(dev, *reviews))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(
            a.kind == HealKind.ESCALATE_CTO and a.issue_id == "d1" and "review loop" in a.reason
            for a in actions
        )

    def test_no_escalation_under_review_loop_threshold(self) -> None:
        dev = _issue("d1", kind="dev", status="closed")
        reviews = [
            _issue(f"r{i}", kind="review", target="code", status="closed",
                   description=f"epic: e1\nupstream: d1")
            for i in range(3)
        ]
        state = State(issues=(dev, *reviews))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.ESCALATE_CTO and a.issue_id == "d1" for a in actions)

    def test_dev_closed_but_no_review_filed(self) -> None:
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        state = State(issues=(epic, dev))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(
            a.kind == HealKind.FILE_REVIEW and a.target_id == "d1" and a.epic_id == "e1"
            for a in actions
        )

    def test_dev_closed_with_review_filed_no_heal(self) -> None:
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        review = _issue("r1", kind="review", target="code", description="epic: e1\nupstream: d1")
        state = State(issues=(epic, dev, review))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.FILE_REVIEW for a in actions)

    def test_review_approved_but_no_merge_filed(self) -> None:
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        review = _issue("r1", kind="review", target="code", status="closed",
                        description="epic: e1\nupstream: d1")
        state = State(issues=(epic, dev, review))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(
            a.kind == HealKind.FILE_MERGE and a.target_id == "d1" and a.epic_id == "e1"
            for a in actions
        )

    def test_review_approved_with_merge_filed_no_heal(self) -> None:
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        review = _issue("r1", kind="review", target="code", status="closed",
                        description="epic: e1\nupstream: d1")
        merge = _issue("m1", kind="merge", target="code", description="epic: e1\nupstream: d1")
        state = State(issues=(epic, dev, review, merge))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.FILE_MERGE for a in actions)

    def test_breakdown_done_but_no_plan(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed", description="epic: e1")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(
            a.kind == HealKind.ESCALATE_CTO and a.issue_id == "e1" and "no plan filed" in a.reason
            for a in actions
        )

    def test_plan_done_but_no_dev(self) -> None:
        epic = _epic("e1")
        bd = _issue("bd1", kind="breakdown", status="closed", description="epic: e1")
        bd_merge = _issue("m1", kind="merge", target="breakdown", status="closed", description="epic: e1")
        plan = _issue("p1", kind="plan", status="closed", description="epic: e1")
        plan_merge = _issue("m2", kind="merge", target="plan", status="closed", description="epic: e1")
        state = State(issues=(epic, bd, bd_merge, plan, plan_merge))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(
            a.kind == HealKind.ESCALATE_CTO and a.issue_id == "e1" and "no dev tasks filed" in a.reason
            for a in actions
        )

    def test_stuck_epic_no_children_1h(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()
        epic = _epic("e1", created_at=old)
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        # Should be caught by leak detection (orphaned epic)
        assert any(a.kind == HealKind.ESCALATE_CTO for a in actions)

    def test_heal_action_is_auto_safe(self) -> None:
        assert HealAction(kind=HealKind.ADD_LABEL).is_auto_safe
        assert HealAction(kind=HealKind.RESET_CLAIM).is_auto_safe
        assert HealAction(kind=HealKind.ESCALATE_CTO).is_auto_safe is False

    def test_multiple_epics(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        e1 = _epic("e1", created_at=old)
        e2 = _epic("e2", created_at=old)
        bd = _issue("bd1", kind="breakdown", description="epic: e1")
        state = State(issues=(e1, e2, bd))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        # e1 has child, e2 is orphaned
        assert any(a.issue_id == "e2" for a in actions)
        assert not any(a.issue_id == "e1" for a in actions)

    def test_closed_epic_ignored(self) -> None:
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        epic = _epic("e1", status="closed", created_at=old)
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.issue_id == "e1" for a in actions)

    def test_changes_requested_review_not_yet_approved(self) -> None:
        """A review closed changes-requested should NOT trigger a merge filing."""
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        review = _issue("r1", kind="review", target="code", status="closed",
                        close_reason="changes-requested", description="epic: e1\nupstream: d1")
        state = State(issues=(epic, dev, review))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert not any(a.kind == HealKind.FILE_MERGE for a in actions)
