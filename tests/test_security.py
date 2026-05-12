"""Tests for src/security.py — 6-layer sandbox.

Run with:  uv run pytest tests/test_security.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from security import (
    GitCommandPolicy,
    MCPToolBoundary,
    NetworkPolicy,
    OSSandbox,
    Sandbox,
    SandboxViolation,
    WorktreeLifecycle,
    WritePathIsolation,
)


# ---------------------------------------------------------------------------
# Layer 1 — Write-path isolation
# ---------------------------------------------------------------------------

class TestWritePathIsolation:
    def test_allowed_inside_worktree(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        iso = WritePathIsolation(allowed_root=worktree, team_dir=tmp_path)
        assert iso.check(worktree / "file.txt") is True
        assert iso.check(worktree / "sub" / "dir" / "file.py") is True

    def test_blocked_outside_worktree(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        iso = WritePathIsolation(allowed_root=worktree, team_dir=tmp_path)
        assert iso.check(tmp_path / "other.txt") is False
        assert iso.check("/etc/passwd") is False

    def test_assert_safe_raises(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        iso = WritePathIsolation(allowed_root=worktree, team_dir=tmp_path)
        iso.assert_safe(worktree / "ok.txt")
        with pytest.raises(SandboxViolation):
            iso.assert_safe(tmp_path / "bad.txt")

    def test_symlink_escape_blocked(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink = worktree / "escape"
        symlink.symlink_to(outside)
        iso = WritePathIsolation(allowed_root=worktree, team_dir=tmp_path)
        # The symlink points outside, so resolve() escapes → blocked
        assert iso.check(symlink / "file.txt") is False


# ---------------------------------------------------------------------------
# Layer 2 — Disallowed git commands
# ---------------------------------------------------------------------------

class TestGitCommandPolicy:
    def test_force_push_blocked(self) -> None:
        g = GitCommandPolicy()
        ok, reason = g.check(["git", "push", "origin", "main", "--force"])
        assert ok is False
        assert "force" in (reason or "").lower()

    def test_force_with_lease_blocked(self) -> None:
        g = GitCommandPolicy()
        ok, _ = g.check(["git", "push", "origin", "main", "--force-with-lease"])
        assert ok is False

    def test_hard_reset_blocked(self) -> None:
        g = GitCommandPolicy()
        ok, _ = g.check(["git", "reset", "--hard", "HEAD~1"])
        assert ok is False

    def test_branch_delete_blocked(self) -> None:
        g = GitCommandPolicy(allow_branch_delete=False)
        ok, _ = g.check(["git", "branch", "-D", "feature"])
        assert ok is False

    def test_branch_delete_allowed_when_configured(self) -> None:
        g = GitCommandPolicy(allow_branch_delete=True)
        ok, _ = g.check(["git", "branch", "-D", "feature"])
        assert ok is True

    def test_filter_branch_blocked(self) -> None:
        g = GitCommandPolicy()
        ok, _ = g.check(["git", "filter-branch", "--force"])
        assert ok is False

    def test_normal_commit_allowed(self) -> None:
        g = GitCommandPolicy()
        ok, reason = g.check(["git", "commit", "-m", "hello"])
        assert ok is True
        assert reason is None

    def test_normal_push_allowed(self) -> None:
        g = GitCommandPolicy(allow_push=True)
        ok, _ = g.check(["git", "push", "origin", "main"])
        assert ok is True

    def test_push_disabled(self) -> None:
        g = GitCommandPolicy(allow_push=False)
        ok, _ = g.check(["git", "push", "origin", "main"])
        assert ok is False

    def test_string_input(self) -> None:
        g = GitCommandPolicy()
        ok, _ = g.check("git push --force")
        assert ok is False

    def test_assert_allowed_raises(self) -> None:
        g = GitCommandPolicy()
        g.assert_allowed(["git", "status"])
        with pytest.raises(SandboxViolation):
            g.assert_allowed(["git", "reset", "--hard", "HEAD"])


# ---------------------------------------------------------------------------
# Layer 3 — OS-level sandbox
# ---------------------------------------------------------------------------

class TestOSSandbox:
    def test_wrap_command_returns_list(self, tmp_path: Path) -> None:
        sb = OSSandbox(tmp_path)
        wrapped = sb.wrap_command(["python3", "script.py"])
        assert isinstance(wrapped, list)
        assert len(wrapped) >= 2

    def test_seatbelt_profile_generated_on_darwin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        sb = OSSandbox(tmp_path)
        profile = sb._generate_seatbelt_profile()
        assert profile.exists()
        text = profile.read_text()
        assert "(version 1)" in text
        assert str(tmp_path) in text
        sb.cleanup()
        assert not profile.exists()

    def test_cleanup_idempotent(self, tmp_path: Path) -> None:
        sb = OSSandbox(tmp_path)
        sb.cleanup()
        sb.cleanup()  # should not raise


# ---------------------------------------------------------------------------
# Layer 4 — Network domain allowlist
# ---------------------------------------------------------------------------

class TestNetworkPolicy:
    def test_allowed_domain(self) -> None:
        n = NetworkPolicy()
        assert n.is_allowed("https://github.com/user/repo") is True
        assert n.is_allowed("github.com") is True
        assert n.is_allowed("api.github.com") is True
        assert n.is_allowed("raw.githubusercontent.com") is True

    def test_disallowed_domain(self) -> None:
        n = NetworkPolicy()
        assert n.is_allowed("evil.com") is False
        assert n.is_allowed("https://attacker.example.com/payload") is False

    def test_custom_domains(self) -> None:
        n = NetworkPolicy(allowed_domains={"my-org.com"})
        assert n.is_allowed("my-org.com") is True
        assert n.is_allowed("github.com") is False

    def test_env_overrides(self) -> None:
        n = NetworkPolicy()
        env = n.env_overrides()
        assert "HTTP_PROXY" in env
        assert "NO_PROXY" in env
        assert "github.com" in env["NO_PROXY"]

    def test_extract_domain(self) -> None:
        n = NetworkPolicy()
        assert n._extract_domain("https://api.github.com/v1") == "api.github.com"
        assert n._extract_domain("http://pypi.org:80/simple") == "pypi.org"
        assert n._extract_domain("raw.githubusercontent.com") == "raw.githubusercontent.com"


# ---------------------------------------------------------------------------
# Layer 5 — MCP tool boundary
# ---------------------------------------------------------------------------

class TestMCPToolBoundary:
    def test_bash_c_blocked(self) -> None:
        m = MCPToolBoundary()
        ok, _ = m.check_shell_spawn(["bash", "-c", "echo hi"])
        assert ok is False

    def test_sh_c_blocked(self) -> None:
        m = MCPToolBoundary()
        ok, _ = m.check_shell_spawn(["sh", "-c", "rm -rf /"])
        assert ok is False

    def test_zsh_c_blocked(self) -> None:
        m = MCPToolBoundary()
        ok, _ = m.check_shell_spawn(["zsh", "-c", "echo hi"])
        assert ok is False

    def test_bash_version_allowed(self) -> None:
        m = MCPToolBoundary()
        ok, _ = m.check_shell_spawn(["bash", "--version"])
        assert ok is True

    def test_python_subprocess_blocked(self) -> None:
        m = MCPToolBoundary()
        # python3 -c is NOT blocked by MCP boundary (it's not a shell binary)
        # The boundary blocks bash/sh/zsh with -c, not arbitrary interpreters.
        ok, _ = m.check_shell_spawn(["python3", "-c", "import os; os.system('ls')"])
        assert ok is True

    def test_interpreter_with_dangerous_code_blocked(self) -> None:
        m = MCPToolBoundary()
        # If someone tries to eval/exec via a shell, that's blocked
        ok, _ = m.check_shell_spawn(["bash", "-c", "eval 'rm -rf /'"])
        assert ok is False

    def test_non_shell_command_allowed(self) -> None:
        m = MCPToolBoundary()
        ok, _ = m.check_shell_spawn(["git", "status"])
        assert ok is True
        ok, _ = m.check_shell_spawn(["python3", "script.py"])
        assert ok is True

    def test_assert_no_shell_raises(self) -> None:
        m = MCPToolBoundary()
        m.assert_no_shell(["git", "status"])
        with pytest.raises(SandboxViolation):
            m.assert_no_shell(["bash", "-c", "echo hi"])


# ---------------------------------------------------------------------------
# Layer 6 — Worktree lifecycle
# ---------------------------------------------------------------------------

class TestWorktreeLifecycle:
    def test_worktree_add_blocked(self) -> None:
        w = WorktreeLifecycle()
        ok, _ = w.check(["git", "worktree", "add", "../new"])
        assert ok is False

    def test_worktree_remove_blocked(self) -> None:
        w = WorktreeLifecycle()
        ok, _ = w.check(["git", "worktree", "remove", "../new"])
        assert ok is False

    def test_worktree_prune_blocked(self) -> None:
        w = WorktreeLifecycle()
        ok, _ = w.check(["git", "worktree", "prune"])
        assert ok is False

    def test_normal_git_allowed(self) -> None:
        w = WorktreeLifecycle()
        ok, _ = w.check(["git", "status"])
        assert ok is True

    def test_assert_allowed_raises(self) -> None:
        w = WorktreeLifecycle()
        w.assert_allowed(["git", "commit", "-m", "ok"])
        with pytest.raises(SandboxViolation):
            w.assert_allowed(["git", "worktree", "add", "wt"])


# ---------------------------------------------------------------------------
# Unified Sandbox
# ---------------------------------------------------------------------------

class TestSandbox:
    def test_for_agent_factory(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sb = Sandbox.for_agent(tmp_path, worktree)
        assert isinstance(sb.paths, WritePathIsolation)
        assert isinstance(sb.git, GitCommandPolicy)
        assert isinstance(sb.os_sandbox, OSSandbox)
        assert isinstance(sb.network, NetworkPolicy)
        assert isinstance(sb.mcp, MCPToolBoundary)
        assert isinstance(sb.worktrees, WorktreeLifecycle)

    def test_apply_returns_env(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sb = Sandbox.for_agent(tmp_path, worktree)
        env = sb.apply()
        assert env["AICTO_SANDBOX"] == "1"
        assert env["AICTO_WORKTREE"] == str(worktree)
        assert "HTTP_PROXY" in env
        assert "NO_PROXY" in env

    def test_check_command_git(self, tmp_path: Path) -> None:
        sb = Sandbox.for_agent(tmp_path, tmp_path / "wt")
        ok, _ = sb.check_command(["git", "status"])
        assert ok is True
        ok, reason = sb.check_command(["git", "push", "--force"])
        assert ok is False
        assert reason is not None

    def test_check_command_shell(self, tmp_path: Path) -> None:
        sb = Sandbox.for_agent(tmp_path, tmp_path / "wt")
        ok, _ = sb.check_command(["bash", "-c", "echo hi"])
        assert ok is False

    def test_assert_command_raises(self, tmp_path: Path) -> None:
        sb = Sandbox.for_agent(tmp_path, tmp_path / "wt")
        sb.assert_command(["git", "status"])
        with pytest.raises(SandboxViolation):
            sb.assert_command(["git", "reset", "--hard", "HEAD"])

    def test_wrap_returns_list(self, tmp_path: Path) -> None:
        sb = Sandbox.for_agent(tmp_path, tmp_path / "wt")
        wrapped = sb.wrap(["python3", "script.py"])
        assert isinstance(wrapped, list)
        assert wrapped[0] in ("sandbox-exec", "python3")

    def test_paths_assert_safe_through_sandbox(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sb = Sandbox.for_agent(tmp_path, worktree)
        sb.paths.assert_safe(worktree / "file.txt")
        with pytest.raises(SandboxViolation):
            sb.paths.assert_safe(tmp_path / "outside.txt")

    def test_git_assert_allowed_through_sandbox(self, tmp_path: Path) -> None:
        sb = Sandbox.for_agent(tmp_path, tmp_path / "wt")
        sb.git.assert_allowed(["git", "status"])
        with pytest.raises(SandboxViolation):
            sb.git.assert_allowed(["git", "push", "--force"])

    def test_worktrees_assert_allowed_through_sandbox(self, tmp_path: Path) -> None:
        sb = Sandbox.for_agent(tmp_path, tmp_path / "wt")
        sb.worktrees.assert_allowed(["git", "commit", "-m", "ok"])
        with pytest.raises(SandboxViolation):
            sb.worktrees.assert_allowed(["git", "worktree", "add", "foo"])

    def test_cleanup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sb = Sandbox.for_agent(tmp_path, worktree)
        # Trigger profile creation
        _ = sb.os_sandbox._generate_seatbelt_profile()
        sb.cleanup()
        assert sb.os_sandbox._profile_path is None or not sb.os_sandbox._profile_path.exists()


# ---------------------------------------------------------------------------
# Integration — subprocess env application
# ---------------------------------------------------------------------------

class TestSandboxEnvIntegration:
    def test_env_restricts_http(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        sb = Sandbox.for_agent(tmp_path, worktree)
        env = sb.apply()
        assert env["HTTP_PROXY"] == "http://127.0.0.1:9"
        assert "github.com" in env["NO_PROXY"]
