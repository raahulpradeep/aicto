#!/usr/bin/env python3
"""Screenshot test for Dashboard V2 Phase B screens.

Opens each new screen in sequence and saves SVG screenshots.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from dashboard.screens.epic_detail import EpicDetailScreen
from dashboard.screens.agent_detail import AgentDetailScreen
from dashboard.screens.diff_viewer import DiffViewerScreen
from dashboard.modals.confirm_modal import ConfirmModal
from dashboard.modals.comment_modal import CommentModal


class ScreenshotTestApp(App):
    """Minimal app for screenshotting individual screens."""

    CSS = """
    Screen { align: center middle; }
    """

    def on_mount(self) -> None:
        self._screenshot_idx = 0
        self._screenshots = [
            ("epic_detail", self._show_epic_detail),
            ("agent_detail", self._show_agent_detail),
            ("diff_viewer", self._show_diff_viewer),
            ("confirm_modal", self._show_confirm),
            ("comment_modal", self._show_comment),
        ]
        self._next_screenshot()

    def _next_screenshot(self) -> None:
        if self._screenshot_idx >= len(self._screenshots):
            self.exit()
            return
        name, fn = self._screenshots[self._screenshot_idx]
        self._screenshot_idx += 1
        fn(name)

    def _show_epic_detail(self, name: str) -> None:
        screen = EpicDetailScreen({
            "team": "demo",
            "epic_id": "api-v2",
            "title": "Refactor API layer for v2",
            "stages": {
                "breakdown": {"status": "done"},
                "plan": {"status": "done"},
                "dev": {"status": "active"},
                "review": {"status": "pending"},
                "merge": {"status": "pending"},
                "ship": {"status": "pending"},
            },
        })
        self.push_screen(screen)
        self.set_timer(2.0, lambda: self._save_and_next(name))

    def _show_agent_detail(self, name: str) -> None:
        screen = AgentDetailScreen({
            "agent": "demo:dev-2",
            "team": "demo",
            "window": "dev-2",
            "issue": {
                "id": "plan-34",
                "title": "Extract auth handler",
                "started_at": "2026-05-05T14:00:00Z",
            },
            "provider": "kimi",
            "model": "k2",
        })
        self.push_screen(screen)
        self.set_timer(2.0, lambda: self._save_and_next(name))

    def _show_diff_viewer(self, name: str) -> None:
        screen = DiffViewerScreen("demo", "api-v2", "Refactor API layer for v2")
        self.push_screen(screen)
        self.set_timer(2.0, lambda: self._save_and_next(name))

    def _show_confirm(self, name: str) -> None:
        modal = ConfirmModal("Kill demo:dev-2?", "This will send SIGTERM to the agent.")
        self.push_screen(modal)
        self.set_timer(2.0, lambda: self._save_and_next(name))

    def _show_comment(self, name: str) -> None:
        modal = CommentModal("plan-34", "Extract auth handler")
        self.push_screen(modal)
        self.set_timer(2.0, lambda: self._save_and_next(name))

    def _save_and_next(self, name: str) -> None:
        path = ROOT / "dashboard" / f"screenshot_{name}.svg"
        try:
            self.save_screenshot(str(path))
            print(f"Screenshot saved: {path}", file=sys.stderr)
        except Exception as e:
            print(f"Screenshot failed for {name}: {e}", file=sys.stderr)
        self.pop_screen()
        self.set_timer(0.5, self._next_screenshot)


if __name__ == "__main__":
    ScreenshotTestApp().run()
