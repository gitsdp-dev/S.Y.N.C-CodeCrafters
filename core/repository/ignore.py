"""
core/repository/ignore.py — Gitignore and default ignore rule engine.

Parses .gitignore patterns and enforces strict security and repository exclusion
rules for files and directories.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# Default directories excluded from scanning and indexing
DEFAULT_IGNORE_DIRS: set[str] = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "htmlcov",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".eggs",
    ".aws",
    ".ssh",
    ".gnupg",
    ".sync_backup",
    ".jarvis_backup",
}

# Default file patterns excluded from scanning and indexing
DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.obj",
    "*.o",
    "*.a",
    "*.lib",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.sqlite-journal",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "api_keys.json",
    "api_keys.json.*",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pkcs12",
    "*.p12",
    "*.pfx",
    "*.crt",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    "*token*",
    "*secret*",
    "*credential*",
    "sync_repo_index.db*",
)


def _pattern_to_regex(pattern: str) -> Tuple[bool, bool, re.Pattern]:
    """Compile a gitignore pattern into a regex tuple: (is_negation, dir_only, regex)."""
    is_negation = False
    if pattern.startswith("!"):
        is_negation = True
        pattern = pattern[1:]

    dir_only = False
    if pattern.endswith("/"):
        dir_only = True
        pattern = pattern[:-1]

    # Handle leading slash (anchored to root)
    anchored = False
    if pattern.startswith("/"):
        anchored = True
        pattern = pattern[1:]
    elif "/" in pattern:
        # If there is a slash anywhere inside the pattern, it's anchored relative to root
        anchored = True

    # Translate glob wildcards to regex
    i, n = 0, len(pattern)
    res: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # '**'
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    res.append("(?:.+/)?")
                else:
                    res.append(".*")
            else:
                res.append("[^/]*")
                i += 1
        elif c == "?":
            res.append("[^/]")
            i += 1
        elif c in r"\[]{}()^$.|+":
            res.append("\\" + c)
            i += 1
        else:
            res.append(c)
            i += 1

    pattern_regex = "".join(res)
    if anchored:
        regex_str = f"^{pattern_regex}$"
    else:
        regex_str = f"(?:^|/){pattern_regex}$"

    return is_negation, dir_only, re.compile(regex_str, re.IGNORECASE)


class IgnoreMatcher:
    """Matches relative paths against default ignores and repository .gitignore rules."""

    def __init__(self, root: Path, custom_patterns: Optional[Iterable[str]] = None):
        self.root = root.resolve()
        self._compiled_rules: List[Tuple[bool, bool, re.Pattern]] = []
        self._load_defaults()
        if custom_patterns:
            for pat in custom_patterns:
                self.add_pattern(pat)
        self._load_gitignore()

    def _load_defaults(self) -> None:
        for pat in DEFAULT_IGNORE_PATTERNS:
            self.add_pattern(pat)
        for d in DEFAULT_IGNORE_DIRS:
            self.add_pattern(f"{d}/")

    def _load_gitignore(self) -> None:
        gi_file = self.root / ".gitignore"
        if not gi_file.is_file():
            return
        try:
            content = gi_file.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self.add_pattern(line)
        except Exception:
            pass

    def add_pattern(self, pattern: str) -> None:
        """Add a single gitignore-style pattern."""
        p = pattern.strip()
        if not p or p.startswith("#"):
            return
        try:
            rule = _pattern_to_regex(p)
            self._compiled_rules.append(rule)
        except re.error:
            pass

    def is_ignored(self, path: Path | str, is_dir: Optional[bool] = None) -> bool:
        """Check whether a given path (absolute or relative to root) is ignored."""
        p = Path(path)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(self.root)
            except ValueError:
                # Path is outside root — always ignore/reject
                return True
        else:
            rel = p

        rel_posix = rel.as_posix().strip("/")
        if not rel_posix:
            return False

        if is_dir is None:
            full_path = self.root / rel
            try:
                is_dir = full_path.is_dir()
            except OSError:
                is_dir = False

        # Fast path for directory segments matching default ignore dirs
        parts = rel_posix.split("/")
        for segment in (parts if is_dir else parts[:-1]):
            if segment in DEFAULT_IGNORE_DIRS:
                return True

        ignored = False
        for is_negation, dir_only, rx in self._compiled_rules:
            if dir_only and not is_dir:
                continue
            if rx.search(rel_posix):
                ignored = not is_negation

        return ignored
