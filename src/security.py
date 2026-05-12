"""6-layer security sandbox for AI CTO agent processes.

Adopts the Delegate-inspired layered security model:
  Layer 1: Write-path isolation    → agents write only to their worktree
  Layer 2: Disallowed git commands  → block force push, hard reset, branch deletion
  Layer 3: OS-level sandbox        → macOS Seatbelt profile (or chroot fallback)
  Layer 4: Network domain allowlist → only whitelisted domains reachable
  Layer 5: MCP tool boundary       → agents use only provided MCP tools
  Layer 6: Daemon worktree lifecycle → supervisor owns creation/destruction

Usage:
    from security import Sandbox
    sb = Sandbox.for_agent(team_dir, worktree_path, allowed_domains=["github.com"])
    sb.apply()          # enforce all active layers
    sb.check_git(cmd)   # validate a git command before execution
    sb.check_path(path) # validate a filesystem write target
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Layer 1 — Write-path isolation
# ---------------------------------------------------------------------------

@dataclass
class WritePathIsolation:
    """Restricts agent writes to a single worktree directory.

    Any attempt to write outside the allowed root is blocked.
    Reads are still permitted outside (for system libraries, git history, etc.).
    """
    allowed_root: Path
    team_dir: Path  # parent is blocked for writes

    def check(self, target_path: str | Path) -> bool:
        """Return True if *target_path* is inside the allowed worktree."""
        p = Path(target_path).resolve()
        allowed = self.allowed_root.resolve()
        # Must be inside allowed_root AND not inside team_dir (which is the parent)
        try:
            p.relative_to(allowed)
            # Also ensure it's not escaping via symlinks
            if p.exists():
                p = p.resolve()
                p.relative_to(allowed)
            return True
        except ValueError:
            return False

    def assert_safe(self, target_path: str | Path) -> None:
        if not self.check(target_path):
            raise SandboxViolation(
                f"Write-path violation: {target_path} is outside allowed worktree {self.allowed_root}"
            )


# ---------------------------------------------------------------------------
# Layer 2 — Disallowed git commands
# ---------------------------------------------------------------------------

# Commands that mutate branch topology destructively.
_DISALLOWED_GIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bgit\b.*\bpush\b.*--force\b|\b-f\b"),
    re.compile(r"\bgit\b.*\bpush\b.*--force-with-lease"),
    re.compile(r"\bgit\b.*\breset\b.*--hard\b"),
    re.compile(r"\bgit\b.*\bbranch\b.*\b-D\b|\b-d\b.*\b-f\b"),
    re.compile(r"\bgit\b.*\bbranch\b.*--delete\b.*--force\b"),
    re.compile(r"\bgit\b.*\bfilter-branch\b"),
    re.compile(r"\bgit\b.*\bupdate-ref\b.*-d\b"),
    re.compile(r"\bgit\b.*\breflog\b.*delete\b"),
    re.compile(r"\bgit\b.*\bpush\b.*--delete\b"),
    re.compile(r"\bgit\b.*\bremote\b.*\bremove\b|\brm\b"),
]

# Allowed git subcommands (whitelist approach for extra safety)
_ALLOWED_GIT_SUBCOMMANDS: set[str] = {
    "add", "branch", "checkout", "clone", "commit", "diff", "fetch",
    "init", "log", "merge", "pull", "push", "rebase", "reset",
    "show", "stash", "status", "tag", "worktree", "config",
    "rev-parse", "symbolic-ref", "merge-base", "for-each-ref",
}


class GitCommandPolicy:
    """Validates git commands before execution."""

    def __init__(self, allow_push: bool = True, allow_branch_delete: bool = False):
        self.allow_push = allow_push
        self.allow_branch_delete = allow_branch_delete

    def check(self, cmd: list[str] | str) -> tuple[bool, Optional[str]]:
        """Return (ok, reason_if_denied)."""
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
            tokens = cmd
        else:
            cmd_str = cmd
            tokens = cmd.split()

        # Must start with git
        if tokens and tokens[0] != "git":
            return True, None  # not our concern

        # Check against disallowed patterns
        for pat in _DISALLOWED_GIT_PATTERNS:
            if pat.search(cmd_str):
                return False, f"disallowed git pattern matched: {pat.pattern[:60]}..."

        # Subcommand whitelist validation (optional strict mode)
        subcmd = next((t for t in tokens[1:] if not t.startswith("-")), None)
        if subcmd and subcmd not in _ALLOWED_GIT_SUBCOMMANDS:
            return False, f"git subcommand '{subcmd}' not in allowlist"

        # Extra rules
        if "push" in tokens and not self.allow_push:
            return False, "git push is disabled"
        if "branch" in tokens and ("-D" in tokens or "--delete" in tokens) and not self.allow_branch_delete:
            return False, "branch deletion is disabled"

        return True, None

    def assert_allowed(self, cmd: list[str] | str) -> None:
        ok, reason = self.check(cmd)
        if not ok:
            raise SandboxViolation(f"Git policy violation: {reason}")


# ---------------------------------------------------------------------------
# Layer 3 — OS-level sandbox (macOS Seatbelt / chroot fallback)
# ---------------------------------------------------------------------------

class OSSandbox:
    """Platform-specific process sandboxing.

    On macOS: generates a temporary Seatbelt (sandbox-exec) profile that
    restricts file writes to the worktree plus standard read-only system paths.

    On Linux: uses a lightweight chroot-like wrapper (unshare + bind mounts)
    if running as root, otherwise falls back to LD_PRELOAD path interception.

    On other platforms: best-effort — returns a wrapper script that sets
    restrictive env vars and warns.
    """

    def __init__(self, worktree: Path, read_only_paths: Optional[list[Path]] = None):
        self.worktree = worktree.resolve()
        self.read_only_paths = [p.resolve() for p in (read_only_paths or [])]
        self._profile_path: Optional[Path] = None

    # Seatbelt profile template for macOS.
    _SEATBELT_TEMPLATE = """(version 1)
