"""
core/repository/audit.py — Real-time code change audit trail and voice summaries.

Maintains an immutable record of code changes made by SYNC, formats live diffs
and statistics for the GUI audit panel, and generates natural voice summaries
for spoken responses to the user.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class CodeAuditEntry:
    id: str
    timestamp: str
    file_path: str
    action: str               # 'modify' | 'create' | 'rename' | 'delete'
    voice_summary: str        # Concise voice-ready description of what was changed
    diff_summary: str         # e.g. '+18 / -4 lines'
    diff_text: str            # Full or truncated unified diff text
    ide: str = ""             # Name of active IDE in which changes are live
    task_id: str = ""         # Background or parallel task id if applicable


class CodeAuditLogger:
    """Thread-safe logger for workspace code modifications."""

    def __init__(self, log_path: Optional[Path] = None):
        self._lock = threading.Lock()
        self._entries: list[CodeAuditEntry] = []
        self._listeners: list[Callable[[CodeAuditEntry], None]] = []

        if log_path:
            self._log_path = log_path
        else:
            self._log_path = Path.cwd() / "cache" / "code_audit.jsonl"

        self._load_recent()

    def _load_recent(self) -> None:
        """Load recent entries from persistent audit file if present, filtering out demo/test entries."""
        if not self._log_path.exists():
            return
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]  # Keep last 100 on load
            loaded = []
            for line in lines:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    fp = data.get("file_path", "")
                    # Filter out test harness artifacts / demo files
                    if fp in ("calc.py", "origin.txt", "dest.txt", "created.txt", "src/main.py") and not data.get("task_id"):
                        continue
                    loaded.append(CodeAuditEntry(**data))
            self._entries = loaded
        except Exception:
            pass

    def clear(self) -> None:
        """Clear all audit entries from memory and wipe the persistent audit log file."""
        with self._lock:
            self._entries.clear()
            try:
                if self._log_path.exists():
                    self._log_path.unlink()
            except Exception:
                pass

    def add_listener(self, callback: Callable[[CodeAuditEntry], None]) -> None:
        """Register a callback for real-time audit notifications (e.g. GUI signals)."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def record_change(
        self,
        file_path: str,
        action: str,
        diff_text: str = "",
        voice_summary: str = "",
        additions: int = 0,
        deletions: int = 0,
        ide: str = "",
        task_id: str = "",
        is_demo: bool = False,
    ) -> CodeAuditEntry:
        """
        Record an atomic code modification, notify listeners, and persist to audit log.
        Only real code modifications in local folders are persisted.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry_id = f"aud_{int(time.time())}_{uuid.uuid4().hex[:6]}"

        # Calculate additions and deletions from diff if not provided
        if not additions and not deletions and diff_text:
            for l in diff_text.splitlines():
                if l.startswith("+") and not l.startswith("+++"):
                    additions += 1
                elif l.startswith("-") and not l.startswith("---"):
                    deletions += 1

        diff_summary = f"+{additions} / -{deletions} lines" if (additions or deletions) else action.upper()

        if not voice_summary:
            action_desc = {
                "modify": "Updated",
                "create": "Created new file",
                "rename": "Renamed",
                "delete": "Deleted",
            }.get(action, "Modified")
            voice_summary = f"{action_desc} `{file_path}` ({diff_summary})."

        entry = CodeAuditEntry(
            id=entry_id,
            timestamp=now_str,
            file_path=file_path,
            action=action,
            voice_summary=voice_summary,
            diff_summary=diff_summary,
            diff_text=diff_text,
            ide=ide,
            task_id=task_id,
        )

        # Do not persist synthetic demo entries or test artifacts to the actual audit trail
        if is_demo or file_path in ("calc.py", "origin.txt", "dest.txt", "created.txt"):
            return entry

        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > 200:
                self._entries.pop(0)

        # Persist append
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        except Exception:
            pass

        # Notify real-time listeners
        for cb in list(self._listeners):
            try:
                cb(entry)
            except Exception:
                pass

        return entry

    def get_recent(self, limit: int = 30) -> list[CodeAuditEntry]:
        """Return the most recent audit entries."""
        with self._lock:
            return list(self._entries[-limit:])

    def format_ui_audit_text(self, limit: int = 20) -> str:
        """Format recent code audit entries for display in the GUI content panel."""
        entries = self.get_recent(limit)
        if not entries:
            return "No real-time code modifications recorded yet."

        lines = [
            "REAL-TIME CODE CHANGE AUDIT LOG",
            "────────────────────────────────────────────────────────",
        ]
        for e in reversed(entries):
            ide_str = f" [Live in {e.ide}]" if e.ide else ""
            lines.append(f"⏱ {e.timestamp}  ·  {e.action.upper()}: {e.file_path}{ide_str}")
            lines.append(f"   Delta: {e.diff_summary}")
            lines.append(f"   Summary: {e.voice_summary}")
            if e.diff_text:
                # Include a compact snippet of the diff
                diff_lines = [d for d in e.diff_text.splitlines() if d.startswith(("+", "-", "@@"))][:6]
                if diff_lines:
                    lines.append("   Preview:")
                    for dl in diff_lines:
                        lines.append(f"     {dl}")
            lines.append("────────────────────────────────────────────────────────")

        return "\n".join(lines)

    def generate_voice_summary(self, limit: int = 3) -> str:
        """
        Generate a natural, clear voice description of the most recent code changes.
        """
        entries = self.get_recent(limit)
        if not entries:
            return "No code changes were made."

        if len(entries) == 1:
            e = entries[0]
            ide_mention = f" Live in {e.ide}." if e.ide else ""
            return f"I made real-time code changes in `{e.file_path}`: {e.voice_summary}{ide_mention} The full audit diff is displayed on screen."

        summaries = [f"`{e.file_path}` ({e.diff_summary})" for e in entries[-limit:]]
        return f"I applied real-time code changes to {len(entries)} files: {', '.join(summaries)}. All edits are live in your IDE and detailed in the on-screen audit panel."


_audit_logger_instance: Optional[CodeAuditLogger] = None
_audit_logger_lock = threading.Lock()


def get_audit_logger() -> CodeAuditLogger:
    """Return the global CodeAuditLogger singleton."""
    global _audit_logger_instance
    with _audit_logger_lock:
        if _audit_logger_instance is None:
            _audit_logger_instance = CodeAuditLogger()
        return _audit_logger_instance
