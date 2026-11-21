"""
core/repository/diagnostics.py — Safe command runner and project validation subsystem.

Executes tests, linters, type checkers, formatters, and builds safely with
process timeouts, bounded output, argument array enforcement, and destructive
command prevention.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import confirm as confirm_gate
from core.repository.index import RepositoryIndex
from core.repository.workspace import (
    WorkspaceError,
    WorkspaceManager,
    detect_hardcoded_secrets,
    get_workspace,
)
from memory import config_manager


_DESTRUCTIVE_COMMAND_PATTERNS = [
    re.compile(r"\b(rm|rmdir|del)\b.*(/s|\\s|-r|-rf|\*)", re.IGNORECASE),
    re.compile(r"\b(format|fdisk|mkfs|diskpart)\b", re.IGNORECASE),
    re.compile(r"\b(shutdown|reboot|init\s+0)\b", re.IGNORECASE),
]

_RESTRICTED_COMMAND_PATTERNS = [
    re.compile(r"\bgit\s+(commit|push|checkout|reset|clean|rebase|merge|branch\s+-[dDmM])\b", re.IGNORECASE),
    re.compile(r"\b(curl|wget|nc|ncat|netcat|invoke-webrequest)\b", re.IGNORECASE),
    re.compile(r"\b(pip\s+install|npm\s+install|yarn\s+add|cargo\s+build|mvn|gradle)\b", re.IGNORECASE),
]


class CommandExecutionError(WorkspaceError):
    pass


class BlockedCommandError(WorkspaceError):
    pass


def run_safe_command(
    args: list[str],
    timeout: int = 60,
    max_output_chars: int = 50_000,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    auto_approve: bool = False,
    explicit_user_request: bool = False,
) -> dict:
    """
    Execute a command using an argument array with the repository root as cwd.
    Captures stdout/stderr, enforces timeout, limits output size, and blocks
    destructive command patterns.

    Restricted commands (git mutations, network tools, package installations)
    are permitted when explicit_user_request is True or auto_approve is True.
    """
    if not isinstance(args, list) or not args:
        raise CommandExecutionError("Command must be a non-empty list of string arguments.")

    cmd_str = " ".join(args)

    # 1. Truly destructive system commands are always blocked
    for pat in _DESTRUCTIVE_COMMAND_PATTERNS:
        if pat.search(cmd_str):
            raise BlockedCommandError(f"Command blocked by safety rules: '{cmd_str}'")

    # 2. Restricted commands (Git mutations, network calls, package installations)
    # Blocked during autonomous inspection, allowed upon explicit user instruction
    if not explicit_user_request and not auto_approve:
        for pat in _RESTRICTED_COMMAND_PATTERNS:
            if pat.search(cmd_str):
                raise BlockedCommandError(
                    f"Command '{cmd_str}' is restricted during repository inspection unless explicitly instructed by the user."
                )

    ws = get_workspace()
    effective_cwd = cwd or ws.root or Path.cwd()

    # Approval check for destructive or dependency commands if not already explicit
    if (args[0] in ("pip", "npm", "yarn", "pnpm", "cargo") and len(args) > 1 and args[1] in ("install", "add", "update")):
        if config_manager.get_engineering_confirm_dependency_installs() and not auto_approve and not explicit_user_request:
            req_msg = confirm_gate.request(
                key=f"install_{args[0]}",
                title=f"Install dependencies: {cmd_str[:60]}",
                detail=f"Executes '{cmd_str}' in {effective_cwd.name}",
                run=lambda: _execute_subprocess(args, timeout, max_output_chars, effective_cwd, env),
            )
            return {
                "status": "approval_required",
                "command": cmd_str,
                "message": req_msg,
            }

    return _execute_subprocess(args, timeout, max_output_chars, effective_cwd, env)


def _execute_subprocess(
    args: list[str],
    timeout: int,
    max_output_chars: int,
    cwd: Path,
    env: Optional[dict],
) -> dict:
    """Internal process execution worker."""
    start_time = time.monotonic()
    timed_out = False
    stdout_text = ""
    stderr_text = ""
    exit_code = -1

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    # Ensure stdout/stderr in subprocess are unbuffered utf-8
    merged_env["PYTHONUNBUFFERED"] = "1"
    merged_env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=merged_env,
        )
        try:
            raw_out, raw_err = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            raw_out, raw_err = proc.communicate()
            timed_out = True
            exit_code = -1

        stdout_text = raw_out.decode("utf-8", errors="replace")
        stderr_text = raw_err.decode("utf-8", errors="replace")

    except FileNotFoundError as e:
        return {
            "status": "error",
            "command": args,
            "error": f"Executable not found: {args[0]} ({e})",
            "exit_code": 127,
            "elapsed_s": time.monotonic() - start_time,
        }
    except Exception as e:
        return {
            "status": "error",
            "command": args,
            "error": str(e),
            "exit_code": -1,
            "elapsed_s": time.monotonic() - start_time,
        }

    elapsed = time.monotonic() - start_time

    if len(stdout_text) > max_output_chars:
        stdout_text = stdout_text[:max_output_chars] + f"\n... [Truncated: output exceeded {max_output_chars} characters]"
    if len(stderr_text) > max_output_chars:
        stderr_text = stderr_text[:max_output_chars] + f"\n... [Truncated: output exceeded {max_output_chars} characters]"

    return {
        "status": "timed_out" if timed_out else ("success" if exit_code == 0 else "failed"),
        "command": args,
        "exit_code": exit_code,
        "stdout": stdout_text.strip(),
        "stderr": stderr_text.strip(),
        "elapsed_s": round(elapsed, 3),
        "timed_out": timed_out,
    }


def run_tests(
    target: Optional[str] = None,
    timeout: int = 120,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Detect and execute project test runner."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    # 1. Python tests
    if (root / "pytest.ini").is_file() or (root / "pyproject.toml").is_file() or any(root.glob("test*.py")) or any(root.glob("tests")):
        # Check if pytest is available
        try:
            import pytest  # noqa: F401
            has_pytest = True
        except ImportError:
            has_pytest = False

        if has_pytest:
            cmd = [sys.executable, "-m", "pytest"]
            if target:
                cmd.append(target)
            else:
                cmd.extend(["-q", "--tb=short"])
            return run_safe_command(cmd, timeout=timeout, cwd=root)
        else:
            cmd = [sys.executable, "-m", "unittest"]
            if target:
                cmd.append(target)
            else:
                cmd.extend(["discover", "-s", ".", "-p", "test*.py"])
            return run_safe_command(cmd, timeout=timeout, cwd=root)

    # 2. Node / JS tests
    if (root / "package.json").is_file():
        cmd = ["npm", "test"]
        if target:
            cmd.extend(["--", target])
        return run_safe_command(cmd, timeout=timeout, cwd=root)

    # 3. Rust tests
    if (root / "Cargo.toml").is_file():
        cmd = ["cargo", "test"]
        if target:
            cmd.append(target)
        return run_safe_command(cmd, timeout=timeout, cwd=root)

    # 4. Go tests
    if (root / "go.mod").is_file():
        cmd = ["go", "test", target or "./..."]
        return run_safe_command(cmd, timeout=timeout, cwd=root)

    return {"error": "No supported test suite detected in the active workspace."}


def run_linter(
    target: Optional[str] = None,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Run available project linter."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    # 1. Python linter: check ruff or flake8, fallback py_compile
    has_python = (root / "pyproject.toml").is_file() or any(root.glob("*.py"))
    if has_python:
        # Try ruff
        if shutil.which("ruff"):
            cmd = ["ruff", "check"] + ([target] if target else ["."])
            return run_safe_command(cmd, cwd=root)
        # Try flake8
        if shutil.which("flake8"):
            cmd = ["flake8"] + ([target] if target else ["."])
            return run_safe_command(cmd, cwd=root)
        # Fallback to py_compile
        if target:
            cmd = [sys.executable, "-m", "py_compile", target]
            return run_safe_command(cmd, cwd=root)
        return {"status": "success", "stdout": "No ruff or flake8 found; syntax validation available via collect_diagnostics."}

    # 2. Node / JS linter
    if (root / "package.json").is_file():
        if shutil.which("npm"):
            return run_safe_command(["npm", "run", "lint"], cwd=root)

    # 3. Rust clippy
    if (root / "Cargo.toml").is_file() and shutil.which("cargo"):
        return run_safe_command(["cargo", "clippy"], cwd=root)

    return {"error": "No supported linter detected for active workspace."}


def run_formatter(
    target: Optional[str] = None,
    check_only: bool = True,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Run available project code formatter."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    if shutil.which("ruff"):
        cmd = ["ruff", "format"]
        if check_only:
            cmd.append("--check")
        if target:
            cmd.append(target)
        else:
            cmd.append(".")
        return run_safe_command(cmd, cwd=root)

    if shutil.which("black"):
        cmd = ["black"]
        if check_only:
            cmd.append("--check")
        if target:
            cmd.append(target)
        else:
            cmd.append(".")
        return run_safe_command(cmd, cwd=root)

    return {"error": "No supported formatter (ruff, black) found on PATH."}


def run_type_checker(
    target: Optional[str] = None,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Run available type checker (mypy, pyright, tsc)."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    # Python: mypy
    try:
        import mypy  # noqa: F401
        has_mypy = True
    except ImportError:
        has_mypy = bool(shutil.which("mypy"))

    if has_mypy:
        cmd = [sys.executable, "-m", "mypy", "--no-error-summary"]
        if target:
            cmd.append(target)
        else:
            cmd.append(".")
        return run_safe_command(cmd, cwd=root)

    if (root / "tsconfig.json").is_file() and shutil.which("npx"):
        return run_safe_command(["npx", "tsc", "--noEmit"], cwd=root)

    return {"error": "No supported type checker (mypy, pyright, tsc) found."}


def run_build(workspace: Optional[WorkspaceManager] = None) -> dict:
    """Run project build command."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    if (root / "setup.py").is_file():
        return run_safe_command([sys.executable, "setup.py", "build"], cwd=root)

    if (root / "package.json").is_file():
        return run_safe_command(["npm", "run", "build"], cwd=root)

    if (root / "Cargo.toml").is_file():
        return run_safe_command(["cargo", "build"], cwd=root)

    if (root / "CMakeLists.txt").is_file() and shutil.which("cmake"):
        return run_safe_command(["cmake", "--build", "build"], cwd=root)

    return {"error": "No recognized build configuration in active workspace."}


