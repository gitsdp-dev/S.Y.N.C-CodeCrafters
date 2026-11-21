"""
actions/repo_tools.py — Standard repository, editing, validation, and git operations
for S.Y.N.C software engineering mode.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.repository import (
    WorkspaceError,
    apply_patch as _apply_patch_op,
    collect_diagnostics as _collect_diagnostics_op,
    find_references as _find_references_op,
    find_symbol as _find_symbol_op,
    get_patch_manager,
    get_repository_summary as _get_repository_summary_op,
    git_branch as _git_branch_op,
    git_commit as _git_commit_op,
    git_diff as _git_diff_op,
    git_log as _git_log_op,
    git_push as _git_push_op,
    git_status as _git_status_op,
    inspect_dependencies as _inspect_dependencies_op,
    list_files as _list_files_op,
    preview_patch as _preview_patch_op,
    read_file as _read_file_op,
    read_range as _read_range_op,
    run_build as _run_build_op,
    run_formatter as _run_formatter_op,
    run_linter as _run_linter_op,
    run_tests as _run_tests_op,
    run_type_checker as _run_type_checker_op,
    search_text as _search_text_op,
)


def _fmt(obj: Any) -> str:
    """Format dictionary or result as compact readable text or JSON."""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, indent=2)
    except Exception:
        return str(obj)


# ── Action Handlers ──────────────────────────────────────────────────────────

def list_files(parameters: dict, player=None) -> str:
    res = _list_files_op(
        pattern=parameters.get("pattern"),
        subdirectory=parameters.get("subdirectory"),
        limit=int(parameters.get("limit") or 200),
        offset=int(parameters.get("offset") or 0),
    )
    return _fmt(res)


def read_file(parameters: dict, player=None) -> str:
    path = parameters.get("path")
    if not path:
        return "Error: 'path' parameter is required."
    max_b = int(parameters.get("max_bytes")) if parameters.get("max_bytes") else None
    res = _read_file_op(path, max_bytes=max_b)
    if res.get("status") == "success":
        content = res.get("content", "")
        # For large output, keep bounded
        if len(content) > 40_000:
            preview = content[:40_000] + f"\n... [Truncated: {len(content)} total characters]"
            return f"File: {res.get('path')} ({res.get('line_count')} lines, {res.get('size')} bytes)\n\n{preview}"
        return f"File: {res.get('path')} ({res.get('line_count')} lines, {res.get('size')} bytes)\n\n{content}"
    return _fmt(res)


def read_range(parameters: dict, player=None) -> str:
    path = parameters.get("path")
    if not path:
        return "Error: 'path' parameter is required."
    try:
        start_line = int(parameters.get("start_line", 1))
        end_line = int(parameters.get("end_line", 100))
    except (ValueError, TypeError):
        return "Error: 'start_line' and 'end_line' must be integers."
    res = _read_range_op(path, start_line, end_line)
    if res.get("status") == "success":
        return f"Range {res['start_line']}-{res['end_line']} of {res['path']} ({res['total_lines']} total lines):\n\n{res['lines']}"
    return _fmt(res)


def search_text(parameters: dict, player=None) -> str:
    query = parameters.get("query")
    if not query:
        return "Error: 'query' parameter is required."
    res = _search_text_op(
        query=query,
        is_regex=bool(parameters.get("is_regex", False)),
        case_sensitive=bool(parameters.get("case_sensitive", False)),
        file_pattern=parameters.get("file_pattern"),
        max_results=int(parameters.get("max_results") or 50),
        max_files=int(parameters.get("max_files") or 20),
    )
    if res.get("matches"):
        lines = [f"Found {res['total_matches']} match(es) across {res['files_matched']} file(s) for '{query}':"]
        for m in res["matches"]:
            lines.append(f"{m['file']}:{m['line']}: {m['snippet']}")
        return "\n".join(lines)
    elif "error" in res:
        return f"Search error: {res['error']}"
    return f"No matches found for '{query}'."


def find_symbol(parameters: dict, player=None) -> str:
    query = parameters.get("query")
    if not query:
        return "Error: 'query' parameter is required."
    res = _find_symbol_op(
        query=query,
        kind=parameters.get("kind"),
        exact=bool(parameters.get("exact", False)),
        limit=int(parameters.get("limit") or 50),
    )
    syms = res.get("symbols", [])
    if syms:
        lines = [f"Found {len(syms)} symbol(s) matching '{query}':"]
        for s in syms:
            lines.append(f"  [{s['kind']}] {s['name']} -> {s['file_path']}:{s['line_start']}-{s['line_end']} | {s.get('signature', '')}")
        return "\n".join(lines)
    return f"No symbols found matching '{query}'."


def find_references(parameters: dict, player=None) -> str:
    sym = parameters.get("symbol_name")
    if not sym:
        return "Error: 'symbol_name' parameter is required."
    res = _find_references_op(
        symbol_name=sym,
        file_path=parameters.get("file_path"),
        limit=int(parameters.get("limit") or 50),
    )
    refs = res.get("references", [])
    if refs:
        lines = [f"Found {len(refs)} reference(s) for '{sym}':"]
        for r in refs:
            lines.append(f"  {r['file_path']}:{r['line']}")
        return "\n".join(lines)
    return f"No references found for '{sym}' in the index."


def inspect_dependencies(parameters: dict, player=None) -> str:
    res = _inspect_dependencies_op()
    return _fmt(res)


def get_repository_summary(parameters: dict, player=None) -> str:
    res = _get_repository_summary_op()
    return _fmt(res)


def preview_patch(parameters: dict, player=None) -> str:
    diff = parameters.get("diff_text")
    if not diff:
        return "Error: 'diff_text' is required."
    pm = get_patch_manager()
    try:
        res = pm.preview_patch(diff)
        return _fmt(res)
    except WorkspaceError as e:
        return f"Patch preview error: {e}"


def apply_patch(parameters: dict, player=None) -> str:
    diff = parameters.get("diff_text")
    if not diff:
        return "Error: 'diff_text' is required."
    auto_approve = bool(parameters.get("auto_approve", False))
    pm = get_patch_manager()
    try:
        res = pm.apply_patch(diff, auto_approve=auto_approve)
        return _fmt(res)
    except WorkspaceError as e:
        return f"Apply patch error: {e}"


def rollback_patch(parameters: dict, player=None) -> str:
    snap_id = parameters.get("snapshot_id")
    pm = get_patch_manager()
    res = pm.rollback_patch(snap_id)
    return _fmt(res)


def create_file(parameters: dict, player=None) -> str:
    path = parameters.get("path")
    content = parameters.get("content", "")
    if not path:
        return "Error: 'path' is required."
    auto_approve = bool(parameters.get("auto_approve", False))
    pm = get_patch_manager()
    res = pm.create_file(path, content, auto_approve=auto_approve)
    return _fmt(res)


def rename_file(parameters: dict, player=None) -> str:
    old_p = parameters.get("old_path")
    new_p = parameters.get("new_path")
    if not old_p or not new_p:
        return "Error: 'old_path' and 'new_path' are required."
    auto_approve = bool(parameters.get("auto_approve", False))
    pm = get_patch_manager()
    res = pm.rename_file(old_p, new_p, auto_approve=auto_approve)
    return _fmt(res)


def delete_file(parameters: dict, player=None) -> str:
    path = parameters.get("path")
    if not path:
        return "Error: 'path' is required."
    auto_approve = bool(parameters.get("auto_approve", False))
    pm = get_patch_manager()
    res = pm.delete_file(path, auto_approve=auto_approve)
    return _fmt(res)


def run_tests(parameters: dict, player=None) -> str:
    target = parameters.get("target")
    timeout = int(parameters.get("timeout") or 120)
    res = _run_tests_op(target=target, timeout=timeout)
    return _fmt(res)


def run_linter(parameters: dict, player=None) -> str:
    target = parameters.get("target")
    res = _run_linter_op(target=target)
    return _fmt(res)


def run_formatter(parameters: dict, player=None) -> str:
    target = parameters.get("target")
    check_only = bool(parameters.get("check_only", True))
    res = _run_formatter_op(target=target, check_only=check_only)
    return _fmt(res)


def run_type_checker(parameters: dict, player=None) -> str:
    target = parameters.get("target")
    res = _run_type_checker_op(target=target)
    return _fmt(res)


def run_build(parameters: dict, player=None) -> str:
    res = _run_build_op()
    return _fmt(res)


def collect_diagnostics(parameters: dict, player=None) -> str:
    target = parameters.get("target_file")
    res = _collect_diagnostics_op(target_file=target)
    return _fmt(res)


def git_status(parameters: dict, player=None) -> str:
    res = _git_status_op()
    return _fmt(res)


def git_diff(parameters: dict, player=None) -> str:
    staged = bool(parameters.get("staged", False))
    file_path = parameters.get("file_path")
    commit = parameters.get("commit")
    res = _git_diff_op(staged=staged, file_path=file_path, commit=commit)
    return _fmt(res)


def git_log(parameters: dict, player=None) -> str:
    limit = int(parameters.get("limit") or 15)
    file_path = parameters.get("file_path")
    res = _git_log_op(limit=limit, file_path=file_path)
    return _fmt(res)


def git_branch(parameters: dict, player=None) -> str:
    res = _git_branch_op()
    return _fmt(res)


def git_commit(parameters: dict, player=None) -> str:
    """Commit changes to the active repository on explicit user instruction."""
    message = parameters.get("message")
    if not message:
        return "Error: 'message' parameter is required for git commit."
    add_all = bool(parameters.get("add_all", True))
    res = _git_commit_op(message=message, add_all=add_all, explicit_user_request=True)
    if player:
        player.write_log(f"[Git] 📌 Committed: {message[:50]}")
    return _fmt(res)


def git_push(parameters: dict, player=None) -> str:
    """Push commits to remote (e.g. GitHub) on explicit user instruction."""
    remote = parameters.get("remote", "origin")
    branch = parameters.get("branch")
    res = _git_push_op(remote=remote, branch=branch, explicit_user_request=True)
    if player:
        player.write_log(f"[Git] 🚀 Pushed to {remote} {branch or ''}".strip())
    return _fmt(res)


def write_ide_terminal(parameters: dict, player=None) -> str:
    """
    Write the exact command into the active IDE / VS Code terminal WITHOUT executing it.
    The command is typed/pasted into the terminal prompt and staged.
    It will NOT execute until the user explicitly says to execute it.
    """
    cmd = parameters.get("command")
    if not cmd:
        return "Error: 'command' parameter is required."

    ide_name = parameters.get("ide")
    dry_run = bool(parameters.get("dry_run", False))

    from core.repository.ide import write_command_to_ide_terminal
    ok, msg, used_ide = write_command_to_ide_terminal(
        command=cmd,
        ide=ide_name,
        dry_run=dry_run,
        player=player,
    )
    return msg


def execute_ide_terminal(parameters: dict, player=None) -> str:
    """
    Execute the command currently staged in the IDE / VS Code terminal on explicit user instruction.
    Called when the user says 'execute it', 'run it', 'press enter', or 'run that command'.
    """
    cmd = parameters.get("command")
    dry_run = bool(parameters.get("dry_run", False))

    from core.repository.ide import execute_staged_ide_terminal
    ok, msg, used_ide = execute_staged_ide_terminal(
        command=cmd,
        dry_run=dry_run,
        player=player,
    )
    return msg


def execute_command(parameters: dict, player=None) -> str:
    """
    Execute a terminal command within the repository on explicit user instruction.
    Allows running curl, wget, package installations, or build scripts when explicitly requested.
    If stage_only is True, writes the command to the IDE terminal without running it.
    """
    cmd = parameters.get("command")
    if not cmd:
        return "Error: 'command' parameter is required."

    # If staging is requested or user asked to write to terminal, use IDE terminal staging
    if parameters.get("stage_only") or parameters.get("write_only"):
        return write_ide_terminal(parameters, player=player)

    if isinstance(cmd, str):
        import shlex
        try:
            args = shlex.split(cmd)
        except Exception:
            args = cmd.split()
    elif isinstance(cmd, list):
        args = [str(x) for x in cmd]
    else:
        return "Error: 'command' must be a string or list of arguments."

    timeout = int(parameters.get("timeout") or 60)
    from core.repository.diagnostics import run_safe_command
    try:
        res = run_safe_command(args, timeout=timeout, explicit_user_request=True)
        if player:
            player.write_log(f"[Shell] 💻 Ran: {' '.join(args)[:50]}")
        return _fmt(res)
    except Exception as e:
        return f"Command execution error: {e}"



def add_workspace_folder(parameters: dict, player=None) -> str:
    """
    Set and incrementally scan an active workspace repository.
    Detects installed IDEs on the user's PC:
    - If 1 IDE: auto-launches that IDE and starts real-time coding.
    - If >1 IDEs: prompts user in voice and on HUD popup to choose editor.
    - If 0 IDEs: opens folder in OS explorer.
    """
    path_str = parameters.get("path")
    if not path_str:
        return "Error: 'path' parameter is required."

    preferred_ide_arg = str(parameters.get("ide") or "").strip()

    from core.repository.workspace import get_workspace, WorkspaceError
    from core.repository.ide import (
        detect_installed_ides,
        launch_ide,
        register_pending_ide_choice,
        get_preferred_ide,
        set_preferred_ide,
    )
    from core.repository.scanner import RepositoryScanner
    from core.repository.index import RepositoryIndex, get_db_path_for_repo
    from memory.config_manager import save_engineering_mode
    import threading
    import os, sys

    ws = get_workspace()
    try:
        target_root = ws.set_root(path_str)
    except WorkspaceError as e:
        return f"Failed to add workspace folder: {e}"

    # Auto-enable engineering mode
    save_engineering_mode(True)
    if player and hasattr(player, "set_engineering_mode"):
        try:
            player.set_engineering_mode(True)
        except Exception:
            pass

    folder_name = target_root.name

    # Background incremental scan
    def _bg_scan():
        try:
            idx = RepositoryIndex(get_db_path_for_repo(target_root))
            scanner = RepositoryScanner(target_root, idx)
            count = scanner.scan()
            if player:
                player.write_log(f"[RepoIndex] 📁 Scanned {count} files in {folder_name}.")
        except Exception as e:
            if player:
                player.write_log(f"[RepoIndex] ⚠️ Background scan warning: {e}")

    threading.Thread(target=_bg_scan, daemon=True, name=f"scan-{folder_name}").start()

    # Detect installed IDEs on the machine
    ides = detect_installed_ides()

    # Case 1: No IDE found
    if not ides:
        if sys.platform == "win32":
            try:
                os.startfile(str(target_root))
            except Exception:
                pass
        msg = (
            f"Added and scanning workspace folder '{folder_name}'. No dedicated IDE was detected on your PC, "
            f"so I've opened it in File Explorer. SYNC is ready to code and apply real-time modifications."
        )
        if player:
            player.write_log(f"[Workspace] 📁 {msg}")
        return msg

    # Case 2: User explicitly requested an IDE or 1 IDE available or preferred IDE exists
    chosen_ide = None
    if preferred_ide_arg:
        chosen_ide = next((i for i in ides if i.id.lower() == preferred_ide_arg.lower() or preferred_ide_arg.lower() in i.name.lower()), None)

    if not chosen_ide and len(ides) == 1:
        chosen_ide = ides[0]

    if not chosen_ide and len(ides) > 1:
        saved_pref = get_preferred_ide()
        if saved_pref:
            chosen_ide = next((i for i in ides if i.id == saved_pref), None)

    dry_run = bool(parameters.get("dry_run", False))
    if chosen_ide:
        ok, launch_msg = launch_ide(chosen_ide, target_root, dry_run=dry_run)
        if player:
            player.write_log(f"[IDE] 💻 {chosen_ide.name} opened for '{folder_name}'. Real-time coding active.")
        return (
            f"Added workspace folder '{folder_name}' and opened it in {chosen_ide.name}. "
            f"SYNC is actively scanning the repository. Any code changes will appear live in your IDE."
        )

    # Case 3: Multiple IDEs detected (>1) and no explicit choice
    ide_names = [i.name for i in ides]
    register_pending_ide_choice(target_root, ides)

    if player and hasattr(player, "show_ide_picker"):
        player.show_ide_picker(str(target_root), [
            {"id": i.id, "name": i.name, "icon": i.icon} for i in ides
        ])

    return (
        f"[IDE_CHOICE_PENDING] Added workspace folder '{folder_name}'. Multiple IDEs are installed on your computer: "
        f"{', '.join(ide_names)}. Ask the user in voice which IDE they prefer to use, or tell them to select one "
        f"from the options on the screen. Coding changes will synchronize to their chosen IDE in real time."
    )


def choose_ide(parameters: dict, player=None) -> str:
    """
    Select an IDE to launch for real-time coding (answers voice question or UI option).
    """
    ide_input = str(parameters.get("ide") or "").strip()
    if not ide_input:
        return "Error: 'ide' parameter is required (e.g. 'vscode', 'cursor', 'pycharm', 'insiders')."

    from core.repository.ide import resolve_pending_ide_choice, launch_ide, detect_installed_ides, set_preferred_ide
    from core.repository.workspace import get_workspace

    dry_run = bool(parameters.get("dry_run", False))
    ok, msg, matched = resolve_pending_ide_choice(ide_input, dry_run=dry_run)
    if ok and matched:
        if player and hasattr(player, "hide_ide_picker"):
            try:
                player.hide_ide_picker()
            except Exception:
                pass
        if player:
            player.write_log(f"[IDE] 💻 Selected and opened {matched.name}.")
        return (
            f"Opened {matched.name} for the active workspace. Real-time coding mode is ready; "
            f"all code changes will appear in your editor immediately."
        )

    # If no pending choice was waiting, try direct launch on active repository
    ws = get_workspace()
    root = ws.root
    if not root:
        return "No active workspace folder is configured. Please add a folder first using add_workspace_folder."

    detected = detect_installed_ides()
    match = next((d for d in detected if d.id.lower() == ide_input.lower() or ide_input.lower() in d.name.lower()), None)
    if match:
        ok, launch_msg = launch_ide(match, root, dry_run=dry_run)
        if ok:
            set_preferred_ide(match.id)
            if player:
                player.write_log(f"[IDE] 💻 {match.name} launched on {root.name}.")
            return f"Opened {match.name} for workspace '{root.name}'. Real-time changes will appear in the editor."
        return f"Failed to launch {match.name}: {launch_msg}"

    avail = ", ".join(d.name for d in detected)
    return f"Could not find IDE '{ide_input}'. Available installed IDEs: {avail or 'None'}."


def list_available_ides(parameters: dict, player=None) -> str:
    """List detected IDEs and editors on the user's computer."""
    from core.repository.ide import detect_installed_ides, get_preferred_ide
    ides = detect_installed_ides()
    if not ides:
        return "No dedicated IDEs detected in standard paths or on PATH."
    preferred = get_preferred_ide()
    lines = ["Detected IDEs and Editors:"]
    for i, ide in enumerate(ides, 1):
        pref = " (Default / Preferred)" if ide.id == preferred else ""
        lines.append(f"{i}. {ide.icon} {ide.name} [id: {ide.id}]{pref}")
        lines.append(f"   Command: {ide.command}  |  Path: {ide.path}")
    return "\n".join(lines)


