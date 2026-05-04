#!/usr/bin/env -S uv run --quiet --with mcp python
"""MCP server that exposes the `bin/cto` CLI as tools for the CTO Claude
Code session. Tools are thin wrappers over the CLI; see ../bin/cto for the
canonical behaviour. Team sessions intentionally do not load this server
(they run from a different project root, so this .mcp.json is out of
scope)."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
CTO = ROOT / "bin" / "cto"

mcp = FastMCP("cto")


def _run(args: list[str], stdin: str | None = None) -> str:
    """Run `bin/cto …` and return combined stdout+stderr. Raises on failure."""
    proc = subprocess.run(
        [str(CTO), *args],
        cwd=str(ROOT),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout
    if proc.stderr:
        out = (out + ("\n" if out and not out.endswith("\n") else "") + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cto {' '.join(shlex.quote(a) for a in args)} exited {proc.returncode}\n{out}"
        )
    return out.strip() or "(no output)"


# ---- team management ------------------------------------------------------

@mcp.tool()
def team_create(
    name: str,
    repo: Optional[str] = None,
    developers: int = 2,
    reviewers: int = 1,
    container_use: bool = False,
    agent_provider: str = "claude",
    description: Optional[str] = None,
) -> str:
    """Create a new team workspace under teams/<name>/.

    Scaffolds a git repo, runs `bd init`, writes .cto/config.yaml with the
    requested agent counts, and renders role prompts. Use repo=<url> to
    clone an existing repo instead of `git init`."""
    args = ["team", "create", name, "--developers", str(developers), "--reviewers", str(reviewers), "--agent-provider", agent_provider]
    if repo:
        args += ["--repo", repo]
    if container_use:
        args += ["--container-use"]
    if description:
        args += ["--description", description]
    return _run(args)


@mcp.tool()
def team_adopt(
    name: str,
    path: str,
    copy: bool = False,
    developers: int = 2,
    reviewers: int = 1,
    container_use: bool = False,
    agent_provider: str = "claude",
) -> str:
    """Adopt an existing local directory (with or without .git) as a team.

    Moves `path` into teams/<name>/ by default; pass copy=True to copy
    instead. Works on bare directories (runs `git init`) and on existing
    git repos (preserves history). Layers the cto scaffolding on top:
    bd init, .cto/config.yaml, role prompts, breakdowns/plans/docs dirs,
    and a templated CLAUDE.md (the original CLAUDE.md, if any, is
    preserved at .cto/orig-CLAUDE.md).

    Use this when the source has no git URL — for a remote URL, use
    team_create(repo=...) instead."""
    args = ["team", "adopt", name, path,
            "--developers", str(developers),
            "--reviewers", str(reviewers),
            "--agent-provider", agent_provider]
    if copy:
        args.append("--copy")
    if container_use:
        args.append("--container-use")
    return _run(args)


@mcp.tool()
def team_list() -> str:
    """List all teams with their agent counts, container-use mode, tmux
    state, and open bd issue counts."""
    return _run(["team", "list"])


@mcp.tool()
def team_remove(name: str, confirm: bool = False) -> str:
    """Permanently delete a team. Requires confirm=True since this rm -rf's
    the team's repo and bd database."""
    if not confirm:
        return f"refusing to remove '{name}' without confirm=True"
    proc = subprocess.run(
        [str(CTO), "team", "remove", name],
        cwd=str(ROOT),
        input="y\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (proc.stdout + proc.stderr).strip() or "(removed)"


@mcp.tool()
def config(
    name: str,
    developers: Optional[int] = None,
    reviewers: Optional[int] = None,
    container_use: Optional[bool] = None,
    permission_mode: Optional[str] = None,
    model: Optional[str] = None,
    manager_model: Optional[str] = None,
    agent_provider: Optional[str] = None,
) -> str:
    """Edit teams/<name>/.cto/config.yaml. Any unspecified field is left
    unchanged. After config changes, run restart() to apply them.

    `model` controls developer + reviewer windows.
    `manager_model` controls the manager window.
    `agent_provider` is claude, kimi, or codex."""
    args = ["config", name]
    if developers is not None:
        args += ["--developers", str(developers)]
    if reviewers is not None:
        args += ["--reviewers", str(reviewers)]
    if container_use is not None:
        args += ["--container-use", "on" if container_use else "off"]
    if permission_mode is not None:
        args += ["--permission-mode", permission_mode]
    if model is not None:
        args += ["--model", model]
    if manager_model is not None:
        args += ["--manager-model", manager_model]
    if agent_provider is not None:
        args += ["--agent-provider", agent_provider]
    return _run(args)


# ---- task / approval flow ------------------------------------------------

@mcp.tool()
def task(
    team: str,
    title: str,
    description: Optional[str] = None,
    priority: int = 2,
    kind: str = "epic",
    ops: bool = False,
) -> str:
    """File a bd task on a team. Defaults to kind='epic' (the only kind the
    CTO normally files — the manager decomposes from there). Other kinds
    ('dev', 'plan') exist as escape hatches but bypass the breakdown gate.

    Set ops=True for one-shot ops epics (git pull, run a sync script, etc.)
    that don't produce a diff. The reconciler files a single kind:dev for
    them and closes the epic when the dev closes — no breakdown, plan,
    review, or merge ceremony. ops=True only applies to kind='epic'.
    """
    if kind not in ("epic", "dev", "plan"):
        raise ValueError(f"kind must be epic|dev|plan, got {kind!r}")
    if ops and kind != "epic":
        raise ValueError("ops=True only applies to kind='epic'")
    args = ["task", team, title, "-p", str(priority), f"--{kind}"]
    if ops:
        args.append("--ops")
    if description:
        args += ["-d", description]
    return _run(args)


@mcp.tool()
def merge(
    team: str,
    branch: str,
    target: Optional[str] = None,
    epic: Optional[str] = None,
) -> str:
    """File a kind:merge role:manager bd issue for `branch`. The manager
    will execute it (merging into epic/<epic-id> when `epic` is given,
    else into `main` for legacy in-flight epics). Use as a manual escape
    hatch — `cto approve` already auto-files sub-merge issues.

    `target` is breakdown|plan|code (added as a label so the manager
    knows what's being merged)."""
    args = ["merge", team, branch]
    if target:
        args += ["--target", target]
    if epic:
        args += ["--epic", epic]
    return _run(args)


@mcp.tool()
def merge_epic(team: str, epic_id: str, comment: Optional[str] = None) -> str:
    """CTO-only: merge `epic/<epic-id>` into the target branch from the
    team's main worktree. The merge target is determined by the
    `parent_branch` value stored in the epic's description (defaulting to
    `main` when not set). Closes the open `kind:merge target:epic` issue,
    closes the epic, and prunes the epic worktree + branch. Idempotent.

    Surfaces in `cto inbox` as a `role:cto kind:merge target:epic` issue
    that the manager files when the epic's sub-branches are all merged
    into the feature branch and tests pass. Equivalent to running
    `cto approve` on that inbox item — both paths execute the merge."""
    args = ["merge-epic", team, epic_id]
    if comment:
        args += ["--comment", comment]
    return _run(args)


@mcp.tool()
def promote(team: str, issue_ids: list[str], kind: str = "epic") -> str:
    """Retrofit existing bd issues with role:manager + kind:<kind> labels
    so the team's manager agent picks them up. Use after adopting a repo
    whose pre-existing issues don't yet carry the cto workflow labels.

    kind defaults to 'epic' (the manager will then decompose them); use
    'dev' or 'plan' only as escape hatches that bypass the breakdown
    gate."""
    if kind not in ("epic", "dev", "plan"):
        raise ValueError(f"kind must be epic|dev|plan, got {kind!r}")
    if not issue_ids:
        raise ValueError("issue_ids must contain at least one id")
    return _run(["promote", team, f"--{kind}", *issue_ids])


@mcp.tool()
def inbox(team: Optional[str] = None) -> str:
    """List open bd issues with role:cto across all teams (or a single
    team). This is what's awaiting human-CTO approval — typically
    breakdown approvals and plan approvals."""
    args = ["inbox"]
    if team:
        args.append(team)
    return _run(args)


@mcp.tool()
def approve(team: str, issue_id: str, comment: Optional[str] = None) -> str:
    """Close a role:cto approval issue (breakdown or plan). The team's
    workflow then advances: a merge issue is filed, the manager merges,
    and downstream work is unblocked."""
    args = ["approve", team, issue_id]
    if comment:
        args += ["--comment", comment]
    return _run(args)


@mcp.tool()
def reject(team: str, issue_id: str, comment: str) -> str:
    """Reject a role:cto approval. Closes the approval as rejected and
    reopens the upstream artifact (breakdown or plan) with the comment so
    the responsible agent can address feedback. `comment` is required."""
    return _run(["reject", team, issue_id, "--comment", comment])


# ---- runtime control -----------------------------------------------------

@mcp.tool()
def start(name: str, dangerous: bool = False) -> str:
    """Bring a team online: open a tmux session 'cto-<name>' with one
    window per agent (manager, dev-1..N, review-1..M). Each window runs
    the team's configured agent CLI interactively with the role prompt
    as system context.

    dangerous=True upgrades --permission-mode to bypassPermissions for
    claude teams (fully autonomous; only use in trusted workspaces)."""
    args = ["start", name]
    if dangerous:
        args.append("--dangerous")
    return _run(args)


@mcp.tool()
def stop(name: str) -> str:
    """Gracefully stop a team: send Ctrl-C to each tmux window so agents
    can flush bd state, then kill the session."""
    return _run(["stop", name])


@mcp.tool()
def restart(name: str) -> str:
    """Stop then start. Use after config() changes to apply them."""
    return _run(["restart", name])


# ---- observation ---------------------------------------------------------

@mcp.tool()
def status(team: Optional[str] = None) -> str:
    """Show overall status. With no team, lists all teams + the CTO inbox.
    With a team, shows full bd state, recent status digests, and tmux
    window list."""
    args = ["status"]
    if team:
        args.append(team)
    return _run(args)


@mcp.tool()
def update(name: str, fresh: bool = False) -> str:
    """Print the team manager's latest status digest from bd. fresh=True
    files a kind:status-request issue and waits up to 60s for the manager
    to write a fresh digest in response."""
    args = ["update", name]
    if fresh:
        args.append("--fresh")
    return _run(args)


@mcp.tool()
def worktrees(name: str) -> str:
    """List active git worktrees for a team. Per-task worktrees live under
    teams/<name>/.cto/worktrees/<id>."""
    return _run(["worktrees", name])


@mcp.tool()
def worktrees_prune(name: str) -> str:
    """Garbage-collect worktrees whose branch is fully merged into `main`
    OR into any open `epic/*` feature branch (so completed sub-branches
    of in-flight epics are reclaimed without waiting for the epic to
    ship)."""
    return _run(["worktrees", "prune", name])


# ---- artifact access -----------------------------------------------------

@mcp.tool()
def read_artifact(team: str, path: str) -> str:
    """Read a file from a team's repo. Use this for breakdowns, plans, and
    review notes — bd issue gists point at file paths and you read them
    here. Refuses paths outside the team workspace."""
    base = (ROOT / "teams" / team).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base) + os.sep) and target != base:
        raise ValueError(f"path escapes team workspace: {path}")
    if not target.exists():
        raise FileNotFoundError(f"no such file: teams/{team}/{path}")
    if target.is_dir():
        return "\n".join(sorted(p.name for p in target.iterdir()))
    text = target.read_text(errors="replace")
    if len(text) > 100_000:
        text = text[:100_000] + f"\n…(truncated; full size {len(text)} bytes)"
    return text


if __name__ == "__main__":
    mcp.run()
