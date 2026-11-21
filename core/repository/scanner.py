"""
core/repository/scanner.py — Incremental CPU-efficient repository scanner.

Discovers repository files, checks mtime/size/SHA-256, extracts AST symbols
concurrently within worker limits, and updates the SQLite index.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from core.repository.ignore import IgnoreMatcher
from core.repository.index import RepositoryIndex
from core.repository.symbols import detect_language, extract_symbols_and_imports
from core.repository.workspace import WorkspaceManager, get_workspace
from memory import config_manager


def _hash_file_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class RepositoryScanner:
    """Performs incremental scanning and indexing on a repository."""

    def __init__(
        self,
        workspace: Optional[WorkspaceManager] = None,
        index: Optional[RepositoryIndex] = None,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.workspace = workspace or get_workspace()
        self.logger = logger or print
        self._index = index

    @property
    def index(self) -> RepositoryIndex:
        if self._index is None:
            root = self.workspace.root
            if not root:
                raise ValueError("No active repository root to index.")
            self._index = RepositoryIndex(root)
        return self._index

    def scan(
        self,
        force_reindex: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict:
        """
        Run an incremental scan over the workspace.

        Returns summary of scanned, indexed, skipped, and removed files.
        Runs safely in worker threads without touching Qt widgets.
        """
        root = self.workspace.root
        if not root:
            return {"status": "error", "error": "No active workspace configured."}

        matcher = self.workspace.ignore_matcher
        max_depth = config_manager.get_repository_max_depth()
        max_files = config_manager.get_repository_max_files()
        max_bytes = config_manager.get_repository_max_file_bytes()
        max_workers = config_manager.get_repository_index_worker_count()

        # Step 1: Collect files respecting ignores, symlinks, and max depth
        discovered_paths: list[Path] = []
        root_depth = len(root.parts)

        for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
            dp = Path(dirpath)
            current_depth = len(dp.parts) - root_depth
            if current_depth > max_depth:
                dirnames.clear()
                continue

            # Filter dirnames in-place to avoid descending into ignored directories
            filtered_dirs = []
            for d in dirnames:
                dir_path = dp / d
                if not matcher.is_ignored(dir_path, is_dir=True):
                    filtered_dirs.append(d)
            dirnames[:] = filtered_dirs

            for f in filenames:
                fp = dp / f
                if not matcher.is_ignored(fp, is_dir=False):
                    discovered_paths.append(fp)
                    if len(discovered_paths) >= max_files:
                        self.logger(f"[Scanner] Reached maximum file limit ({max_files}). Stopping discovery.")
                        break

            if len(discovered_paths) >= max_files:
                break

        total_discovered = len(discovered_paths)
        current_relative_paths: set[str] = set()

        files_to_parse: list[Tuple[Path, str, os.stat_result, str]] = []  # (abs_path, rel_path, stat, hash)
        unchanged_count = 0
        skipped_count = 0

        # Step 2: Compare against index using mtime and size before hashing
        for p in discovered_paths:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue

            current_relative_paths.add(rel)

            try:
                st = p.stat()
            except OSError:
                skipped_count += 1
                continue

            # Skip oversized files
            if st.st_size > max_bytes:
                skipped_count += 1
                continue

            # Skip binary files
            if self.workspace.is_binary_file(p):
                skipped_count += 1
                continue

            existing = self.index.get_indexed_file(rel)
            if existing and not force_reindex:
                # Fast check: mtime_ns and size match
                if existing["mtime_ns"] == st.st_mtime_ns and existing["size"] == st.st_size:
                    unchanged_count += 1
                    continue

                # Content hash check
                try:
                    file_hash = _hash_file_sha256(p)
                except OSError:
                    skipped_count += 1
                    continue

                if existing["sha256"] == file_hash:
                    # File was touched without content changes — just update mtime
                    self.index.touch_file_mtime(rel, st.st_mtime_ns)
                    unchanged_count += 1
                    continue
            else:
                try:
                    file_hash = _hash_file_sha256(p)
                except OSError:
                    skipped_count += 1
                    continue

            files_to_parse.append((p, rel, st, file_hash))

        # Step 3: Remove deleted files from index
        all_indexed = self.index.get_all_indexed_paths()
        stale_paths = all_indexed - current_relative_paths
        removed_count = self.index.remove_files(stale_paths)

        # Step 4: Parse changed/new files concurrently
        indexed_count = 0
        parse_errors = 0

        def _parse_task(item: Tuple[Path, str, os.stat_result, str]) -> Tuple[str, str, int, int, str, list, list, list]:
            abs_p, rel_p, stat_info, h = item
            lang = detect_language(abs_p)
            try:
                content = abs_p.read_text(encoding="utf-8", errors="replace")
                symbols, imports, refs = extract_symbols_and_imports(content, rel_p, lang)
            except Exception as e:
                # Per-file failure must never abort the whole scan
                return rel_p, lang, stat_info.st_size, stat_info.st_mtime_ns, h, [], [], []
            return rel_p, lang, stat_info.st_size, stat_info.st_mtime_ns, h, symbols, imports, refs

        if files_to_parse:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_parse_task, item): item for item in files_to_parse}
                for f in concurrent.futures.as_completed(futures):
                    try:
                        rel_p, lang, size, mtime_ns, h, syms, imps, refs = f.result()
                        self.index.update_file_record(rel_p, lang, size, mtime_ns, h, syms, imps, refs)
                        indexed_count += 1
                        if progress_callback:
                            try:
                                progress_callback(indexed_count, len(files_to_parse), rel_p)
                            except Exception:
                                pass
                    except Exception as e:
                        parse_errors += 1
                        self.logger(f"[Scanner] Error indexing file: {e}")

        summary = {
            "status": "complete",
            "total_discovered": total_discovered,
            "unchanged_files": unchanged_count,
            "indexed_files": indexed_count,
            "removed_files": removed_count,
            "skipped_files": skipped_count,
            "parse_errors": parse_errors,
        }
        return summary
