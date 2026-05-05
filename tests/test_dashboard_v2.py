#!/usr/bin/env python3
"""Test runner for Dashboard V2 — starts the dashboard, saves a screenshot, then exits.
"""
import sys
import time
from pathlib import Path

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

if __name__ == "__main__":
    ScreenshotApp().run()
