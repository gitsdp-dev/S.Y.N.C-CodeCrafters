"""
core/repository/patches.py — Unified patch engine with preview, atomic application,
and multi-step rollback snapshots.

Enforces workspace boundaries, exact hunk context matching, and approval gates.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import confirm as confirm_gate
from core.repository.workspace import (
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceManager,
    WorkspaceSecurityError,
    get_workspace,
)
from memory import config_manager


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class ParsedDiff:
    file_path: str
    hunks: list[Hunk] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0


@dataclass
class Snapshot:
    id: str
    action: str  # 'modify' | 'create' | 'delete' | 'rename'
    file_path: str
    old_path: Optional[str] = None
    previous_bytes: Optional[bytes] = None
    previous_mtime_ns: Optional[int] = None
    created_at: float = field(default_factory=time.time)


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    """Parse a single-file unified diff text into structured hunks."""
    lines = diff_text.strip().splitlines()
    if not lines:
        raise WorkspaceError("Empty diff text.")

    file_path: Optional[str] = None
    hunks: list[Hunk] = []
    current_hunk: Optional[Hunk] = None
    additions = 0
    deletions = 0

    hunk_header_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

    for line in lines:
        if line.startswith("--- "):
            continue
        elif line.startswith("+++ "):
            # Extract target file name, strip a/ or b/ prefixes
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            file_path = target
            continue

        m = hunk_header_re.match(line)
        if m:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            current_hunk = Hunk(old_start, old_count, new_start, new_count)
            hunks.append(current_hunk)
            continue

        if current_hunk is not None:
            if line.startswith("+"):
                additions += 1
                current_hunk.lines.append(line)
            elif line.startswith("-"):
                deletions += 1
                current_hunk.lines.append(line)
            elif line.startswith(" "):
                current_hunk.lines.append(line)
            elif line.startswith("\\"):
                # "\ No newline at end of file"
                continue
            elif not line.strip():
                # Treat empty line as context with single space
                current_hunk.lines.append(" ")

    if not file_path:
        raise WorkspaceError("Could not determine target file from diff headers ('+++ b/filepath').")
    if not hunks:
        raise WorkspaceError(f"No valid hunks (@@ ... @@) found in diff for '{file_path}'.")

    return ParsedDiff(file_path=file_path, hunks=hunks, additions=additions, deletions=deletions)


def apply_hunks_to_lines(original_lines: list[str], hunks: list[Hunk]) -> list[str]:
    """Apply parsed hunks against list of file lines, validating context exactly."""
    result: list[str] = []
    cursor = 0  # 0-based line index in original_lines

    for hunk_idx, hunk in enumerate(hunks, 1):
        target_line = max(0, hunk.old_start - 1)

        # Copy lines before the hunk
        while cursor < target_line and cursor < len(original_lines):
            result.append(original_lines[cursor])
            cursor += 1

        for hline in hunk.lines:
            indicator = hline[0] if hline else " "
            content = hline[1:]

            if indicator == " ":
                if cursor >= len(original_lines):
                    raise WorkspaceError(
                        f"Hunk #{hunk_idx} failed: unexpected end of file while expecting context '{content}'."
                    )
                actual = original_lines[cursor]
                if actual != content:
                    raise WorkspaceError(
                        f"Hunk #{hunk_idx} context mismatch at line {cursor + 1}:\n"
                        f"  Expected: '{content}'\n"
                        f"  Actual:   '{actual}'"
                    )
                result.append(actual)
                cursor += 1
            elif indicator == "-":
                if cursor >= len(original_lines):
                    raise WorkspaceError(
                        f"Hunk #{hunk_idx} failed: unexpected end of file while expecting to remove '{content}'."
                    )
                actual = original_lines[cursor]
                if actual != content:
                    raise WorkspaceError(
                        f"Hunk #{hunk_idx} deletion mismatch at line {cursor + 1}:\n"
                        f"  Expected: '{content}'\n"
                        f"  Actual:   '{actual}'"
                    )
                cursor += 1
            elif indicator == "+":
                result.append(content)

    # Copy any remaining lines
    while cursor < len(original_lines):
        result.append(original_lines[cursor])
        cursor += 1

    return result


class PatchManager:
    """Coordinates patch previewing, atomic application, and rollback snapshots."""

    def __init__(self, workspace: Optional[WorkspaceManager] = None):
        self.workspace = workspace or get_workspace()
        self._snapshots: list[Snapshot] = []

    def _should_record_audit(self) -> bool:
        """Return True only if this patch is applied to a real local workspace folder,
        and not an internal test runner or temporary mock directory."""
        if not self.workspace or not self.workspace.root:
            return False
        if getattr(self.workspace, "_is_test", False):
            return False
        try:
            import tempfile
            root = Path(self.workspace.root).resolve()
            temp_dir = Path(tempfile.gettempdir()).resolve()
            if root == temp_dir or temp_dir in root.parents:
                return False
        except Exception:
            pass
        return True

    def _save_repo_sync_backup(
        self,
        action: str,
        rel_path: str,
        snap_id: str,
        previous_bytes: Optional[bytes] = None,
        diff_text: str = "",
        diff_summary: str = "",
    ) -> None:
        """Save a backup copy and append to .sync_backup/audit.jsonl in the local repository."""
        if not self._should_record_audit() or not self.workspace.root:
            return
        try:
            root = Path(self.workspace.root)
            bdir = root / ".sync_backup"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            # If we have previous bytes, save a copy in .sync_backup/<stamp>/
            if previous_bytes is not None:
                dest = bdir / stamp / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(previous_bytes)

            bdir.mkdir(parents=True, exist_ok=True)
            audit_file = bdir / "audit.jsonl"
            from datetime import datetime
            record = {
                "id": snap_id,
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "file_path": rel_path,
                "stamp": stamp,
                "diff_summary": diff_summary,
                "diff_text": diff_text[:500] if diff_text else "",
            }
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def preview_patch(self, diff_text: str) -> dict:
        """
        Preview a unified patch without modifying files on disk.
        Validates hunk context against the current file.
        """
        parsed = parse_unified_diff(diff_text)
        target_path = self.workspace.resolve_safe_path(parsed.file_path, must_exist=True, require_file=True)

        content, meta = self.workspace.read_file_text(target_path)
        original_lines = content.splitlines()

        # Dry run application
        new_lines = apply_hunks_to_lines(original_lines, parsed.hunks)

        return {
            "status": "preview_ok",
            "file": meta["path"],
            "hunks_count": len(parsed.hunks),
            "additions": parsed.additions,
            "deletions": parsed.deletions,
            "original_lines": len(original_lines),
            "resulting_lines": len(new_lines),
            "preview_diff": diff_text.strip(),
        }

    def apply_patch(
        self,
        diff_text: str,
        auto_approve: bool = False,
    ) -> dict:
        """
        Apply a patch atomically to a workspace file.
        Saves a rollback snapshot prior to writing.
        """
        parsed = parse_unified_diff(diff_text)
        target_path = self.workspace.resolve_safe_path(parsed.file_path, must_exist=True, require_file=True)

        content, meta = self.workspace.read_file_text(target_path)
        original_lines = content.splitlines()

        # Validate hunks match before writing anything
        new_lines = apply_hunks_to_lines(original_lines, parsed.hunks)
        new_content = "\n".join(new_lines)
        if content.endswith("\n"):
            new_content += "\n"

        # Check approval gate if confirmation is configured
        if config_manager.get_engineering_confirm_patches() and not auto_approve:
            req_msg = confirm_gate.request(
                key=f"patch_{target_path.name}",
                title=f"Apply patch to {meta['path']}",
                detail=f"+{parsed.additions} / -{parsed.deletions} lines across {len(parsed.hunks)} hunk(s)",
                run=lambda: self._execute_patch_apply(target_path, meta["path"], new_content, diff_text, parsed.additions, parsed.deletions),
            )
            return {
                "status": "approval_required",
                "file": meta["path"],
                "message": req_msg,
            }

        return self._execute_patch_apply(target_path, meta["path"], new_content, diff_text, parsed.additions, parsed.deletions)

    def _execute_patch_apply(
        self,
        target_path: Path,
        rel_path: str,
        new_content: str,
        diff_text: str = "",
        additions: int = 0,
        deletions: int = 0,
    ) -> dict:
        """Internal worker executing patch application and recording snapshot."""
        st = target_path.stat()
        snap_id = f"snap_{int(time.time())}_{uuid.uuid4().hex[:6]}"

        snapshot = Snapshot(
            id=snap_id,
            action="modify",
            file_path=rel_path,
            previous_bytes=target_path.read_bytes(),
            previous_mtime_ns=st.st_mtime_ns,
        )
        self._snapshots.append(snapshot)

        # Write new content safely
        write_meta = self.workspace.write_file_text(target_path, new_content, overwrite=True)

        # Record real-time audit entry (only for real local folders)
        if self._should_record_audit():
            try:
                from core.repository.audit import get_audit_logger
                from core.repository.ide import get_preferred_ide
                get_audit_logger().record_change(
                    file_path=rel_path,
                    action="modify",
                    diff_text=diff_text,
                    additions=additions,
                    deletions=deletions,
                    ide=get_preferred_ide() or "",
                )
            except Exception:
                pass
            self._save_repo_sync_backup(
                action="modify",
                rel_path=rel_path,
                snap_id=snap_id,
                previous_bytes=snapshot.previous_bytes,
                diff_text=diff_text,
                diff_summary=f"+{additions} / -{deletions} lines",
            )

        return {
            "status": "applied",
            "file": rel_path,
            "snapshot_id": snap_id,
            "size": write_meta["size"],
        }

    def rollback_patch(self, snapshot_id: Optional[str] = None) -> dict:
        """
        Roll back a previous modification, file creation, deletion, or rename.
        If snapshot_id is omitted, rolls back the most recent change.
        """
        if not self._snapshots:
            return {"status": "error", "error": "No rollback snapshots available."}

        target_snap: Optional[Snapshot] = None
        if snapshot_id:
            for s in reversed(self._snapshots):
                if s.id == snapshot_id:
                    target_snap = s
                    break
            if not target_snap:
                return {"status": "error", "error": f"Snapshot '{snapshot_id}' not found."}
            self._snapshots.remove(target_snap)
        else:
            target_snap = self._snapshots.pop()

        root = self.workspace.root
        if not root:
            return {"status": "error", "error": "No active workspace configured."}

        file_path = root / target_snap.file_path

        try:
            self._save_repo_sync_backup(
                action="rollback",
                rel_path=target_snap.file_path,
                snap_id=target_snap.id,
                diff_summary=f"Rolled back {target_snap.action}",
            )
            if target_snap.action == "modify":
                if target_snap.previous_bytes is not None:
                    file_path.write_bytes(target_snap.previous_bytes)
                    if target_snap.previous_mtime_ns:
                        try:
                            os.utime(file_path, ns=(target_snap.previous_mtime_ns, target_snap.previous_mtime_ns))
                        except OSError:
                            pass
                    return {
                        "status": "rolled_back",
                        "snapshot_id": target_snap.id,
                        "action": "restored_modified_file",
                        "file": target_snap.file_path,
                    }

            elif target_snap.action == "create":
                if file_path.is_file():
                    file_path.unlink()
                    return {
                        "status": "rolled_back",
                        "snapshot_id": target_snap.id,
                        "action": "deleted_created_file",
                        "file": target_snap.file_path,
                    }

            elif target_snap.action == "delete":
                if target_snap.previous_bytes is not None:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_bytes(target_snap.previous_bytes)
                    return {
                        "status": "rolled_back",
                        "snapshot_id": target_snap.id,
                        "action": "restored_deleted_file",
                        "file": target_snap.file_path,
                    }

            elif target_snap.action == "rename":
                if target_snap.old_path:
                    old_full = root / target_snap.old_path
                    if file_path.exists():
                        file_path.rename(old_full)
                        return {
                            "status": "rolled_back",
                            "snapshot_id": target_snap.id,
                            "action": "restored_renamed_file",
                            "from": target_snap.file_path,
                            "to": target_snap.old_path,
                        }

        except Exception as e:
            return {"status": "error", "error": f"Rollback failed: {e}"}

        return {"status": "error", "error": "Rollback could not be performed."}

    def create_file(self, path: str, content: str, auto_approve: bool = False) -> dict:
        """Create a new file with approval gating and rollback tracking."""
        target = self.workspace.resolve_safe_path(path, must_exist=False)
        if target.exists():
            return {"status": "error", "error": f"File '{path}' already exists."}

        rel = target.relative_to(self.workspace.root).as_posix()

        def _do_create():
            meta = self.workspace.write_file_text(target, content, overwrite=False)
            snap_id = f"snap_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            self._snapshots.append(Snapshot(id=snap_id, action="create", file_path=rel))
            if self._should_record_audit():
                try:
                    from core.repository.audit import get_audit_logger
                    from core.repository.ide import get_preferred_ide
                    get_audit_logger().record_change(
                        file_path=rel,
                        action="create",
                        voice_summary=f"Created new file `{rel}` ({len(content.splitlines())} lines).",
                        additions=len(content.splitlines()),
                        ide=get_preferred_ide() or "",
                    )
                except Exception:
                    pass
                self._save_repo_sync_backup(
                    action="create",
                    rel_path=rel,
                    snap_id=snap_id,
                    diff_summary=f"+{len(content.splitlines())} lines",
                )
            return {
                "status": "created",
                "file": rel,
                "size": meta["size"],
                "snapshot_id": snap_id,
            }

        if config_manager.get_engineering_confirm_patches() and not auto_approve:
            req_msg = confirm_gate.request(
                key=f"create_{target.name}",
                title=f"Create file {rel}",
                detail=f"Create new file ({len(content)} characters)",
                run=_do_create,
            )
            return {"status": "approval_required", "file": rel, "message": req_msg}

        return _do_create()

    def rename_file(self, old_path: str, new_path: str, auto_approve: bool = False) -> dict:
        """Rename/move a file within the workspace with approval gating."""
        src = self.workspace.resolve_safe_path(old_path, must_exist=True, require_file=True)
        dst = self.workspace.resolve_safe_path(new_path, must_exist=False)

        if dst.exists():
            return {"status": "error", "error": f"Destination path '{new_path}' already exists."}

        rel_src = src.relative_to(self.workspace.root).as_posix()
        rel_dst = dst.relative_to(self.workspace.root).as_posix()

        def _do_rename():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            snap_id = f"snap_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            self._snapshots.append(Snapshot(id=snap_id, action="rename", file_path=rel_dst, old_path=rel_src))
            if self._should_record_audit():
                try:
                    from core.repository.audit import get_audit_logger
                    from core.repository.ide import get_preferred_ide
                    get_audit_logger().record_change(
                        file_path=rel_dst,
                        action="rename",
                        voice_summary=f"Renamed `{rel_src}` to `{rel_dst}`.",
                        ide=get_preferred_ide() or "",
                    )
                except Exception:
                    pass
                self._save_repo_sync_backup(
                    action="rename",
                    rel_path=rel_dst,
                    snap_id=snap_id,
                    diff_summary=f"From {rel_src}",
                )
            return {"status": "renamed", "from": rel_src, "to": rel_dst, "snapshot_id": snap_id}

        if config_manager.get_engineering_confirm_patches() and not auto_approve:
            req_msg = confirm_gate.request(
                key=f"rename_{src.name}",
                title=f"Rename {rel_src} -> {rel_dst}",
                detail=f"Move file within repository",
                run=_do_rename,
            )
            return {"status": "approval_required", "from": rel_src, "to": rel_dst, "message": req_msg}

        return _do_rename()

    def delete_file(self, path: str, auto_approve: bool = False) -> dict:
        """Delete a file within the workspace with approval gating."""
        target = self.workspace.resolve_safe_path(path, must_exist=True, require_file=True)
        rel = target.relative_to(self.workspace.root).as_posix()
        prev_bytes = target.read_bytes()

        def _do_delete():
            target.unlink()
            snap_id = f"snap_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            self._snapshots.append(Snapshot(
                id=snap_id,
                action="delete",
                file_path=rel,
                previous_bytes=prev_bytes,
            ))
            if self._should_record_audit():
                try:
                    from core.repository.audit import get_audit_logger
                    from core.repository.ide import get_preferred_ide
                    get_audit_logger().record_change(
                        file_path=rel,
                        action="delete",
                        voice_summary=f"Deleted file `{rel}`.",
                        ide=get_preferred_ide() or "",
                    )
                except Exception:
                    pass
                self._save_repo_sync_backup(
                    action="delete",
                    rel_path=rel,
                    snap_id=snap_id,
                    previous_bytes=prev_bytes,
                    diff_summary="Deleted file",
                )
            return {"status": "deleted", "file": rel, "snapshot_id": snap_id}

        if config_manager.get_engineering_confirm_patches() and not auto_approve:
            req_msg = confirm_gate.request(
                key=f"delete_{target.name}",
                title=f"Delete file {rel}",
                detail=f"Remove file from repository ({len(prev_bytes):,} bytes)",
                run=_do_delete,
            )
            return {"status": "approval_required", "file": rel, "message": req_msg}

        return _do_delete()


# Global singleton
_patch_manager: Optional[PatchManager] = None


def get_patch_manager() -> PatchManager:
    global _patch_manager
    if _patch_manager is None:
        _patch_manager = PatchManager()
    return _patch_manager


def preview_patch(diff_text: str) -> dict:
    """Preview a unified patch without modifying files."""
    return get_patch_manager().preview_patch(diff_text)


def apply_patch(diff_text: str, auto_approve: bool = False) -> dict:
    """Apply a unified diff patch atomically."""
    return get_patch_manager().apply_patch(diff_text, auto_approve=auto_approve)


def rollback_patch(snapshot_id: Optional[str] = None) -> dict:
    """Roll back previous patch or file changes."""
    return get_patch_manager().rollback_patch(snapshot_id=snapshot_id)


def create_file(path: str, content: str, auto_approve: bool = False) -> dict:
    """Create a new file in the workspace."""
    return get_patch_manager().create_file(path, content, auto_approve=auto_approve)


def rename_file(old_path: str, new_path: str, auto_approve: bool = False) -> dict:
    """Rename a file in the workspace."""
    return get_patch_manager().rename_file(old_path, new_path, auto_approve=auto_approve)


def delete_file(path: str, auto_approve: bool = False) -> dict:
    """Delete a file in the workspace with rollback tracking."""
    return get_patch_manager().delete_file(path, auto_approve=auto_approve)
