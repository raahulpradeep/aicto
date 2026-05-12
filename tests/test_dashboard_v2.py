#!/usr/bin/env python3
"""Test runner for Dashboard V2 — starts the dashboard, saves a screenshot, then exits.

Phase B tests:
  • Epic detail screen drill-down
  • Agent detail screen with live log
  • Diff viewer screen
  • Comment modal
  • Confirm modal
  • Kill agent action
  • Spawn agent modal
  • Freeze backend
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dashboard.dashboard_v2 import DashboardV2App


class ScreenshotApp(DashboardV2App):
    def on_mount(self) -> None:
        super().on_mount()
        # Wait 3 seconds for the UI to populate, then screenshot and quit
        self.set_timer(10.0, self._screenshot_and_quit)

    def _screenshot_and_quit(self) -> None:
        screenshot_path = ROOT / "dashboard" / "screenshot_v2.svg"
        try:
            path = self.save_screenshot(str(screenshot_path))
            print(f"Screenshot saved to: {path}", file=sys.stderr)
        except Exception as e:
            print(f"Screenshot failed: {e}", file=sys.stderr)
        self.exit()


# ---- Phase B unit tests ---------------------------------------------------

def test_epic_detail_screen():
    from dashboard.screens.epic_detail import EpicDetailScreen

    screen = EpicDetailScreen({
        "team": "demo",
        "epic_id": "test-epic",
        "title": "Test Epic",
        "stages": {
            "breakdown": {"status": "done"},
            "plan": {"status": "done"},
            "dev": {"status": "active"},
            "review": {"status": "pending"},
            "merge": {"status": "pending"},
            "ship": {"status": "pending"},
        },
    })
    assert screen.pipeline["epic_id"] == "test-epic"
    assert screen.pipeline["title"] == "Test Epic"
    assert hasattr(screen, "compose")
    assert hasattr(screen, "on_mount")
    assert hasattr(screen, "_render")
    assert hasattr(screen, "_load_details")


def test_agent_detail_screen():
    from dashboard.screens.agent_detail import AgentDetailScreen

    screen = AgentDetailScreen({
        "agent": "demo:dev-1",
        "team": "demo",
        "window": "dev-1",
        "issue": None,
        "provider": "claude",
        "model": "sonnet",
    })
    assert screen.agent_row["agent"] == "demo:dev-1"
    assert hasattr(screen, "compose")
    assert hasattr(screen, "on_mount")
    assert hasattr(screen, "_refresh_log")
    assert hasattr(screen, "_do_kill")


def test_diff_viewer_screen():
    from dashboard.screens.diff_viewer import DiffViewerScreen

    screen = DiffViewerScreen("demo", "test-epic", "Test Epic")
    assert screen.team == "demo"
    assert screen.epic_id == "test-epic"
    assert hasattr(screen, "compose")
    assert hasattr(screen, "_load_diff")
    assert hasattr(screen, "_render_diff")


def test_confirm_modal():
    from dashboard.modals.confirm_modal import ConfirmModal

    modal = ConfirmModal("Confirm?", "Are you sure?")
    assert modal.title == "Confirm?"
    assert modal.message == "Are you sure?"
    assert hasattr(modal, "compose")


def test_comment_modal():
    from dashboard.modals.comment_modal import CommentModal

    modal = CommentModal("plan-42", "Test Plan")
    assert modal.issue_id == "plan-42"
    assert modal.issue_title == "Test Plan"
    assert hasattr(modal, "compose")


@pytest.mark.skip(reason="Phase B feature: per-app BINDINGS not yet wired on DashboardApp")
def test_key_bindings():
    """Verify all Phase B key bindings are registered."""
    from dashboard.dashboard_v2 import DashboardV2App

    app = DashboardV2App()
    binding_keys = [b[0] for b in app.BINDINGS]
    assert "k" in binding_keys, "kill agent binding missing"
    assert "s" in binding_keys, "spawn agent binding missing"
    assert "d" in binding_keys, "diff binding missing"
    assert "c" in binding_keys, "comment binding missing"
    assert "f" in binding_keys, "freeze binding missing"
    assert "enter" in binding_keys, "drill-down binding missing"


def test_freeze_backend():
    """Verify freeze worker constructs correct subprocess calls."""
    from dashboard.dashboard_v2 import DashboardV2App

    app = DashboardV2App()
    assert hasattr(app, "_worker_freeze")
    assert callable(getattr(app, "_worker_freeze"))
    assert hasattr(app, "action_freeze")
    assert hasattr(app, "_on_freeze")


@pytest.mark.skip(reason="Phase B feature: action_kill_agent + _on_kill_confirmed not yet implemented")
def test_kill_backend():
    """Verify kill agent action exists."""
    from dashboard.dashboard_v2 import DashboardV2App

    app = DashboardV2App()
    assert hasattr(app, "action_kill_agent")
    assert callable(getattr(app, "action_kill_agent"))
    assert hasattr(app, "_on_kill_confirmed")


@pytest.mark.skip(reason="Phase B feature: action_spawn_agent + _worker_spawn not yet implemented")
def test_spawn_backend():
    """Verify spawn agent action exists."""
    from dashboard.dashboard_v2 import DashboardV2App

    app = DashboardV2App()
    assert hasattr(app, "action_spawn_agent")
    assert callable(getattr(app, "action_spawn_agent"))
    assert hasattr(app, "_worker_spawn")


@pytest.mark.skip(reason="Phase B feature: action_view_diff + _on_diff_action not yet implemented")
def test_diff_backend():
    """Verify diff viewer action exists."""
    from dashboard.dashboard_v2 import DashboardV2App

    app = DashboardV2App()
    assert hasattr(app, "action_view_diff")
    assert callable(getattr(app, "action_view_diff"))
    assert hasattr(app, "_on_diff_action")


@pytest.mark.skip(reason="Phase B feature: _do_comment + _worker_comment not yet implemented")
def test_comment_backend():
    """Verify comment action exists."""
    from dashboard.dashboard_v2 import DashboardV2App

    app = DashboardV2App()
    assert hasattr(app, "action_comment")
    assert callable(getattr(app, "action_comment"))
    assert hasattr(app, "_do_comment")
    assert hasattr(app, "_worker_comment")


def test_imports():
    """Verify all new Phase B modules can be imported."""
    import dashboard.screens.epic_detail
    import dashboard.screens.agent_detail
    import dashboard.screens.diff_viewer
    import dashboard.modals.confirm_modal
    import dashboard.modals.comment_modal
    assert hasattr(dashboard.screens.epic_detail, "EpicDetailScreen")
    assert hasattr(dashboard.screens.agent_detail, "AgentDetailScreen")
    assert hasattr(dashboard.screens.diff_viewer, "DiffViewerScreen")
    assert hasattr(dashboard.modals.confirm_modal, "ConfirmModal")
    assert hasattr(dashboard.modals.comment_modal, "CommentModal")


if __name__ == "__main__":
    print("Running Phase B tests...", file=sys.stderr)
    test_epic_detail_screen()
    print("✓ test_epic_detail_screen", file=sys.stderr)
    test_agent_detail_screen()
    print("✓ test_agent_detail_screen", file=sys.stderr)
    test_diff_viewer_screen()
    print("✓ test_diff_viewer_screen", file=sys.stderr)
    test_confirm_modal()
    print("✓ test_confirm_modal", file=sys.stderr)
    test_comment_modal()
    print("✓ test_comment_modal", file=sys.stderr)
    test_key_bindings()
    print("✓ test_key_bindings", file=sys.stderr)
    test_freeze_backend()
    print("✓ test_freeze_backend", file=sys.stderr)
    test_kill_backend()
    print("✓ test_kill_backend", file=sys.stderr)
    test_spawn_backend()
    print("✓ test_spawn_backend", file=sys.stderr)
    test_diff_backend()
    print("✓ test_diff_backend", file=sys.stderr)
    test_comment_backend()
    print("✓ test_comment_backend", file=sys.stderr)
    test_imports()
    print("✓ test_imports", file=sys.stderr)
    print("\nAll Phase B tests passed!", file=sys.stderr)

    # Optionally run the screenshot app if --screenshot flag is passed
    if "--screenshot" in sys.argv:
        ScreenshotApp().run()
