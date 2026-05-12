"""Tests for §13 — Claude `--resume` for the manager (cross-iteration session continuity)."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_process import AgentConfig, AgentProcess  # noqa: E402


def _make_proc(tmp_path: Path, *, resume: bool, role: str = "manager") -> AgentProcess:
    role_prompt = tmp_path / "manager.md"
    role_prompt.write_text("# manager role manual\n")
    cfg = AgentConfig(
        team="t",
        role=role,
        slot=role,
        team_dir=tmp_path,
        role_prompt_path=role_prompt,
        model="opus",
        agent_provider="claude",
        permission_mode="bypassPermissions",
        claude_resume_session=resume,
    )
    return AgentProcess(cfg)


def test_resume_disabled_uses_fresh_session(tmp_path: Path) -> None:
    proc = _make_proc(tmp_path, resume=False)
    cmd, sid, resumed = proc._build_claude_cmd("starter", str(proc.cfg.role_prompt_path),
                                               "bypassPermissions", "opus")
    assert sid is None
    assert resumed is False
    assert "--session-id" not in cmd
    assert "--resume" not in cmd
    assert "--append-system-prompt" in cmd


def test_resume_first_call_assigns_and_persists_session_id(tmp_path: Path) -> None:
    proc = _make_proc(tmp_path, resume=True)
    cmd, sid, resumed = proc._build_claude_cmd("starter", str(proc.cfg.role_prompt_path),
                                               "bypassPermissions", "opus")
    assert sid is not None
    assert resumed is False  # first call generates, not resumes
    uuid.UUID(sid)  # validates well-formed UUID
    assert "--session-id" in cmd and sid in cmd
    assert "--append-system-prompt" in cmd

    # Persistence: re-loading state from a second AgentProcess sees the saved id.
    proc2 = _make_proc(tmp_path, resume=True)
    assert proc2.state.extras.get("claude_session_id") == sid


def test_resume_second_call_uses_resume_no_system_prompt(tmp_path: Path) -> None:
    proc = _make_proc(tmp_path, resume=True)
    _, sid, _ = proc._build_claude_cmd("first", str(proc.cfg.role_prompt_path),
                                        "bypassPermissions", "opus")

    cmd2, sid2, resumed = proc._build_claude_cmd("second", str(proc.cfg.role_prompt_path),
                                                  "bypassPermissions", "opus")
    assert resumed is True
    assert sid2 == sid
    assert "--resume" in cmd2
    assert "--session-id" not in cmd2
    assert "--append-system-prompt" not in cmd2
    assert "second" in cmd2


def test_stderr_session_missing_detection(tmp_path: Path) -> None:
    err = tmp_path / "err.txt"
    # Positives
    for phrasing in (
        "Error: session not found",
        "session does not exist anymore",
        "the session has expired",
        "no such session",
        "could not find session 12ab",
    ):
        err.write_text(phrasing)
        assert AgentProcess._stderr_session_missing(str(err)), phrasing
    # Negatives
    for non_match in (
        "rate limit exceeded",
        "model claude-opus-4-5 is unavailable",
        "ECONNRESET",
        "permission denied for tool: edit",
        "",
        "session active",  # has 'session' but no failure phrase
    ):
        err.write_text(non_match)
        assert not AgentProcess._stderr_session_missing(str(err)), non_match


def test_resume_clears_session_id_when_first_id_persisted(tmp_path: Path) -> None:
    """After explicitly clearing claude_session_id, the next build_claude_cmd
    assigns a brand-new UUID and treats this as a first-time session (not a
    resume). This is the recovery hook for the stderr-session-missing path."""
    proc = _make_proc(tmp_path, resume=True)
    _, sid, _ = proc._build_claude_cmd("a", str(proc.cfg.role_prompt_path),
                                        "bypassPermissions", "opus")
    # Simulate the recovery: drop the saved id.
    proc.state.extras.pop("claude_session_id", None)
    proc.store.save(proc.state)
    _, sid2, resumed = proc._build_claude_cmd("b", str(proc.cfg.role_prompt_path),
                                                "bypassPermissions", "opus")
    assert sid2 is not None and sid2 != sid
    assert resumed is False
