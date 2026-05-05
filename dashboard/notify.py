"""Desktop notifications for the AI CTO workspace.

Fires on critical events only (inbox arrivals, agent crashes, review-loop
escalations). Workflow motion is visible in the Activity Stream panel but
does NOT trigger desktop notifications, to avoid notification fatigue.
"""
from __future__ import annotations

import platform
import subprocess


_system = platform.system()


def notify(title: str, body: str, urgency: str = "normal") -> None:
    """Best-effort desktop notification."""
    if _system == "Darwin":
        _notify_macos(title, body)
    elif _system == "Linux":
        _notify_linux(title, body, urgency)


def _notify_macos(title: str, body: str) -> None:
    script = (
        'display notification "{body}" '
        'with title "{title}"'
    ).format(body=_escape_applescript(body), title=_escape_applescript(title))
    subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
    )


def _notify_linux(title: str, body: str, urgency: str) -> None:
    subprocess.run(
        ["notify-send", title, body, "-u", urgency],
        capture_output=True,
    )


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
