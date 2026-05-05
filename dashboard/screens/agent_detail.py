"""Per-agent live log screen.

Shows agent status, model, session tokens, and live log tail from tmux capture-pane.
Includes kill/restart buttons.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import time
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _capture_tmux_log(session: str, window: str, lines: int = 50) -> str:
    """Capture last N lines from a tmux pane."""
    try:
        out = _run(["tmux", "capture-pane", "-t", f"{session}:{window}", "-p", "-S", f"-{lines}"], timeout=3.0)
        return out
    except Exception:
        return "(could not capture tmux log)"


def _kill_agent(session: str, window: str, signal: int = 15) -> bool:
    """Send signal to a tmux pane."""
    try:
        if signal == 9:
            # Kill -9: find the PID and kill it
            pid_out = _run(["tmux", "list-panes", "-t", f"{session}:{window}", "-F", "#{pane_pid}"], timeout=2.0)
            pid = pid_out.strip()
            if pid and pid.isdigit():
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=2.0)
                return True
        else:
            # Send Ctrl-C
            subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window}", "C-c"], capture_output=True, timeout=2.0)
            return True
    except Exception:
        pass
    return False


def _restart_agent(team: str, window: str) -> bool:
    """Restart an agent via cto restart."""
    try:
        root = TEAMS_DIR.parent
        result = subprocess.run(
            [str(root / "bin" / "cto"), "restart", team],
            capture_output=True, text=True, timeout=30.0,
        )
        return result.returncode == 0
    except Exception:
        return False


class AgentDetailScreen(ModalScreen[None]):
    """Live agent detail with log tail and control buttons."""

    log_lines = reactive[str]("")
    agent_data = reactive[dict[str, Any]]({})
    _refresh_interval: float = 1.0
    _timer_handle: Any = None

    def __init__(self, agent_row: dict[str, Any]) -> None:
        super().__init__()
        self.agent_row = agent_row
        self.agent_data = agent_row
        self.session = f"cto-{agent_row['team']}"
        self.window = agent_row["window"]

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-detail"):
            yield Static("", id="agent-header")
            yield Static("", id="agent-stats")
            yield Static("", id="agent-log")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Kill (SIGTERM)", variant="error", id="kill")
                yield Button("Kill -9", variant="error", id="kill9")
                yield Button("Restart", variant="warning", id="restart")
                yield Button("Back", variant="default", id="back")

    def on_mount(self) -> None:
        self._update_display()
        # Start periodic refresh of the log
        self._timer_handle = self.set_interval(self._refresh_interval, self._refresh_log)

    def on_unmount(self) -> None:
        if self._timer_handle:
            try:
                self._timer_handle.stop()
            except Exception:
                pass

    def _refresh_log(self) -> None:
        log = _capture_tmux_log(self.session, self.window, lines=50)
        self.log_lines = log

    def watch_log_lines(self, log: str) -> None:
        self._update_log_display(log)

    def _update_display(self) -> None:
        a = self.agent_data
        agent_name = a.get("agent", "unknown")
        team = a.get("team", "")
        issue = a.get("issue")
        provider = a.get("provider", "—")
        model = a.get("model", "—")
        stale = a.get("stale", False)

        status = "idle"
        if stale:
            status = "stale"
        elif issue:
            status = "working"

        now = dt.datetime.now(dt.timezone.utc)
        started = None
        if issue:
            started_str = issue.get("started_at") or issue.get("updated_at") or ""
            try:
                started = dt.datetime.fromisoformat(started_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        elapsed_secs = int((now - started).total_seconds()) if started else 0

        if status == "working":
            status_text = f"🟢 WORKING  {_fmt_elapsed(max(0, elapsed_secs))}"
        elif status == "stale":
            status_text = f"🟡 STALE"
        else:
            status_text = f"⚪ IDLE"

        header = f"Agent: {agent_name}\n{status_text}"
        self.query_one("#agent-header", Static).update(header)

        tokens = a.get("tokens_used", 0)
        tokens_str = f"{tokens / 1000:.1f}K" if tokens >= 1000 else str(tokens)

        issue_text = "—"
        if issue:
            issue_text = f"{issue['id']} — {issue.get('title', '')[:50]}"

        stats = (
            f"Model: {provider}:{model}  |  "
            f"Session tokens: {tokens_str}  |  "
            f"Current issue: {issue_text}"
        )
        self.query_one("#agent-stats", Static).update(stats)

        # Initial log load
        log = _capture_tmux_log(self.session, self.window, lines=50)
        self._update_log_display(log)

    def _update_log_display(self, log: str) -> None:
        if not log:
            log = "(no log output yet)"
        # Truncate to last 50 lines to avoid overwhelming the display
        lines = log.splitlines()
        if len(lines) > 50:
            lines = lines[-50:]
        display = "\n".join(lines)

        # Try to render with syntax highlighting if it looks like code
        panel = Panel(
            Text(display, no_wrap=False),
            title="Live Log (tmux capture-pane)",
            border_style="cyan",
        )
        try:
            self.query_one("#agent-log", Static).update(panel)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.dismiss()
        elif event.button.id == "kill":
            self._do_kill(15)
        elif event.button.id == "kill9":
            self._do_kill(9)
        elif event.button.id == "restart":
            self._do_restart()

    def _do_kill(self, signal: int) -> None:
        name = "SIGTERM" if signal == 15 else "SIGKILL"
        if _kill_agent(self.session, self.window, signal):
            self.notify(f"Sent {name} to {self.agent_row['agent']}", title="Kill")
        else:
            self.notify(f"Failed to kill {self.agent_row['agent']}", severity="error", title="Error")
        # Refresh log after kill
        self._refresh_log()

    def _do_restart(self) -> None:
        if _restart_agent(self.agent_row["team"], self.window):
            self.notify(f"Restarted team {self.agent_row['team']}", title="Restart")
        else:
            self.notify(f"Failed to restart {self.agent_row['agent']}", severity="error", title="Error")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()
        elif event.key == "k":
            self._do_kill(15)


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"
