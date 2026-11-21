"""
core/repository/search.py — Repository search and inspection tools.

Provides list_files, read_file, read_range, search_text, find_symbol,
find_references, and get_repository_summary.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.repository.index import RepositoryIndex
from core.repository.workspace import WorkspaceError, WorkspaceManager, get_workspace
from memory import config_manager


def list_files(
    pattern: Optional[str] = None,
    subdirectory: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """List non-ignored files within the repository with optional filtering."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active repository root selected."}

    matcher = ws.ignore_matcher
    target_dir = root
    if subdirectory:
        try:
            target_dir = ws.resolve_safe_path(subdirectory, must_exist=True)
            if not target_dir.is_dir():
                return {"error": f"Subdirectory '{subdirectory}' is not a directory."}
        except WorkspaceError as e:
            return {"error": str(e)}

    files: list[str] = []
    max_scan = config_manager.get_repository_max_files()

    for dirpath, dirnames, filenames in os.walk(str(target_dir), topdown=True, followlinks=False):
        dp = Path(dirpath)
        # Filter directories using matcher
        dirnames[:] = [d for d in dirnames if not matcher.is_ignored(dp / d, is_dir=True)]

        for f in filenames:
            fp = dp / f
            if matcher.is_ignored(fp, is_dir=False):
                continue
            try:
                rel = fp.relative_to(root).as_posix()
            except ValueError:
                continue

            if pattern and not fnmatch.fnmatch(f, pattern) and not fnmatch.fnmatch(rel, pattern):
                continue

            files.append(rel)
            if len(files) >= max_scan:
                break
        if len(files) >= max_scan:
            break

    files.sort()
    total_count = len(files)
    paginated = files[offset: offset + limit]

    return {
        "files": paginated,
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total_count,
    }


