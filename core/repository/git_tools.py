"""
core/repository/git_tools.py — Read-only Git operations for workspace repositories.

Provides git_status, git_diff, git_log, and git_branch using safe argument arrays.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from core.repository.diagnostics import run_safe_command
from core.repository.workspace import WorkspaceManager, get_workspace


def _check_git(workspace: Optional[WorkspaceManager] = None) -> tuple[bool, str, Optional[Path]]:
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return False, "No active repository root selected.", None
    if not ws.is_git:
        return False, f"Directory '{root.name}' is not a Git repository.", root
    if not shutil.which("git"):
        return False, "The 'git' executable was not found on PATH.", root
    return True, "", root


def git_status(workspace: Optional[WorkspaceManager] = None) -> dict:
    """Run git status --porcelain -b on the active repository."""
    ok, err, root = _check_git(workspace)
    if not ok:
        return {"error": err}
    return run_safe_command(["git", "status", "--porcelain=v1", "-b"], cwd=root)


def git_diff(
    staged: bool = False,
    file_path: Optional[str] = None,
    commit: Optional[str] = None,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Run git diff with optional staged/commit/file parameters."""
    ok, err, root = _check_git(workspace)
    if not ok:
        return {"error": err}

    cmd = ["git", "diff"]
    if staged:
        cmd.append("--cached")
    if commit:
        cmd.append(commit)
    if file_path:
        cmd.extend(["--", file_path])

    return run_safe_command(cmd, cwd=root)


def git_log(
    limit: int = 15,
    file_path: Optional[str] = None,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Run git log showing recent commits."""
    ok, err, root = _check_git(workspace)
    if not ok:
        return {"error": err}

    cmd = ["git", "log", f"-{max(1, min(100, int(limit)))}", "--oneline", "--decorate"]
    if file_path:
        cmd.extend(["--", file_path])

    return run_safe_command(cmd, cwd=root)


def git_branch(workspace: Optional[WorkspaceManager] = None) -> dict:
    """Run git branch listing branches."""
    ok, err, root = _check_git(workspace)
    if not ok:
        return {"error": err}
    return run_safe_command(["git", "branch", "-a"], cwd=root)


def git_commit(
    message: str,
    add_all: bool = True,
    explicit_user_request: bool = True,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Commit changes to the active Git repository on explicit user instruction."""
    ok, err, root = _check_git(workspace)
    if not ok:
        return {"error": err}
    if not message:
        return {"error": "Commit message is required."}

    if add_all:
        add_res = run_safe_command(["git", "add", "-A"], cwd=root, explicit_user_request=explicit_user_request)
        if add_res.get("status") != "success":
            return {"error": f"git add failed: {add_res.get('stderr') or add_res.get('stdout')}"}

    commit_res = run_safe_command(
        ["git", "commit", "-m", str(message).strip()],
        cwd=root,
        explicit_user_request=explicit_user_request,
    )
    return commit_res


def git_push(
    remote: str = "origin",
    branch: Optional[str] = None,
    explicit_user_request: bool = True,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Push commits to remote (e.g. GitHub) on explicit user instruction."""
    ok, err, root = _check_git(workspace)
    if not ok:
        return {"error": err}

    cmd = ["git", "push", remote or "origin"]
    if branch:
        cmd.append(branch)

    return run_safe_command(cmd, cwd=root, explicit_user_request=explicit_user_request)

