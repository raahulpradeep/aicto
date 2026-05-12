"""Integration and backward-compatibility tests for the reconciler redesign.

Run with:  python -m pytest tests/test_reconciler_integration.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ and templates/ are on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "templates"))

import pytest

from workflow import Issue, State, EpicState
from health_auditor import HealthAuditor, HealKind
from action_queue import ActionQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue(id, kind="", target="", role="", status="open", description="", labels=(), close_reason="", issue_type="", created_at="", updated_at=""):
    if kind:
        labels = (*labels, f"kind:{kind}")
    if target:
        labels = (*labels, f"target:{target}")
    if role:
        labels = (*labels, f"role:{role}")
    return Issue(
        id=id, title=id, description=description, status=status,
        labels=labels, close_reason=close_reason, issue_type=issue_type,
        created_at=created_at, updated_at=updated_at,
    )

def _epic(id, **kwargs):
    return _issue(id, kind="epic", issue_type="epic", **kwargs)


# ---------------------------------------------------------------------------
# Backward compatibility: old reconciler API surface
# ---------------------------------------------------------------------------

class TestReconcilerCompat:
    def test_import_reconcile_and_execute(self):
        from reconciler import reconcile, execute, Issue, State, Action
        # Should not raise

    def test_reconcile_returns_list(self):
        from reconciler import reconcile
        epic = _epic("e1")
        state = State(issues=(epic,))
        actions = reconcile(state)
        assert isinstance(actions, list)

    def test_load_state_exists(self):
        from reconciler import load_state
        assert callable(load_state)

    def test_health_auditor_re_exported(self):
        from reconciler import HealthAuditor, ActionQueue
        assert HealthAuditor is not None
        assert ActionQueue is not None


# ---------------------------------------------------------------------------
# Integration: full epic lifecycle via new components
# ---------------------------------------------------------------------------

class TestFullLifecycleIntegration:
    def test_health_auditor_detects_stuck_breakdown(self):
        """Epic created 20m ago, no breakdown filed → escalate."""
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)).isoformat()
        epic = _epic("e1", created_at=old)
        state = State(issues=(epic,))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        assert any(a.kind == HealKind.ESCALATE_CTO for a in actions)

    def test_health_auditor_detects_missing_review(self):
        """Dev closed but reviewer forgot to file review → heal."""
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        state = State(issues=(epic, dev))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        heal = [a for a in actions if a.kind == HealKind.FILE_REVIEW]
        assert len(heal) == 1
        assert heal[0].target_id == "d1"

    def test_health_auditor_detects_missing_merge(self):
        """Review approved but no merge filed → heal."""
        epic = _epic("e1")
        dev = _issue("d1", kind="dev", status="closed", description="epic: e1")
        review = _issue("r1", kind="review", target="code", status="closed",
                        description="epic: e1\nupstream: d1")
        state = State(issues=(epic, dev, review))
        auditor = HealthAuditor()
        actions = auditor.audit(state)
        heal = [a for a in actions if a.kind == HealKind.FILE_MERGE]
        assert len(heal) == 1
        assert heal[0].target_id == "d1"

    def test_action_queue_preview(self):
        """Preview mode should not execute."""
        queue = ActionQueue()
        from action_queue import FileIssue
        actions = [FileIssue(title="Test", description="d", labels=("role:developer", "kind:dev"))]
        preview = queue.preview(actions)
        assert any("file:" in line for line in preview)
        # preview() does not mutate history; submit(..., dry_run=True) does
        preview2 = queue.submit(actions, dry_run=True).preview()
        assert queue.history() == [preview2]

    def test_reconciler_new_flags(self):
        """The shrunk reconciler supports --preview flag."""
        from reconciler import main
        # --dry-run with empty state should return 0 (can't actually load state without bd)
        # but we can at least verify arg parsing doesn't crash
        rc = main(["--role", "manager", "--dry-run"])
        assert rc == 0
        rc = main(["--role", "manager", "--preview"])
        assert rc == 0

    def test_reconciler_non_manager_returns_zero(self):
        """Only role=manager reconciles; others noop."""
        from reconciler import main
        rc = main(["--role", "developer"])
        assert rc == 0


# ---------------------------------------------------------------------------
# Regression: verify old test suite still loads
# ---------------------------------------------------------------------------

class TestRegression:
    def test_old_imports_still_work(self):
        """All old public names are still importable from reconciler."""
        import reconciler
        names = ["Issue", "State", "FileIssue", "FilePair", "AddLabel",
                 "RemoveLabel", "ReopenIssue", "CloseIssue", "AutoMergeEpic",
                 "reconcile", "execute", "load_state"]
        for name in names:
            assert hasattr(reconciler, name), f"reconciler missing {name}"

    def test_old_issue_api(self):
        """Issue dataclass API is preserved."""
        issue = _issue("x1", kind="dev", target="code", role="developer")
        assert issue.kind == "dev"
        assert issue.target == "code"
        assert issue.role == "developer"
        assert issue.is_open
        assert issue.has_label("kind:dev")
        assert issue.linked_epic() is None

    def test_old_state_api(self):
        """State dataclass API is preserved."""
        epic = _epic("e1")
        child = _issue("c1", kind="breakdown", description="epic: e1")
        state = State(issues=(epic, child))
        assert state.by_id("e1") == epic
        assert state.by_id("missing") is None
        assert len(state.epics()) == 1
        assert len(state.children_of("e1")) == 1