def read_file(
    path: str,
    max_bytes: Optional[int] = None,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Read a repository text file safely."""
    ws = workspace or get_workspace()
    try:
        content, metadata = ws.read_file_text(path, max_bytes=max_bytes)
        return {
            "status": "success",
            "path": metadata["path"],
            "size": metadata["size"],
            "line_count": metadata["line_count"],
            "encoding": metadata["encoding"],
            "content": content,
        }
    except WorkspaceError as e:
        return {"status": "error", "error": str(e)}


def read_range(
    path: str,
    start_line: int,
    end_line: int,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Read a specific line range (1-based, inclusive) from a file."""
    ws = workspace or get_workspace()
    try:
        content, metadata = ws.read_file_text(path)
        all_lines = content.splitlines()
        total = len(all_lines)

        s = max(1, int(start_line))
        e = min(total, int(end_line))

        if s > total:
            return {
                "status": "error",
                "error": f"Requested start line {s} exceeds total lines {total}.",
            }
        if s > e:
            return {
                "status": "error",
                "error": f"Start line ({s}) cannot be greater than end line ({e}).",
            }

        selected = all_lines[s - 1: e]
        formatted = "\n".join(f"{s + idx:5d} | {line}" for idx, line in enumerate(selected))

        return {
            "status": "success",
            "path": metadata["path"],
            "start_line": s,
            "end_line": e,
            "total_lines": total,
            "lines": formatted,
        }
    except WorkspaceError as e:
        return {"status": "error", "error": str(e)}


def search_text(
    query: str,
    is_regex: bool = False,
    case_sensitive: bool = False,
    file_pattern: Optional[str] = None,
    max_results: int = 50,
    max_files: int = 20,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """
    Search text across repository files.
    Returns line-numbered matching snippets bounded by max_results and max_files.
    """
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active repository root selected."}

    if not query:
        return {"error": "Search query cannot be empty."}

    flags = 0 if case_sensitive else re.IGNORECASE
    if is_regex:
        try:
            rx = re.compile(query, flags)
        except re.error as e:
            return {"error": f"Invalid regular expression: {e}"}
    else:
        rx = re.compile(re.escape(query), flags)

    matcher = ws.ignore_matcher
    matches: list[dict] = []
    files_with_matches = 0

    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not matcher.is_ignored(dp / d, is_dir=True)]

        for f in filenames:
            fp = dp / f
            if matcher.is_ignored(fp, is_dir=False):
                continue
            if file_pattern and not fnmatch.fnmatch(f, file_pattern):
                continue

            if ws.is_binary_file(fp):
                continue

            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            lines = content.splitlines()
            file_had_match = False

            for line_no, line_text in enumerate(lines, start=1):
                if rx.search(line_text):
                    if not file_had_match:
                        file_had_match = True
                        files_with_matches += 1

                    try:
                        rel = fp.relative_to(root).as_posix()
                    except ValueError:
                        rel = fp.name

                    # Truncate overly long lines to preserve readability
                    snippet = line_text.strip()
                    if len(snippet) > 200:
                        snippet = snippet[:197] + "..."

                    matches.append({
                        "file": rel,
                        "line": line_no,
                        "snippet": snippet,
                    })

                    if len(matches) >= max_results:
                        break

            if len(matches) >= max_results or files_with_matches >= max_files:
                break
        if len(matches) >= max_results or files_with_matches >= max_files:
            break

    return {
        "query": query,
        "is_regex": is_regex,
        "total_matches": len(matches),
        "files_matched": files_with_matches,
        "matches": matches,
    }


def find_symbol(
    query: str,
    kind: Optional[str] = None,
    exact: bool = False,
    limit: int = 50,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Find a symbol in the SQLite index."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active repository root selected."}

    index = RepositoryIndex(root)
    results = index.find_symbols(name_query=query, kind=kind, exact=exact, limit=limit)
    return {
        "query": query,
        "kind": kind,
        "count": len(results),
        "symbols": results,
    }


def find_references(
    symbol_name: str,
    file_path: Optional[str] = None,
    limit: int = 50,
    workspace: Optional[WorkspaceManager] = None,
) -> dict:
    """Find references to a symbol in the SQLite index."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active repository root selected."}

    index = RepositoryIndex(root)
    results = index.find_references(symbol_name=symbol_name, file_path=file_path, limit=limit)
    return {
        "symbol_name": symbol_name,
        "file_path": file_path,
        "count": len(results),
        "references": results,
    }


def get_repository_summary(workspace: Optional[WorkspaceManager] = None) -> dict:
    """Generate high-level summary of the active repository."""
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active repository root configured."}

    # Detect entry points
    common_entries = ["main.py", "app.py", "index.js", "index.ts", "server.js", "src/main.rs", "main.go"]
    detected_entries = [e for e in common_entries if (root / e).is_file()]

    # Detect build/test configurations
    build_configs = {
        "pyproject.toml": (root / "pyproject.toml").is_file(),
        "requirements.txt": (root / "requirements.txt").is_file(),
        "setup.py": (root / "setup.py").is_file(),
        "package.json": (root / "package.json").is_file(),
        "Cargo.toml": (root / "Cargo.toml").is_file(),
        "go.mod": (root / "go.mod").is_file(),
        "CMakeLists.txt": (root / "CMakeLists.txt").is_file(),
        "pytest.ini": (root / "pytest.ini").is_file(),
    }
    active_configs = [k for k, v in build_configs.items() if v]

    # Index summary
    index = RepositoryIndex(root)
    idx_summary = index.get_summary()

    # Git info if git repository
    git_info = {"is_git": ws.is_git}
    if ws.is_git:
        head_file = root / ".git" / "HEAD"
        if head_file.is_file():
            try:
                head_content = head_file.read_text(encoding="utf-8").strip()
                if head_content.startswith("ref: refs/heads/"):
                    git_info["branch"] = head_content.replace("ref: refs/heads/", "")
                else:
                    git_info["commit"] = head_content[:8]
            except Exception:
                pass

    return {
        "root": str(root),
        "name": root.name,
        "git": git_info,
        "entry_points": detected_entries,
        "build_configurations": active_configs,
        "total_files": idx_summary["total_files"],
        "total_bytes": idx_summary["total_bytes"],
        "total_symbols": idx_summary["total_symbols"],
        "language_distribution": idx_summary["language_distribution"],
        "largest_files": idx_summary["largest_files"],
        "diagnostics_recorded": idx_summary["diagnostic_count"],
    }