def collect_diagnostics(
    target_file: Optional[str] = None,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """
    Run linting, syntax compilation, and type checking to collect structured diagnostics.
    Persists findings to SQLite repository index.
    """
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    diagnostics: list[dict] = []
    index = RepositoryIndex(root)

    # 1. Syntax check Python files using py_compile
    files_to_check: list[Path] = []
    if target_file:
        try:
            target = ws.resolve_safe_path(target_file, must_exist=True, require_file=True)
            if target.suffix == ".py":
                files_to_check.append(target)
        except WorkspaceError as e:
            return {"error": str(e)}
    else:
        # Check top-level and first few python files
        matcher = ws.ignore_matcher
        for p in root.rglob("*.py"):
            if not matcher.is_ignored(p):
                files_to_check.append(p)
                if len(files_to_check) >= 50:
                    break

    import py_compile
    for p in files_to_check:
        rel = p.relative_to(root).as_posix()
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            msg = str(e.msg if hasattr(e, "msg") else e)
            line = getattr(e, "lineno", 1) or 1
            diagnostics.append({
                "file_path": rel,
                "tool": "py_compile",
                "severity": "error",
                "line": int(line),
                "message": msg,
            })

        # Secret and sensitive credential scan (redacting raw secret)
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            sec_findings = detect_hardcoded_secrets(content, file_path=rel)
            for sf in sec_findings:
                diagnostics.append({
                    "file_path": sf["file_path"],
                    "tool": "secret_scanner",
                    "severity": sf["severity"],
                    "line": sf["line"],
                    "message": sf["message"],
                })
        except Exception:
            pass

    # 2. Run linter if available
    lint_res = run_linter(target=target_file, workspace=ws)
    if lint_res.get("status") in ("success", "failed"):
        out = (lint_res.get("stdout") or "") + "\n" + (lint_res.get("stderr") or "")
        # Parse ruff/flake8 format: filepath:line:col: code message
        pattern = re.compile(r"^([^:\n]+):(\d+):(?:\d+:)?\s*(.*)$", re.MULTILINE)
        for m in pattern.finditer(out):
            fpath, line_s, msg = m.groups()
            diagnostics.append({
                "file_path": fpath.strip(),
                "tool": "linter",
                "severity": "warning" if "warning" in msg.lower() else "error",
                "line": int(line_s),
                "message": msg.strip(),
            })

    # Record into index
    index.clear_diagnostics(target_file)
    index.record_diagnostics(diagnostics)

    return {
        "status": "success",
        "target": target_file or "workspace",
        "diagnostics_count": len(diagnostics),
        "diagnostics": diagnostics[:100],
    }
