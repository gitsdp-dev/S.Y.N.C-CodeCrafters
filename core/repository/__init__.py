"""
core/repository — S.Y.N.C Workspace & Repository Subsystem.

Provides repository isolation, persistent SQLite indexing, AST symbol extraction,
patch-based editing, safe command execution, and engineering diagnostics.
"""
from core.repository.workspace import (
    WorkspaceError,
    WorkspaceSecurityError,
    WorkspaceFileNotFoundError,
    NoActiveWorkspaceError,
    WorkspaceManager,
    get_workspace,
)
from core.repository.ignore import IgnoreMatcher
from core.repository.index import RepositoryIndex, get_db_path_for_repo
from core.repository.scanner import RepositoryScanner
from core.repository.symbols import detect_language, extract_symbols_and_imports
from core.repository.search import (
    list_files,
    read_file,
    read_range,
    search_text,
    find_symbol,
    find_references,
    get_repository_summary,
)
from core.repository.dependencies import inspect_dependencies
from core.repository.patches import (
    PatchManager,
    get_patch_manager,
    parse_unified_diff,
    apply_patch,
    preview_patch,
    rollback_patch,
    create_file,
    rename_file,
    delete_file,
)
from core.repository.diagnostics import (
    run_safe_command,
    run_tests,
    run_linter,
    run_formatter,
    run_type_checker,
    run_build,
    collect_diagnostics,
)
from core.repository.git_tools import (
    git_status,
    git_diff,
    git_log,
    git_branch,
    git_commit,
    git_push,
)
from core.repository.ide import (
    DetectedIDE,
    detect_installed_ides,
    launch_ide,
    get_preferred_ide,
    set_preferred_ide,
    register_pending_ide_choice,
    get_pending_ide_choice,
    clear_pending_ide_choice,
    resolve_pending_ide_choice,
)
from core.repository.audit import (
    CodeAuditEntry,
    CodeAuditLogger,
    get_audit_logger,
)

__all__ = [
    "WorkspaceError",
    "WorkspaceSecurityError",
    "WorkspaceFileNotFoundError",
    "NoActiveWorkspaceError",
    "WorkspaceManager",
    "get_workspace",
    "IgnoreMatcher",
    "RepositoryIndex",
    "get_db_path_for_repo",
    "RepositoryScanner",
    "detect_language",
    "extract_symbols_and_imports",
    "list_files",
    "read_file",
    "read_range",
    "search_text",
    "find_symbol",
    "find_references",
    "get_repository_summary",
    "inspect_dependencies",
    "PatchManager",
    "get_patch_manager",
    "parse_unified_diff",
    "run_safe_command",
    "run_tests",
    "run_linter",
    "run_formatter",
    "run_type_checker",
    "run_build",
    "collect_diagnostics",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
]
