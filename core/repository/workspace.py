"""
core/repository/workspace.py — Workspace isolation and security boundary.

Enforces workspace-scoped file operations, prevents reads and writes outside
the active repository root, rejects path traversal, Windows drive changes, and
symlink escapes.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional, Union

from core.repository.ignore import IgnoreMatcher
from memory import config_manager


def is_secret_or_credential_file(path: Union[str, Path]) -> bool:
    """Return True if path represents a secret, credential, key, or sensitive file."""
    p = Path(path)
    filename = p.name.lower()
    parts = [seg.lower() for seg in p.parts]
    if (
        "api_keys.json" in filename
        or filename.startswith(".env")
        or filename.endswith((".pem", ".key", ".pkcs12", ".p12", ".pfx", ".crt"))
        or filename.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"))
        or any(seg in (".aws", ".ssh", ".gnupg") for seg in parts)
        or "secret" in filename
        or "credential" in filename
        or "token" in filename
    ):
        return True
    return False


_SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("GitHub Token", re.compile(r"\b(ghp_[0-9a-zA-Z]{36}|github_pat_[0-9a-zA-Z_]{82})\b")),
    ("Slack Token", re.compile(r"\b(xox[baprs]-[0-9a-zA-Z]{10,48})\b")),
    ("Google API Key", re.compile(r"\b(AIza[0-9A-Za-z\-_]{35})\b")),
    ("Private Key Header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Generic Hardcoded Secret", re.compile(r"""(?i)(?:api_key|apikey|secret|password|passwd|auth_token|access_token|private_key)\s*[:=]\s*["']([^"'\s]{8,})["']""")),
]


def detect_hardcoded_secrets(text: str, file_path: str = "") -> list[dict]:
    """
    Scan source text for hardcoded API keys, tokens, or passwords.
    Returns findings with file_path and line number, strictly REDACTING the secret value.
    """
    findings: list[dict] = []
    lines = text.splitlines()
    for line_idx, line in enumerate(lines, 1):
        for rule_name, pat in _SECRET_PATTERNS:
            match = pat.search(line)
            if match:
                findings.append({
                    "file_path": file_path,
                    "line": line_idx,
                    "rule": rule_name,
                    "severity": "security_risk",
                    "message": f"Hardcoded credential/secret detected ({rule_name}) at line {line_idx}. Value is redacted.",
                })
                break
    return findings


def redact_secrets(text: str) -> str:
    """Redact secrets so no raw key or token is echoed in outputs."""
    redacted = text
    for rule_name, pat in _SECRET_PATTERNS:
        if rule_name == "Generic Hardcoded Secret":
            redacted = pat.sub(r'secret_key = "[REDACTED_SECRET]"', redacted)
        else:
            redacted = pat.sub("[REDACTED_SECRET]", redacted)
    return redacted


class WorkspaceError(Exception):
    """Base exception for workspace operations."""
    pass


class WorkspaceSecurityError(WorkspaceError):
    """Raised when an operation attempts to breach workspace boundaries."""
    pass


class WorkspaceFileNotFoundError(WorkspaceError):
    """Raised when a requested file inside the workspace cannot be found."""
    pass


class NoActiveWorkspaceError(WorkspaceError):
    """Raised when an operation requires an active repository but none is set."""
    pass


class WorkspaceManager:
    """Manages the active repository root and validates path isolation."""

    def __init__(self, root: Optional[Union[str, Path]] = None):
        self._root: Optional[Path] = None
        self._ignore_matcher: Optional[IgnoreMatcher] = None
        self._is_git: bool = False

        if root:
            self.set_root(root)
        else:
            configured = config_manager.get_active_repository()
            if configured:
                try:
                    self.set_root(configured)
                except Exception:
                    self._root = None

    @property
    def root(self) -> Optional[Path]:
        """Return the resolved active repository root, or None if unset."""
        if self._root is not None and self._root.is_dir():
            return self._root
        configured = config_manager.get_active_repository()
        if configured:
            p = Path(configured).resolve()
            if p.is_dir():
                self._root = p
                self._ignore_matcher = IgnoreMatcher(p)
                self._is_git = (p / ".git").is_dir()
                return self._root
        return None

    @property
    def is_git(self) -> bool:
        """Whether the active workspace root contains a .git repository."""
        r = self.root
        if not r:
            return False
        return (r / ".git").is_dir()

    @property
    def ignore_matcher(self) -> IgnoreMatcher:
        """Return the active IgnoreMatcher instance."""
        r = self.root
        if not r:
            raise NoActiveWorkspaceError("No active repository selected.")
        if self._ignore_matcher is None or self._ignore_matcher.root != r:
            self._ignore_matcher = IgnoreMatcher(r)
        return self._ignore_matcher

    def set_root(self, path: Union[str, Path]) -> Path:
        """Set and persist the active repository root after strict validation."""
        if not path:
            raise WorkspaceError("Repository path cannot be empty.")

        raw_path = Path(str(path).strip())
        if not raw_path.is_absolute():
            # If relative, resolve against current working directory
            resolved = raw_path.resolve()
        else:
            resolved = raw_path.resolve()

        if not resolved.exists():
            raise WorkspaceFileNotFoundError(f"Repository directory does not exist: {resolved}")
        if not resolved.is_dir():
            raise WorkspaceError(f"Repository path is not a directory: {resolved}")

        # Check read/access permissions
        try:
            next(resolved.iterdir(), None)
        except PermissionError as e:
            raise WorkspaceSecurityError(f"Repository directory is inaccessible: {e}")

        self._root = resolved
        self._is_git = (resolved / ".git").is_dir()
        self._ignore_matcher = IgnoreMatcher(resolved)

        # Persist via configuration manager without touching secrets
        config_manager.save_active_repository(str(resolved))
        return self._root

    def clear_root(self) -> None:
        """Clear the active repository root."""
        self._root = None
        self._ignore_matcher = None
        self._is_git = False
        config_manager.save_active_repository("")

    def resolve_safe_path(
        self,
        rel_or_abs: Union[str, Path],
        must_exist: bool = False,
        require_file: bool = False,
        allow_secret: bool = False,
    ) -> Path:
        """
        Safely resolve a path relative to the active repository root.

        Guarantees:
        - Resolves full real path.
        - Blocks path traversal (../).
        - Blocks Windows drive changes.
        - Blocks symlinks escaping the repository root.
        - Excludes sensitive credential/key files unless explicitly permitted.
        """
        active_root = self.root
        if not active_root:
            raise NoActiveWorkspaceError("No active repository selected. Please configure a repository root first.")

        raw_str = str(rel_or_abs or "").strip()
        if not raw_str:
            raise WorkspaceError("Target path cannot be empty.")

        given_path = Path(raw_str)

        # On Windows, explicitly check drive letter compatibility
        if sys.platform == "win32" and given_path.is_absolute():
            root_drive = active_root.drive.upper()
            given_drive = given_path.drive.upper()
            if given_drive and root_drive and given_drive != root_drive:
                raise WorkspaceSecurityError(
                    f"Path drive '{given_drive}' does not match repository drive '{root_drive}'."
                )

        if given_path.is_absolute():
            resolved = given_path.resolve()
        else:
            resolved = (active_root / given_path).resolve()

        # Strict containment check
        try:
            rel = resolved.relative_to(active_root)
        except ValueError:
            raise WorkspaceSecurityError(
                f"Access denied: path '{raw_str}' escapes repository root '{active_root}'."
            )

        # Symlink check: verify the target does not escape
        try:
            real_path = Path(os.path.realpath(str(resolved)))
            real_path.relative_to(active_root)
        except (ValueError, OSError):
            raise WorkspaceSecurityError(
                f"Access denied: symlink '{raw_str}' resolves outside repository root."
            )

        # Block direct access to secrets / api_keys / certificates / credentials
        if not allow_secret:
            if is_secret_or_credential_file(resolved):
                raise WorkspaceSecurityError(
                    f"Access to credential or secret file '{resolved.name}' is prohibited by Repository Inspection & Audit Rules."
                )

        if must_exist and not resolved.exists():
            raise WorkspaceFileNotFoundError(f"Path does not exist: {rel.as_posix()}")

        if require_file:
            if not resolved.exists():
                raise WorkspaceFileNotFoundError(f"File does not exist: {rel.as_posix()}")
            if not resolved.is_file():
                raise WorkspaceError(f"Path is not a regular file: {rel.as_posix()}")

        return resolved

    def is_binary_file(self, path: Path) -> bool:
        """Determine if a file is binary by inspecting its initial byte chunk."""
        if not path.is_file():
            return False
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
            if not chunk:
                return False
            # Standard null-byte check for binary detection
            if b"\x00" in chunk:
                return True
            # Attempt decoding as UTF-8
            try:
                chunk.decode("utf-8")
                return False
            except UnicodeDecodeError:
                # High proportion of non-ascii control chars indicates binary
                control_chars = sum(1 for byte in chunk if byte < 32 and byte not in (9, 10, 13))
                return (control_chars / len(chunk)) > 0.3
        except (OSError, PermissionError):
            return True

    def read_file_text(
        self,
        rel_or_abs: Union[str, Path],
        max_bytes: Optional[int] = None,
    ) -> tuple[str, dict]:
        """
        Read a file safely inside the workspace.

        Returns (content_text, metadata_dict).
        """
        target = self.resolve_safe_path(rel_or_abs, must_exist=True, require_file=True)
        stat = target.stat()

        limit = max_bytes if max_bytes is not None else config_manager.get_repository_max_file_bytes()
        if stat.st_size > limit:
            raise WorkspaceError(
                f"File '{target.name}' size ({stat.st_size:,} bytes) exceeds configured limit ({limit:,} bytes)."
            )

        if self.is_binary_file(target):
            raise WorkspaceError(f"Cannot read binary file '{target.name}' as text.")

        try:
            raw_bytes = target.read_bytes()
        except OSError as e:
            raise WorkspaceError(f"Could not read '{target.name}': {e}")

        # Try UTF-8 first, fallback to utf-8 with replace
        encoding = "utf-8"
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "utf-8-replace"
            text = raw_bytes.decode("utf-8", errors="replace")

        lines = text.splitlines()
        metadata = {
            "path": target.relative_to(self.root).as_posix(),
            "absolute_path": str(target),
            "size": stat.st_size,
            "line_count": len(lines),
            "encoding": encoding,
            "mtime_ns": stat.st_mtime_ns,
        }
        return text, metadata

    def write_file_text(
        self,
        rel_or_abs: Union[str, Path],
        content: str,
        overwrite: bool = True,
    ) -> dict:
        """
        Safely write text to a file inside the repository root.
        Creates parent directories if necessary.
        """
        target = self.resolve_safe_path(rel_or_abs, must_exist=False, require_file=False)
        if target.exists() and not overwrite:
            raise WorkspaceError(f"File '{target.name}' already exists and overwrite=False.")

        if target.exists() and target.is_dir():
            raise WorkspaceError(f"Cannot overwrite directory '{target.name}' with a file.")

        target.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = content.encode("utf-8")

        limit = config_manager.get_repository_max_file_bytes()
        if len(raw_bytes) > limit:
            raise WorkspaceError(
                f"Write size ({len(raw_bytes):,} bytes) exceeds repository limit ({limit:,} bytes)."
            )

        # Atomic write via temporary file in the same directory
        tmp_target = target.with_name(f".{target.name}.tmp_{os.getpid()}")
        try:
            tmp_target.write_bytes(raw_bytes)
            os.replace(tmp_target, target)
        except OSError as e:
            if tmp_target.exists():
                try:
                    tmp_target.unlink()
                except OSError:
                    pass
            raise WorkspaceError(f"Failed to write file '{target.name}': {e}")

        stat = target.stat()
        return {
            "path": target.relative_to(self.root).as_posix(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }


# Global singleton instance
_workspace_instance: Optional[WorkspaceManager] = None


def get_workspace() -> WorkspaceManager:
    """Return the global WorkspaceManager instance."""
    global _workspace_instance
    if _workspace_instance is None:
        _workspace_instance = WorkspaceManager()
    return _workspace_instance