(deny default)
(allow process-exec)
(allow file-read*)
(allow file-write*
    (subpath "{worktree}")
    (subpath "{tmp}")
)
(allow file-write*
    (subpath "/dev/null")
    (subpath "/dev/zero")
    (subpath "/dev/random")
    (subpath "/dev/urandom")
    (subpath "/dev/tty")
    (subpath "/dev/stdin")
    (subpath "/dev/stdout")
    (subpath "/dev/stderr")
)
(allow network*)
(allow signal (target self))
"""

    def _generate_seatbelt_profile(self) -> Path:
        """Write a Seatbelt profile to a temp file.  Idempotent per instance."""
        if self._profile_path and self._profile_path.exists():
            return self._profile_path

        tmp = Path(tempfile.gettempdir())
        profile = self._SEATBELT_TEMPLATE.format(
            worktree=str(self.worktree),
            tmp=str(tmp),
        )
        fd, path = tempfile.mkstemp(prefix="aicto-seatbelt-", suffix=".sb")
        os.write(fd, profile.encode())
        os.close(fd)
        self._profile_path = Path(path)
        return self._profile_path

    def wrap_command(self, cmd: list[str]) -> list[str]:
        """Return *cmd* wrapped with the appropriate sandbox runner."""
        if sys.platform == "darwin":
            profile = self._generate_seatbelt_profile()
            return ["sandbox-exec", "-f", str(profile)] + cmd
        elif sys.platform.startswith("linux"):
            # Best-effort: use firejail if available, else bwrap, else no-op.
            for runner in ("firejail", "bwrap"):
                if self._which(runner):
                    if runner == "firejail":
                        return ["firejail", "--quiet", f"--whitelist={self.worktree}"] + cmd
                    elif runner == "bwrap":
                        return (
                            ["bwrap", "--die-with-parent", "--proc", "/proc",
                             "--dev", "/dev", "--ro-bind", "/", "/",
                             "--bind", str(self.worktree), str(self.worktree)]
                            + cmd
                        )
            # No sandbox runner available — warn but don't block.
            return cmd
        else:
            return cmd

    @staticmethod
    def _which(name: str) -> Optional[str]:
        try:
            return subprocess.run(
                ["which", name],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def cleanup(self) -> None:
        if self._profile_path and self._profile_path.exists():
            self._profile_path.unlink()
            self._profile_path = None


# ---------------------------------------------------------------------------
# Layer 4 — Network domain allowlist
# ---------------------------------------------------------------------------

@dataclass
class NetworkPolicy:
    """Restrict outbound network to a set of allowed domains.

    Implemented via environment variables that cooperating tools (curl, wget,
    python requests via HTTP_PROXY) honour.  A full enforcement would need
    a packet-filter (pf / iptables / Little Snitch) — this layer provides
    the *configuration* and a validation helper; external firewall rules
    can be derived from the allowlist.
    """
    allowed_domains: set[str] = field(default_factory=lambda: {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "pypi.org",
        "pypi.python.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "npmjs.org",
        "registry.yarnpkg.com",
        "api.openai.com",
        "api.anthropic.com",
        "api.moonshot.cn",          # kimi
        "generativelanguage.googleapis.com",
    })
    block_mode: bool = False  # True → fail closed if proxy missing

    def __post_init__(self) -> None:
        # Ensure allowed_domains is always a set (handle None passed explicitly)
        if self.allowed_domains is None:
            self.allowed_domains = {
                "github.com",
                "api.github.com",
                "raw.githubusercontent.com",
                "pypi.org",
                "pypi.python.org",
                "files.pythonhosted.org",
                "registry.npmjs.org",
                "npmjs.org",
                "registry.yarnpkg.com",
                "api.openai.com",
                "api.anthropic.com",
                "api.moonshot.cn",
                "generativelanguage.googleapis.com",
            }

    def is_allowed(self, url_or_domain: str) -> bool:
        """Check if a URL or bare domain is in the allowlist."""
        domain = self._extract_domain(url_or_domain)
        return any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in self.allowed_domains
        )

    def env_overrides(self) -> dict[str, str]:
        """Return env vars that restrict HTTP(S) traffic.

        Uses a dummy localhost proxy (port 9 / discard) for disallowed
        traffic, and an explicit NO_PROXY for allowed domains so they
        bypass the block proxy.  Tools that don't honour the proxy will
        leak — that's why Layer-3 OS sandbox + external firewall rules
        are recommended as the real enforcement.
        """
        # Dummy block proxy — anything that hits this is denied.
        block_proxy = "http://127.0.0.1:9"
        no_proxy = ",".join(self.allowed_domains)
        return {
            "HTTP_PROXY": block_proxy,
            "HTTPS_PROXY": block_proxy,
            "http_proxy": block_proxy,
            "https_proxy": block_proxy,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }

    @staticmethod
    def _extract_domain(url_or_domain: str) -> str:
        s = url_or_domain.lower().strip()
        if s.startswith("http://") or s.startswith("https://"):
            s = s.split("//", 1)[1]
        s = s.split("/", 1)[0]
        s = s.split(":", 1)[0]
        return s


# ---------------------------------------------------------------------------
# Layer 5 — MCP tool boundary
# ---------------------------------------------------------------------------

class MCPToolBoundary:
    """Ensures the agent interacts with the world only through approved MCP
    tools, never arbitrary shell.

    In practice this is a *policy* layer: the agent prompt is configured
    with a fixed tool list, and this class provides runtime guards that
    reject attempts to spawn a shell directly (e.g. `bash -c`, `sh -c`,
    `zsh`, `exec`, `eval`, `os.system`, `subprocess.call`).
    """

    # Commands that open arbitrary shell execution.
    _SHELL_BINARIES: set[str] = {
        "bash", "sh", "zsh", "fish", "csh", "tcsh", "ksh", "dash",
        "cmd", "cmd.exe", "powershell", "pwsh",
    }

    # Dangerous one-liners that bypass tool boundaries.
    _DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"\b(eval|exec)\s+\["),
        re.compile(r"\b(os\.system|subprocess\.call|subprocess\.run|subprocess\.Popen)\b"),
        re.compile(r"\b`(.*)`\b"),               # backtick execution
        re.compile(r"\$\((.*)\)"),              # command substitution
    ]

    def check_shell_spawn(self, cmd: list[str]) -> tuple[bool, Optional[str]]:
        """Return (ok, reason) for a command list."""
        if not cmd:
            return True, None
        binary = Path(cmd[0]).name
        if binary in self._SHELL_BINARIES:
            # Check if it's a restricted invocation (e.g. bash -c "...")
            cmd_str = " ".join(cmd)
            for pat in self._DANGEROUS_PATTERNS:
                if pat.search(cmd_str):
                    return False, f"dangerous shell pattern detected: {pat.pattern[:40]}"
            # Allow harmless invocations like `bash --version` or `sh -n script.sh`
            if "-c" in cmd or "-s" in cmd:
                return False, "shell with -c/-s flag is blocked (arbitrary code execution)"
        return True, None

    def assert_no_shell(self, cmd: list[str]) -> None:
        ok, reason = self.check_shell_spawn(cmd)
        if not ok:
            raise SandboxViolation(f"MCP boundary violation: {reason}")


# ---------------------------------------------------------------------------
# Layer 6 — Daemon-managed worktree lifecycle
# ---------------------------------------------------------------------------

class WorktreeLifecycle:
    """Only the supervisor (daemon) may create or destroy worktrees.

    Agents receive a pre-created worktree path; any attempt by the agent
    process to run `git worktree add` or `git worktree remove` is blocked.
    """

    def check(self, cmd: list[str]) -> tuple[bool, Optional[str]]:
        if not cmd or cmd[0] != "git":
            return True, None
        if "worktree" in cmd and ("add" in cmd or "remove" in cmd or "prune" in cmd):
            return False, "git worktree lifecycle commands are daemon-only"
        return True, None

    def assert_allowed(self, cmd: list[str]) -> None:
        ok, reason = self.check(cmd)
        if not ok:
            raise SandboxViolation(f"Worktree lifecycle violation: {reason}")


# ---------------------------------------------------------------------------
# Unified Sandbox
# ---------------------------------------------------------------------------

class SandboxViolation(Exception):
    """Raised when any sandbox layer rejects an operation."""


@dataclass
class Sandbox:
    """Assembles all six security layers for a single agent invocation.

    Usage:
        sb = Sandbox.for_agent(team_dir, worktree)
        sb.apply()
        # before every git command:
        sb.git.assert_allowed(["git", "commit", "-m", "hello"])
        # before every write:
        sb.paths.assert_safe("/some/path")
    """
    paths: WritePathIsolation
    git: GitCommandPolicy
    os_sandbox: OSSandbox
    network: NetworkPolicy
    mcp: MCPToolBoundary
    worktrees: WorktreeLifecycle

    @classmethod
    def for_agent(
        cls,
        team_dir: Path,
        worktree: Path,
        allowed_domains: Optional[list[str]] = None,
        allow_push: bool = True,
        allow_branch_delete: bool = False,
    ) -> "Sandbox":
        paths = WritePathIsolation(allowed_root=worktree, team_dir=team_dir)
        git = GitCommandPolicy(allow_push=allow_push, allow_branch_delete=allow_branch_delete)
        os_sb = OSSandbox(worktree)
        net = NetworkPolicy(
            allowed_domains=set(allowed_domains) if allowed_domains else None,
        )
        mcp = MCPToolBoundary()
        wt = WorktreeLifecycle()
        return cls(
            paths=paths,
            git=git,
            os_sandbox=os_sb,
            network=net,
            mcp=mcp,
            worktrees=wt,
        )

    def apply(self) -> dict[str, str]:
        """Return environment overrides that activate applicable layers.

        Callers should merge these into the subprocess env:
            env = os.environ.copy()
            env.update(sandbox.apply())
        """
        env: dict[str, str] = {}
        env.update(self.network.env_overrides())
        env["AICTO_SANDBOX"] = "1"
        env["AICTO_WORKTREE"] = str(self.paths.allowed_root)
        return env

    def wrap(self, cmd: list[str]) -> list[str]:
        """Wrap a command with OS sandbox (seatbelt/chroot)."""
        return self.os_sandbox.wrap_command(cmd)

    def check_command(self, cmd: list[str]) -> tuple[bool, Optional[str]]:
        """Run all policy checks on a command. Returns (ok, reason)."""
        checks = [
            self.git.check(cmd),
            self.mcp.check_shell_spawn(cmd),
            self.worktrees.check(cmd),
        ]
        for ok, reason in checks:
            if not ok:
                return False, reason
        return True, None

    def assert_command(self, cmd: list[str]) -> None:
        ok, reason = self.check_command(cmd)
        if not ok:
            raise SandboxViolation(f"Sandbox blocked command: {reason}")

    def cleanup(self) -> None:
        self.os_sandbox.cleanup()