def get_code_audit(parameters: dict, player=None) -> str:
    """Retrieve the real-time code change audit trail and diffs."""
    from core.repository.audit import get_audit_logger
    limit = int(parameters.get("limit") or 20)
    logger = get_audit_logger()

    if parameters.get("clear"):
        logger.clear()
        if player:
            player.show_content("REAL-TIME CODE AUDIT", "Real-time code audit trail has been cleared.")
            if hasattr(player, "show_code_audit"):
                try:
                    player.show_code_audit([])
                except Exception:
                    pass
        return "Real-time code audit trail has been cleared."

    entries = logger.get_recent(limit)
    if not entries:
        if player and hasattr(player, "show_code_audit"):
            try:
                player.show_code_audit([])
            except Exception:
                pass
        return "No real-time code modifications have been recorded yet."

    ui_text = logger.format_ui_audit_text(limit)
    if player:
        player.show_content("REAL-TIME CODE AUDIT", ui_text)
        if hasattr(player, "show_code_audit"):
            try:
                player.show_code_audit(entries)
            except Exception:
                pass

    voice_summary = logger.generate_voice_summary(limit=min(len(entries), 3))
    return f"{voice_summary}\n\n{ui_text}"


# ── Declarations List ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_files",
        "description": "List non-ignored files within the active repository.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "pattern": {"type": "STRING", "description": "Optional glob pattern (e.g. '*.py')"},
                "subdirectory": {"type": "STRING", "description": "Optional relative subdirectory"},
                "limit": {"type": "INTEGER", "description": "Maximum number of files to return (default 200)"},
                "offset": {"type": "INTEGER", "description": "Pagination offset (default 0)"},
            },
        },
        "handler": list_files,
        "tier": "core",
    },
    {
        "name": "read_file",
        "description": "Read the text content and metadata of a file in the active repository.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Relative path to file"},
                "max_bytes": {"type": "INTEGER", "description": "Optional maximum file bytes to read"},
            },
            "required": ["path"],
        },
        "handler": read_file,
        "tier": "core",
    },
    {
        "name": "read_range",
        "description": "Read a specific 1-based line range from a repository file.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Relative path to file"},
                "start_line": {"type": "INTEGER", "description": "1-based starting line number"},
                "end_line": {"type": "INTEGER", "description": "1-based ending line number"},
            },
            "required": ["path", "start_line", "end_line"],
        },
        "handler": read_range,
        "tier": "core",
    },
    {
        "name": "search_text",
        "description": "Search literal text or regex patterns across repository source files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Search query or regex"},
                "is_regex": {"type": "BOOLEAN", "description": "True if query is a regular expression"},
                "case_sensitive": {"type": "BOOLEAN", "description": "True for case sensitive search"},
                "file_pattern": {"type": "STRING", "description": "Optional file glob filter (e.g. '*.py')"},
                "max_results": {"type": "INTEGER", "description": "Maximum match occurrences (default 50)"},
            },
            "required": ["query"],
        },
        "handler": search_text,
        "tier": "core",
    },
    {
        "name": "find_symbol",
        "description": "Search functions, classes, methods, and constants in the repository index.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Symbol name or substring to find"},
                "kind": {"type": "STRING", "description": "Optional kind filter: class, function, method, constant"},
                "exact": {"type": "BOOLEAN", "description": "True for exact symbol name match"},
                "limit": {"type": "INTEGER", "description": "Maximum number of results (default 50)"},
            },
            "required": ["query"],
        },
        "handler": find_symbol,
        "tier": "core",
    },
    {
        "name": "find_references",
        "description": "Find occurrences and call sites of a symbol across the repository.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "symbol_name": {"type": "STRING", "description": "Symbol name to look up"},
                "file_path": {"type": "STRING", "description": "Optional file path filter"},
                "limit": {"type": "INTEGER", "description": "Maximum results (default 50)"},
            },
            "required": ["symbol_name"],
        },
        "handler": find_references,
        "tier": "core",
    },
    {
        "name": "inspect_dependencies",
        "description": "Inspect project dependencies from manifests and imported modules.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": inspect_dependencies,
        "tier": "core",
    },
    {
        "name": "get_repository_summary",
        "description": "Get high-level summary of active repository structure, languages, entry points, and index state.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": get_repository_summary,
        "tier": "core",
    },
    {
        "name": "preview_patch",
        "description": "Preview a unified diff patch and validate hunk context without applying changes.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "diff_text": {"type": "STRING", "description": "Full unified diff text"},
            },
            "required": ["diff_text"],
        },
        "handler": preview_patch,
        "tier": "core",
    },
    {
        "name": "apply_patch",
        "description": "Atomically apply a unified diff patch to a file with approval gating and rollback tracking.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "diff_text": {"type": "STRING", "description": "Unified diff text to apply"},
                "auto_approve": {"type": "BOOLEAN", "description": "True to bypass confirmation gate if allowed"},
            },
            "required": ["diff_text"],
        },
        "handler": apply_patch,
        "tier": "core",
    },
    {
        "name": "rollback_patch",
        "description": "Roll back a previously applied patch, created file, or file modification.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "snapshot_id": {"type": "STRING", "description": "Optional snapshot ID to restore; defaults to latest"},
            },
        },
        "handler": rollback_patch,
        "tier": "core",
    },
    {
        "name": "create_file",
        "description": "Create a new file within the active repository.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Relative file path to create"},
                "content": {"type": "STRING", "description": "Content of the file"},
                "auto_approve": {"type": "BOOLEAN", "description": "True to bypass approval gate"},
            },
            "required": ["path", "content"],
        },
        "handler": create_file,
        "tier": "core",
    },
    {
        "name": "rename_file",
        "description": "Rename or move a file within the active repository.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "old_path": {"type": "STRING", "description": "Existing relative file path"},
                "new_path": {"type": "STRING", "description": "New relative file path"},
                "auto_approve": {"type": "BOOLEAN", "description": "True to bypass approval gate"},
            },
            "required": ["old_path", "new_path"],
        },
        "handler": rename_file,
        "tier": "core",
    },
    {
        "name": "delete_file",
        "description": "Delete a file within the active repository with rollback tracking.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Relative file path to delete"},
                "auto_approve": {"type": "BOOLEAN", "description": "True to bypass approval gate"},
            },
            "required": ["path"],
        },
        "handler": delete_file,
        "tier": "core",
    },
    {
        "name": "run_tests",
        "description": "Run the project's test suite or targeted tests.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {"type": "STRING", "description": "Optional specific test file or test pattern"},
                "timeout": {"type": "INTEGER", "description": "Execution timeout in seconds (default 120)"},
            },
        },
        "handler": run_tests,
        "tier": "core",
    },
    {
        "name": "run_linter",
        "description": "Run the project's linter (ruff, flake8, clippy, npm lint).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {"type": "STRING", "description": "Optional relative path to check"},
            },
        },
        "handler": run_linter,
        "tier": "core",
    },
    {
        "name": "run_formatter",
        "description": "Check or format project code using ruff, black, or prettier.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {"type": "STRING", "description": "Optional path to check or format"},
                "check_only": {"type": "BOOLEAN", "description": "True to only check without modifying (default True)"},
            },
        },
        "handler": run_formatter,
        "tier": "core",
    },
    {
        "name": "run_type_checker",
        "description": "Run project type checker (mypy, pyright, tsc).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {"type": "STRING", "description": "Optional file path to check"},
            },
        },
        "handler": run_type_checker,
        "tier": "core",
    },
    {
        "name": "run_build",
        "description": "Run the project build step.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": run_build,
        "tier": "core",
    },
    {
        "name": "collect_diagnostics",
        "description": "Run syntax checks, linters, and type checkers to collect diagnostics into the repository index.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target_file": {"type": "STRING", "description": "Optional target file to analyze"},
            },
        },
        "handler": collect_diagnostics,
        "tier": "core",
    },
    {
        "name": "git_status",
        "description": "Get git branch and working tree status (staged, unstaged, untracked).",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": git_status,
        "tier": "core",
    },
    {
        "name": "git_diff",
        "description": "Get git diff for staged, unstaged, or commit changes.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "staged": {"type": "BOOLEAN", "description": "True to inspect staged changes (--cached)"},
                "file_path": {"type": "STRING", "description": "Optional file path filter"},
                "commit": {"type": "STRING", "description": "Optional commit hash or reference"},
            },
        },
        "handler": git_diff,
        "tier": "core",
    },
    {
        "name": "git_log",
        "description": "Get recent git commit history.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "limit": {"type": "INTEGER", "description": "Number of commits to retrieve (default 15)"},
                "file_path": {"type": "STRING", "description": "Optional file path filter"},
            },
        },
        "handler": git_log,
        "tier": "core",
    },
    {
        "name": "git_branch",
        "description": "List local and remote git branches.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": git_branch,
        "tier": "core",
    },
    {
        "name": "git_commit",
        "description": "Commit staged or workspace changes to the active Git repository on explicit user instruction.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING", "description": "Commit message describing the changes"},
                "add_all": {"type": "BOOLEAN", "description": "True to stage all changes before committing (default True)"},
            },
            "required": ["message"],
        },
        "handler": git_commit,
        "tier": "core",
    },
    {
        "name": "git_push",
        "description": "Push committed changes to remote repository (e.g. GitHub) on explicit user instruction.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "remote": {"type": "STRING", "description": "Remote name (default 'origin')"},
                "branch": {"type": "STRING", "description": "Optional branch name to push to"},
            },
        },
        "handler": git_push,
        "tier": "core",
    },
    {
        "name": "write_ide_terminal",
        "description": "Write the exact command into the active VS Code / IDE terminal WITHOUT executing it. The command sits typed in the terminal prompt. It will NOT execute until the user explicitly says to execute it.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "The exact shell command string to write into the IDE terminal"},
                "ide": {"type": "STRING", "description": "Optional specific IDE name (e.g. 'vscode', 'cursor', 'pycharm')"},
            },
            "required": ["command"],
        },
        "handler": write_ide_terminal,
        "tier": "core",
    },
    {
        "name": "execute_ide_terminal",
        "description": "Execute the command currently written/staged in the active VS Code / IDE terminal on explicit user instruction (e.g. 'execute it', 'run it', 'press enter').",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "Optional command string to verify or run if no command was staged"},
            },
        },
        "handler": execute_ide_terminal,
        "tier": "core",
    },
    {
        "name": "execute_command",
        "description": "Execute terminal or shell commands (e.g. curl, wget, npm install, pip install, build tools) on explicit user instruction.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "Command string to execute in repository root"},
                "timeout": {"type": "INTEGER", "description": "Timeout in seconds (default 60)"},
            },
            "required": ["command"],
        },
        "handler": execute_command,
        "tier": "core",
    },
    {
        "name": "add_workspace_folder",
        "description": "Add, scan, and open a workspace folder for real-time coding in the user's preferred IDE.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Path to workspace folder or project directory"},
                "ide": {"type": "STRING", "description": "Optional specific IDE name or id to open (e.g. 'vscode', 'cursor', 'pycharm')"},
            },
            "required": ["path"],
        },
        "handler": add_workspace_folder,
        "tier": "core",
    },
    {
        "name": "choose_ide",
        "description": "Choose which IDE to open for real-time coding when prompted or switching editors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "ide": {"type": "STRING", "description": "Name or ID of chosen IDE (e.g. 'vscode', 'cursor', 'pycharm', 'insiders')"},
            },
            "required": ["ide"],
        },
        "handler": choose_ide,
        "tier": "core",
    },
    {
        "name": "list_available_ides",
        "description": "List all detected IDEs and editors installed on the user's PC.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": list_available_ides,
        "tier": "core",
    },
    {
        "name": "get_code_audit",
        "description": "Retrieve the real-time code change audit trail, diff deltas, and voice summaries.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "limit": {"type": "INTEGER", "description": "Number of recent changes to retrieve (default 20)"},
            },
        },
        "handler": get_code_audit,
        "tier": "core",
    },
]

# For backwards compatibility with single-TOOL action scanner
TOOL = TOOLS[0]
