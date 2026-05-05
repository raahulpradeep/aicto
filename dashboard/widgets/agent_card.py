"""Agent status card widget for the CTO dashboard.

Richer than a table row: shows status dot, elapsed time, model badge,
tokens used, and current issue in a compact card format.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual.reactive import reactive
from textual.widgets import Static

TEAMS_DIR = Path(__file__).resolve().parent.parent.parent / "teams"


def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _human_duration(seconds: int) -> str:
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s and m < 10 else f"{m}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    h, _ = divmod(rem, 3600)
    return f"{d}d {h}h" if h else f"{d}d"


class AgentCard(Static):
    """Compact agent status card.

    Shows: agent name, status (working/idle/crashed), elapsed time,
    model badge, tokens used this session, current issue.
    """

    agent_data = reactive[dict[str, Any]]({})
    tokens_used = reactive(0)
    selected = reactive(False)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def set_agent(self, data: dict[str, Any]) -> None:
        self.agent_data = data

    def set_tokens(self, tokens: int) -> None:
        self.tokens_used = tokens

    def render(self) -> Panel:
        a = self.agent_data
        if not a:
            return Panel("—", border_style="dim")

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
        started = _parse_iso(issue.get("started_at") or issue.get("updated_at") or "") if issue else None
        elapsed_secs = int((now - started).total_seconds()) if started else 0

        # Status dot and color
        if status == "working":
            dot = "🟢"
            status_color = "green"
        elif status == "stale":
            dot = "🟡"
            status_color = "yellow"
        else:
            dot = "⚪"
            status_color = "dim"

        elapsed_str = _fmt_elapsed(max(0, elapsed_secs)) if started else "—"

        # Tokens (from reactive or agent_data)
        tokens = self.tokens_used or a.get("tokens_used", 0)
        tokens_str = f"{tokens / 1000:.1f}K" if tokens >= 1000 else str(tokens)

        # Build the card content
        header = Text.assemble(
            (dot, status_color),
            " ",
            (agent_name, "bold" + (" dim" if stale else "")),
        )

        details = Table(expand=True, show_header=False, show_lines=False, pad_edge=False, box=None)
        details.add_column("KEY", width=8, style="dim")
        details.add_column("VALUE", ratio=1)

        details.add_row("status", Text(status, style=status_color))
        details.add_row("elapsed", Text(elapsed_str, style=status_color if status == "working" else "dim"))
        details.add_row("model", Text(f"{provider}:{model}", style="cyan" if not stale else "dim"))
        details.add_row("tokens", Text(tokens_str, style="magenta" if not stale else "dim"))
        if issue:
            issue_text = f"{issue['id']} — {issue.get('title', '')[:40]}"
            details.add_row("issue", Text(issue_text, style="white" if not stale else "dim"))
        else:
            details.add_row("issue", Text("—", style="dim"))

        # Combine into a compact panel
        content = Text.assemble(header, "\n", "─" * 20, "\n")
        # We can't mix Text and Table directly, so use a Panel with the table
        border = "bright_cyan" if self.selected else ("cyan" if not stale else "dim")

        return Panel(
            details,
            title=f"{agent_name}",
            border_style=border,
            padding=(0, 1),
        )
