"""
core/repository/index.py — Persistent CPU-efficient SQLite repository index.

Stores files, symbols, imports, references, and diagnostics in a dedicated
SQLite database outside source-controlled folders.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


SCHEMA_VERSION = 1


def _get_cache_dir() -> Path:
    """Return the global index cache directory outside source control."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent.parent
    cache_dir = base / "cache" / "repo_indices"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_db_path_for_repo(repo_root: Path) -> Path:
    """Derive deterministic SQLite DB file path for a given repository root."""
    normalized = repo_root.resolve().as_posix().lower()
    h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    sanitized_name = "".join(c if c.isalnum() else "_" for c in repo_root.name)[:24]
    return _get_cache_dir() / f"repo_{sanitized_name}_{h}.db"


class RepositoryIndex:
    """Thread-safe SQLite index for repository files, symbols, imports, and references."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.db_path = get_db_path_for_repo(self.repo_root)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                );
            """)

            cur.execute("SELECT version FROM schema_version LIMIT 1;")
            row = cur.fetchone()
            if row is None:
                cur.execute("INSERT INTO schema_version (version) VALUES (?);", (SCHEMA_VERSION,))
            elif row["version"] < SCHEMA_VERSION:
                # Handle future migrations if schema evolves
                cur.execute("UPDATE schema_version SET version = ?;", (SCHEMA_VERSION,))

            # 1. files table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    language TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    indexed_at REAL NOT NULL
                );
            """)

            # 2. symbols table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    signature TEXT,
                    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
                );
            """)

            # 3. imports table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    imported_path TEXT NOT NULL,
                    import_kind TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
                );
            """)

            # 4. references table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS references_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
                );
            """)

            # 5. diagnostics table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    message TEXT NOT NULL
                );
            """)

            # Search performance indices
            cur.execute("CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_path);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_imports_imported ON imports(imported_path);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_refs_name ON references_index(symbol_name);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_refs_file ON references_index(file_path);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_diag_file ON diagnostics(file_path);")

            conn.commit()

    def get_indexed_file(self, rel_path: str) -> Optional[dict]:
        """Fetch metadata for an indexed file."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT path, language, size, mtime_ns, sha256, indexed_at FROM files WHERE path = ?;", (rel_path,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return None

    def get_all_indexed_paths(self) -> Set[str]:
        """Fetch all file paths currently recorded in the index."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT path FROM files;")
            return {row["path"] for row in cur.fetchall()}

    def update_file_record(
        self,
        rel_path: str,
        language: str,
        size: int,
        mtime_ns: int,
        sha256_hash: str,
        symbols: List[dict],
        imports: List[dict],
        references: List[dict],
    ) -> None:
        """Atomically upsert file metadata and replace its symbols, imports, and references."""
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.cursor()

            # Remove existing children first to preserve clean state
            cur.execute("DELETE FROM symbols WHERE file_path = ?;", (rel_path,))
            cur.execute("DELETE FROM imports WHERE file_path = ?;", (rel_path,))
            cur.execute("DELETE FROM references_index WHERE file_path = ?;", (rel_path,))

            # Upsert file entry
            cur.execute("""
                INSERT INTO files (path, language, size, mtime_ns, sha256, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    language = excluded.language,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    sha256 = excluded.sha256,
                    indexed_at = excluded.indexed_at;
            """, (rel_path, language, size, mtime_ns, sha256_hash, now))

            # Batch insert symbols
            if symbols:
                cur.executemany("""
                    INSERT INTO symbols (file_path, name, kind, line_start, line_end, signature)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, [
                    (
                        rel_path,
                        s["name"],
                        s["kind"],
                        s["line_start"],
                        s["line_end"],
                        s.get("signature", ""),
                    )
                    for s in symbols
                ])

            # Batch insert imports
            if imports:
                cur.executemany("""
                    INSERT INTO imports (file_path, imported_path, import_kind, line)
                    VALUES (?, ?, ?, ?);
                """, [
                    (rel_path, imp["imported_path"], imp["import_kind"], imp.get("line", 1))
                    for imp in imports
                ])

            # Batch insert references
            if references:
                cur.executemany("""
                    INSERT INTO references_index (file_path, symbol_name, line)
                    VALUES (?, ?, ?);
                """, [
                    (rel_path, ref["symbol_name"], ref.get("line", 1))
                    for ref in references
                ])

            conn.commit()

    def touch_file_mtime(self, rel_path: str, mtime_ns: int) -> None:
        """Update mtime for an unchanged file without altering its symbols or hash."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE files SET mtime_ns = ?, indexed_at = ? WHERE path = ?;", (mtime_ns, time.time(), rel_path))
            conn.commit()

    def remove_files(self, rel_paths: Iterable[str]) -> int:
        """Remove files and all associated relations from the index."""
        paths_list = list(rel_paths)
        if not paths_list:
            return 0
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            # Batch in chunks of 500
            chunk_size = 500
            deleted = 0
            for i in range(0, len(paths_list), chunk_size):
                chunk = paths_list[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cur.execute(f"DELETE FROM symbols WHERE file_path IN ({placeholders});", chunk)
                cur.execute(f"DELETE FROM imports WHERE file_path IN ({placeholders});", chunk)
                cur.execute(f"DELETE FROM references_index WHERE file_path IN ({placeholders});", chunk)
                cur.execute(f"DELETE FROM diagnostics WHERE file_path IN ({placeholders});", chunk)
                cur.execute(f"DELETE FROM files WHERE path IN ({placeholders});", chunk)
                deleted += cur.rowcount
            conn.commit()
            return deleted

    def find_symbols(
        self,
        name_query: str,
        kind: Optional[str] = None,
        exact: bool = False,
        limit: int = 50,
    ) -> List[dict]:
        """Search symbols by name and optional kind, ordered deterministically."""
        q = name_query.strip()
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            sql = "SELECT file_path, name, kind, line_start, line_end, signature FROM symbols WHERE "
            params: list[Any] = []

            if exact:
                sql += "name = ? "
                params.append(q)
            else:
                sql += "(name LIKE ? OR name LIKE ?) "
                params.extend([f"{q}%", f"%.{q}%"])

            if kind:
                sql += "AND kind = ? "
                params.append(kind.lower())

            sql += "ORDER BY file_path ASC, line_start ASC LIMIT ?;"
            params.append(limit)

            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def find_references(
        self,
        symbol_name: str,
        file_path: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """Find occurrences of a symbol name across the codebase."""
        sym = symbol_name.strip()
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            sql = "SELECT file_path, symbol_name, line FROM references_index WHERE symbol_name = ? "
            params: list[Any] = [sym]

            if file_path:
                sql += "AND file_path = ? "
                params.append(file_path)

            sql += "ORDER BY file_path ASC, line ASC LIMIT ?;"
            params.append(limit)

            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_imports(self, file_path: Optional[str] = None, limit: int = 100) -> List[dict]:
        """List module imports from the index."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            if file_path:
                cur.execute("""
                    SELECT file_path, imported_path, import_kind, line
                    FROM imports WHERE file_path = ?
                    ORDER BY line ASC LIMIT ?;
                """, (file_path, limit))
            else:
                cur.execute("""
                    SELECT file_path, imported_path, import_kind, line
                    FROM imports
                    ORDER BY file_path ASC, line ASC LIMIT ?;
                """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def record_diagnostics(self, diagnostics: List[dict]) -> None:
        """Store diagnostics records."""
        if not diagnostics:
            return
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO diagnostics (file_path, tool, severity, line, message)
                VALUES (?, ?, ?, ?, ?);
            """, [
                (
                    d["file_path"],
                    d.get("tool", "unknown"),
                    d.get("severity", "error"),
                    d.get("line", 1),
                    d.get("message", ""),
                )
                for d in diagnostics
            ])
            conn.commit()

    def get_diagnostics(self, file_path: Optional[str] = None, limit: int = 50) -> List[dict]:
        """Fetch recorded diagnostics."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            if file_path:
                cur.execute("""
                    SELECT file_path, tool, severity, line, message
                    FROM diagnostics WHERE file_path = ?
                    ORDER BY line ASC LIMIT ?;
                """, (file_path, limit))
            else:
                cur.execute("""
                    SELECT file_path, tool, severity, line, message
                    FROM diagnostics
                    ORDER BY file_path ASC, line ASC LIMIT ?;
                """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def clear_diagnostics(self, file_path: Optional[str] = None) -> None:
        """Clear diagnostics for a file or the whole repository."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            if file_path:
                cur.execute("DELETE FROM diagnostics WHERE file_path = ?;", (file_path,))
            else:
                cur.execute("DELETE FROM diagnostics;")
            conn.commit()

    def get_summary(self) -> dict:
        """Generate repository index statistical summary."""
        with self._lock, self._connect() as conn:
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) AS total_files, SUM(size) AS total_size FROM files;")
            row_files = cur.fetchone()
            total_files = row_files["total_files"] if row_files else 0
            total_size = row_files["total_size"] if row_files and row_files["total_size"] else 0

            cur.execute("SELECT language, COUNT(*) AS count FROM files GROUP BY language ORDER BY count DESC;")
            lang_counts = {r["language"]: r["count"] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS total_symbols FROM symbols;")
            row_sym = cur.fetchone()
            total_symbols = row_sym["total_symbols"] if row_sym else 0

            cur.execute("SELECT path, size FROM files ORDER BY size DESC LIMIT 5;")
            largest_files = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) AS total_diag FROM diagnostics;")
            row_diag = cur.fetchone()
            total_diag = row_diag["total_diag"] if row_diag else 0

            return {
                "total_files": total_files,
                "total_bytes": total_size,
                "language_distribution": lang_counts,
                "total_symbols": total_symbols,
                "largest_files": largest_files,
                "diagnostic_count": total_diag,
            }
