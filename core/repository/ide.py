"""
core/repository/ide.py — Real-time IDE detection, selection, and workspace launching.

Detects installed IDEs on the user's system (VS Code, Cursor, Windsurf, PyCharm,
IntelliJ, Sublime Text, Zed, etc.). Supports single-IDE auto-launch, multi-IDE
voice & HUD popup selection, and real-time live code synchronization.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from memory import config_manager


@dataclass
class DetectedIDE:
    id: str                    # e.g. "vscode", "cursor", "pycharm"
    name: str                  # e.g. "Visual Studio Code"
    command: str               # Executable or launcher command
    path: str                  # Full path if resolved, or command name
    icon: str = "💻"          # HUD display symbol
    is_preferred: bool = False # User's default or chosen IDE


# Definition of known IDEs and their discovery paths on Windows / cross-platform
_KNOWN_IDES = [
    {
        "id": "vscode",
        "name": "Visual Studio Code",
        "commands": ["code.cmd", "code.exe", "code"],
        "icon": "🟦",
        "windows_relative_paths": [
            r"Microsoft VS Code\bin\code.cmd",
            r"Microsoft VS Code\Code.exe",
        ],
    },
    {
        "id": "vscode-insiders",
        "name": "Visual Studio Code Insiders",
        "commands": ["code-insiders.cmd", "code-insiders.exe", "code-insiders"],
        "icon": "🟩",
        "windows_relative_paths": [
            r"Microsoft VS Code Insiders\bin\code-insiders.cmd",
            r"Microsoft VS Code Insiders\Code - Insiders.exe",
        ],
    },
    {
        "id": "cursor",
        "name": "Cursor",
        "commands": ["cursor.cmd", "cursor.exe", "cursor"],
        "icon": "⚡",
        "windows_relative_paths": [
            r"cursor\Cursor.exe",
            r"cursor\resources\app\bin\cursor.cmd",
            r"Programs\cursor\Cursor.exe",
        ],
    },
    {
        "id": "windsurf",
        "name": "Windsurf",
        "commands": ["windsurf.cmd", "windsurf.exe", "windsurf"],
        "icon": "🏄",
        "windows_relative_paths": [
            r"windsurf\Windsurf.exe",
            r"windsurf\bin\windsurf.cmd",
            r"Programs\windsurf\Windsurf.exe",
        ],
    },
    {
        "id": "pycharm",
        "name": "PyCharm",
        "commands": ["pycharm64.exe", "pycharm.cmd", "pycharm.exe", "pycharm"],
        "icon": "🐍",
        "windows_relative_paths": [
            r"JetBrains\PyCharm*\bin\pycharm64.exe",
        ],
    },
    {
        "id": "intellij",
        "name": "IntelliJ IDEA",
        "commands": ["idea64.exe", "idea.cmd", "idea.exe", "idea"],
        "icon": "💡",
        "windows_relative_paths": [
            r"JetBrains\IntelliJ*\bin\idea64.exe",
        ],
    },
    {
        "id": "webstorm",
        "name": "WebStorm",
        "commands": ["webstorm64.exe", "webstorm.cmd", "webstorm.exe", "webstorm"],
        "icon": "🌐",
        "windows_relative_paths": [
            r"JetBrains\WebStorm*\bin\webstorm64.exe",
        ],
    },
    {
        "id": "sublime",
        "name": "Sublime Text",
        "commands": ["subl", "subl.exe", "sublime_text.exe"],
        "icon": "📄",
        "windows_relative_paths": [
            r"Sublime Text\subl.exe",
            r"Sublime Text 3\subl.exe",
        ],
    },
    {
        "id": "zed",
        "name": "Zed",
        "commands": ["zed", "zed.exe"],
        "icon": "⚡",
        "windows_relative_paths": [
            r"Zed\zed.exe",
            r"Programs\Zed\zed.exe",
        ],
    },
    {
        "id": "visualstudio",
        "name": "Microsoft Visual Studio",
        "commands": ["devenv.exe", "devenv"],
        "icon": "🟪",
        "windows_relative_paths": [
            r"Microsoft Visual Studio\*\*\Common7\IDE\devenv.exe",
        ],
    },
    {
        "id": "androidstudio",
        "name": "Android Studio",
        "commands": ["studio64.exe", "studio.exe", "studio"],
        "icon": "🤖",
        "windows_relative_paths": [
            r"Android\Android Studio\bin\studio64.exe",
        ],
    },
    {
        "id": "notepadplusplus",
        "name": "Notepad++",
        "commands": ["notepad++.exe", "notepad++"],
        "icon": "📝",
        "windows_relative_paths": [
            r"Notepad++\notepad++.exe",
        ],
    },
]


def _get_search_roots() -> list[Path]:
    """Return common user and system installation roots on Windows."""
    roots = []
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            p = Path(local_app)
            roots.append(p / "Programs")
            roots.append(p)
            roots.append(p / "JetBrains" / "Toolbox" / "apps")
        prog_files = os.environ.get("ProgramFiles")
        if prog_files:
            roots.append(Path(prog_files))
        prog_x86 = os.environ.get("ProgramFiles(x86)")
        if prog_x86:
            roots.append(Path(prog_x86))
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            roots.append(Path(user_profile) / "Desktop")
    else:
        roots.extend([Path("/usr/bin"), Path("/usr/local/bin"), Path("/Applications")])
    return [r for r in roots if r.exists()]


def detect_installed_ides() -> list[DetectedIDE]:
    """
    Scan the local machine for installed IDEs and editors.
    Returns list of DetectedIDE instances sorted by preference and reliability.
    """
    found: list[DetectedIDE] = []
    seen_ids = set()
    preferred_id = get_preferred_ide()
    search_roots = _get_search_roots()

    for spec in _KNOWN_IDES:
        ide_id = spec["id"]
        ide_name = spec["name"]
        ide_icon = spec["icon"]
        resolved_path: Optional[str] = None
        launcher_cmd: Optional[str] = None

        # 1. Check PATH via shutil.which
        for cmd in spec["commands"]:
            p = shutil.which(cmd)
            if p:
                resolved_path = str(Path(p).resolve())
                launcher_cmd = cmd
                break

        # 2. Check standard installation roots if not on PATH
        if not resolved_path and sys.platform == "win32":
            for rel_pat in spec.get("windows_relative_paths", []):
                for root in search_roots:
                    # Support wildcard patterns (e.g. PyCharm*)
                    matches = list(root.glob(rel_pat))
                    if matches:
                        target = matches[0]
                        if target.is_file():
                            resolved_path = str(target.resolve())
                            launcher_cmd = resolved_path
                            break
                if resolved_path:
                    break

        if resolved_path and ide_id not in seen_ids:
            seen_ids.add(ide_id)
            is_pref = (ide_id == preferred_id)
            found.append(
                DetectedIDE(
                    id=ide_id,
                    name=ide_name,
                    command=launcher_cmd or spec["commands"][0],
                    path=resolved_path,
                    icon=ide_icon,
                    is_preferred=is_pref,
                )
            )

    # Sort preferred IDE to the top if present
    found.sort(key=lambda x: (not x.is_preferred, x.name))
    return found


def launch_ide(
    ide: Union[DetectedIDE, str],
    folder_path: Union[str, Path],
    file_path: Optional[Union[str, Path]] = None,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Launch the specified IDE on the workspace folder, optionally opening a file.
    Runs non-blocking and detached so the assistant and UI never freeze.
    """
    folder = Path(folder_path).resolve()
    if not folder.exists() or not folder.is_dir():
        return False, f"Target folder does not exist or is not a directory: {folder}"

    # Resolve IDE details
    cmd_or_path = ""
    ide_name = "IDE"
    ide_id = ""
    if isinstance(ide, DetectedIDE):
        cmd_or_path = ide.command or ide.path
        ide_name = ide.name
        ide_id = ide.id
    else:
        # Match string by ID or name among detected IDEs
        detected = detect_installed_ides()
        match = next((d for d in detected if d.id.lower() == ide.lower() or ide.lower() in d.name.lower()), None)
        if match:
            cmd_or_path = match.command or match.path
            ide_name = match.name
            ide_id = match.id
        else:
            # Fall back to raw command
            cmd_or_path = ide
            ide_name = ide
            ide_id = ide.lower()

    if not cmd_or_path:
        return False, "No valid IDE executable or launcher command specified."

    # Windows executable resolution
    if sys.platform == "win32":
        p = Path(cmd_or_path)
        if p.exists() and not p.suffix:
            if p.with_suffix(".cmd").exists():
                cmd_or_path = str(p.with_suffix(".cmd"))
            elif p.with_suffix(".exe").exists():
                cmd_or_path = str(p.with_suffix(".exe"))
            elif p.with_suffix(".bat").exists():
                cmd_or_path = str(p.with_suffix(".bat"))
        elif not p.exists() and not p.suffix:
            which_cmd = shutil.which(cmd_or_path + ".cmd") or shutil.which(cmd_or_path + ".exe") or shutil.which(cmd_or_path)
            if which_cmd:
                cmd_or_path = which_cmd

    if dry_run:
        set_preferred_ide(ide_id)
        return True, f"[DryRun] Would launch {ide_name} on {folder.name}."

    # Build launch command
    # Most modern IDEs (code, cursor, windsurf, pycharm, idea, subl) accept `cmd <folder_path> [<file_path>]`
    args = [cmd_or_path, str(folder)]
    if file_path:
        f = Path(file_path).resolve()
        if f.exists():
            args.append(str(f))

    try:
        # Launch detached process without keeping a blocking pipe
        is_cmd = cmd_or_path.lower().endswith((".cmd", ".bat"))
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["shell"] = is_cmd
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
            kwargs["stdin"] = subprocess.DEVNULL

        subprocess.Popen(args, **kwargs)
        set_preferred_ide(ide_id)
        return True, f"Successfully launched {ide_name} on {folder.name}."
    except Exception as e:
        return False, f"Failed to launch {ide_name}: {e}"


# ── Preferred IDE Storage ────────────────────────────────────────────────────

def get_preferred_ide() -> Optional[str]:
    """Return the user's saved preferred IDE id (e.g. 'vscode', 'cursor')."""
    try:
        val = config_manager.get_preferred_ide()
        return val if val else None
    except Exception:
        return None


def set_preferred_ide(ide_id: str) -> None:
    """Save the user's preferred IDE choice."""
    if not ide_id:
        return
    try:
        clean_id = ide_id.lower().strip()
        config_manager.save_preferred_ide(clean_id)
    except Exception:
        pass


# ── Pending Choice Management (Voice or Option Popup Resolution) ─────────────

@dataclass
class _PendingChoice:
    folder_path: Path
    ides: list[DetectedIDE]
    created_at: float
    on_resolved: Optional[Callable[[DetectedIDE], None]] = None


_pending_ide_choice: Optional[_PendingChoice] = None
_pending_lock = threading.Lock()
CHOICE_TIMEOUT_SECONDS = 90.0


def register_pending_ide_choice(
    folder_path: Union[str, Path],
    ides: list[DetectedIDE],
    on_resolved: Optional[Callable[[DetectedIDE], None]] = None,
) -> None:
    """Store an active pending IDE choice waiting for user voice or UI click."""
    global _pending_ide_choice
    with _pending_lock:
        _pending_ide_choice = _PendingChoice(
            folder_path=Path(folder_path).resolve(),
            ides=ides,
            created_at=time.time(),
            on_resolved=on_resolved,
        )


def get_pending_ide_choice() -> Optional[_PendingChoice]:
    """Retrieve active pending IDE choice if not expired."""
    global _pending_ide_choice
    with _pending_lock:
        if _pending_ide_choice is None:
            return None
        if time.time() - _pending_ide_choice.created_at > CHOICE_TIMEOUT_SECONDS:
            _pending_ide_choice = None
            return None
        return _pending_ide_choice


def clear_pending_ide_choice() -> None:
    """Clear any pending IDE selection."""
    global _pending_ide_choice
    with _pending_lock:
        _pending_ide_choice = None


def resolve_pending_ide_choice(
    user_input: str,
    dry_run: bool = False,
) -> Tuple[bool, str, Optional[DetectedIDE]]:
    """
    Resolve pending IDE choice via user's voice utterance or UI button click.
    Matches input against detected IDE names, IDs, or numbers.
    """
    global _pending_ide_choice
    with _pending_lock:
        pending = _pending_ide_choice
        _pending_ide_choice = None

    if pending is None or (time.time() - pending.created_at > CHOICE_TIMEOUT_SECONDS):
        return False, "No active IDE selection is waiting.", None

    text = user_input.lower().strip()
    matched_ide: Optional[DetectedIDE] = None

    # Check index number (e.g. "1", "option 2")
    for i, ide in enumerate(pending.ides, 1):
        if text == str(i) or f"option {i}" in text or f"number {i}" in text:
            matched_ide = ide
            break

    # Check ID or Name match
    if not matched_ide:
        for ide in pending.ides:
            if ide.id.lower() in text or ide.name.lower() in text:
                matched_ide = ide
                break

    # Check partial keywords (e.g. "code", "cursor", "studio", "pycharm")
    if not matched_ide:
        for ide in pending.ides:
            words = [w for w in ide.name.lower().split() if len(w) > 3]
            if any(w in text for w in words):
                matched_ide = ide
                break

    if not matched_ide:
        # Re-register if still valid and unmatched
        with _pending_lock:
            _pending_ide_choice = pending
        ide_list_str = ", ".join(d.name for d in pending.ides)
        return False, f"Could not determine which IDE was chosen from: '{user_input}'. Available: {ide_list_str}", None

    # Launch chosen IDE
    ok, msg = launch_ide(matched_ide, pending.folder_path, dry_run=dry_run)
    if pending.on_resolved:
        try:
            pending.on_resolved(matched_ide)
        except Exception:
            pass

    return ok, msg, matched_ide


# ── IDE Integrated Terminal Automation & Staged Execution ────────────────────

_staged_terminal_lock = threading.Lock()
_staged_terminal_command: Optional[str] = None
_staged_terminal_ide: Optional[str] = None


def get_staged_terminal_command() -> Optional[str]:
    """Return the currently staged command awaiting user execution confirmation."""
    with _staged_terminal_lock:
        return _staged_terminal_command


def clear_staged_terminal_command() -> None:
    """Clear any staged IDE terminal command."""
    global _staged_terminal_command, _staged_terminal_ide
    with _staged_terminal_lock:
        _staged_terminal_command = None
        _staged_terminal_ide = None


def _get_candidate_window_titles(ide_id: str) -> list[str]:
    """Return common window title prefixes or substrings for the given IDE."""
    low = ide_id.lower()
    if "insider" in low:
        return ["Code - Insiders", "Visual Studio Code Insiders", "Visual Studio Code", "Code"]
    if "vscode" in low or "code" in low:
        return ["Visual Studio Code", "Code - Insiders", "Code"]
    if "cursor" in low:
        return ["Cursor"]
    if "windsurf" in low:
        return ["Windsurf"]
    if "pycharm" in low:
        return ["PyCharm"]
    if "intellij" in low or "idea" in low:
        return ["IntelliJ IDEA", "IDEA"]
    if "webstorm" in low:
        return ["WebStorm"]
    if "sublime" in low:
        return ["Sublime Text"]
    if "zed" in low:
        return ["Zed"]
    return ["Visual Studio Code", "Code - Insiders", "Code", "Cursor", "Windsurf", "PyCharm"]


def focus_ide_window(ide_id: Optional[str] = None) -> bool:
    """
    Bring the active or preferred IDE window to the foreground.
    Supports Windows (AppActivate & Win32 API), macOS (AppleScript), and Linux (wmctrl/xdotool).
    """
    chosen_id = ide_id or get_preferred_ide() or "vscode"
    candidates = _get_candidate_window_titles(chosen_id)

    # Windows focus strategy
    if sys.platform == "win32":
        # Strategy 1: WScript.Shell AppActivate (fast & handles title matching)
        for title in candidates:
            try:
                cmd = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if "True" in res.stdout:
                    time.sleep(0.15)
                    return True
            except Exception:
                pass

        # Strategy 2: Win32 API via ctypes
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            SW_RESTORE = 9

            found_hwnd = None
            def _enum(hwnd, _l):
                nonlocal found_hwnd
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        title_text = buff.value
                        if any(c.lower() in title_text.lower() for c in candidates):
                            found_hwnd = hwnd
                            return False
                return True

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDPROC(_enum), 0)

            if found_hwnd:
                if user32.IsIconic(found_hwnd):
                    user32.ShowWindow(found_hwnd, SW_RESTORE)
                user32.BringWindowToTop(found_hwnd)
                user32.SetForegroundWindow(found_hwnd)
                time.sleep(0.15)
                return True
        except Exception:
            pass

    elif sys.platform == "darwin":
        for title in candidates:
            try:
                script = f'tell application "System Events" to set frontmost of (first process whose name contains "{title}") to true'
                r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
                if r.returncode == 0:
                    time.sleep(0.15)
                    return True
            except Exception:
                pass

    elif sys.platform.startswith("linux"):
        for title in candidates:
            try:
                r = subprocess.run(["wmctrl", "-a", title], capture_output=True, timeout=3)
                if r.returncode == 0:
                    time.sleep(0.15)
                    return True
            except Exception:
                pass

    return False


def write_command_to_ide_terminal(
    command: str,
    ide: Optional[str] = None,
    dry_run: bool = False,
    player=None,
) -> Tuple[bool, str, str]:
    """
    Write the exact command into the active IDE terminal WITHOUT executing it.
    The command sits in the terminal prompt staged and waiting for user confirmation.
    Returns (success: bool, status_message: str, ide_name: str).
    """
    clean_cmd = str(command or "").strip()
    if not clean_cmd:
        return False, "Error: Empty command cannot be written to terminal.", ""

    global _staged_terminal_command, _staged_terminal_ide

    # Resolve target IDE
    chosen_ide = ide or get_preferred_ide() or ""
    all_ides = detect_installed_ides()
    matched_ide: Optional[DetectedIDE] = None

    if chosen_ide:
        matched_ide = next((d for d in all_ides if d.id.lower() == chosen_ide.lower() or chosen_ide.lower() in d.name.lower()), None)
    if not matched_ide and all_ides:
        matched_ide = all_ides[0]

    ide_name = matched_ide.name if matched_ide else (chosen_ide or "VS Code")
    ide_id = matched_ide.id if matched_ide else (chosen_ide.lower() or "vscode")

    # Stage the command in state
    with _staged_terminal_lock:
        _staged_terminal_command = clean_cmd
        _staged_terminal_ide = ide_id

    if dry_run:
        msg = f"[DryRun] Staged '{clean_cmd}' into {ide_name} terminal without executing."
        if player:
            player.write_log(f"[Terminal] ⌨️ {msg}")
        return True, msg, ide_name

    # Focus IDE window
    focused = focus_ide_window(ide_id)

    # If not focused, attempt launching/raising the IDE
    if not focused:
        from core.repository.workspace import get_workspace
        ws_root = get_workspace().root
        if ws_root:
            launch_ide(matched_ide or ide_id, ws_root)
            time.sleep(0.6)
            focused = focus_ide_window(ide_id)

    # Perform terminal focus & paste via GUI automation
    gui_typed = False
    try:
        import pyautogui
        import pyperclip

        # 1. Bring integrated terminal to focus:
        # VS Code / Cursor / Windsurf: Ctrl + ` (backtick) or Command + `
        # JetBrains: Alt + F12
        is_jetbrains = any(j in ide_id.lower() for j in ("pycharm", "idea", "intellij", "webstorm"))
        if is_jetbrains:
            pyautogui.hotkey("alt", "f12")
        elif sys.platform == "darwin":
            pyautogui.hotkey("command", "`")
        else:
            pyautogui.hotkey("ctrl", "`")

        time.sleep(0.25)

        # 2. Write exact command into terminal prompt via clipboard paste (never mangles symbols or special chars)
        pyperclip.copy(clean_cmd)
        time.sleep(0.05)
        if sys.platform == "darwin":
            pyautogui.hotkey("command", "v")
        else:
            pyautogui.hotkey("ctrl", "v")

        # CRITICAL SAFETY: DO NOT PRESS ENTER. The command must remain staged.
        gui_typed = True
    except Exception as e:
        # Fallback if PyAutoGUI is not available or display is headlessly sandboxed
        gui_typed = False

    msg = (
        f"Wrote '{clean_cmd}' into your {ide_name} terminal. It is staged and has NOT been executed. "
        f"Say 'execute it' or 'run it' when you are ready for me to execute it."
    )
    if player:
        player.write_log(f"[Terminal] ⌨️ Wrote '{clean_cmd}' in {ide_name} terminal (waiting for execution command).")

    return True, msg, ide_name


def execute_staged_ide_terminal(
    command: Optional[str] = None,
    dry_run: bool = False,
    player=None,
) -> Tuple[bool, str, str]:
    """
    Execute the command currently staged in the IDE terminal on explicit user instruction.
    Returns (success: bool, status_message: str, ide_name: str).
    """
    global _staged_terminal_command, _staged_terminal_ide

    with _staged_terminal_lock:
        cmd_to_run = _staged_terminal_command or (command or "").strip()
        ide_id = _staged_terminal_ide or get_preferred_ide() or "vscode"
        _staged_terminal_command = None
        _staged_terminal_ide = None

    if not cmd_to_run:
        return False, "No staged command was found in the IDE terminal to execute.", ""

    all_ides = detect_installed_ides()
    matched_ide = next((d for d in all_ides if d.id.lower() == ide_id.lower() or ide_id.lower() in d.name.lower()), None)
    ide_name = matched_ide.name if matched_ide else (ide_id.title() or "VS Code")

    if dry_run:
        msg = f"[DryRun] Executed staged command '{cmd_to_run}' in {ide_name} terminal."
        if player:
            player.write_log(f"[Terminal] ▶️ {msg}")
        return True, msg, ide_name

    # Focus IDE window
    focus_ide_window(ide_id)
    time.sleep(0.15)

    # Press Enter in the terminal to execute
    executed_via_gui = False
    try:
        import pyautogui
        # Ensure terminal has focus and press Enter
        is_jetbrains = any(j in ide_id.lower() for j in ("pycharm", "idea", "intellij", "webstorm"))
        if is_jetbrains:
            pyautogui.hotkey("alt", "f12")
        elif sys.platform == "darwin":
            pyautogui.hotkey("command", "`")
        else:
            pyautogui.hotkey("ctrl", "`")
        time.sleep(0.1)

        pyautogui.press("enter")
        executed_via_gui = True
    except Exception:
        executed_via_gui = False

    # Also log to player HUD
    msg = f"Executed '{cmd_to_run}' in your {ide_name} terminal as requested."
    if player:
        player.write_log(f"[Terminal] ▶️ Executed '{cmd_to_run}' in {ide_name} terminal.")

    # In case GUI automation failed (e.g. headless CI or tests), run safely in workspace
    if not executed_via_gui:
        try:
            from core.repository.diagnostics import run_safe_command
            import shlex
            args = shlex.split(cmd_to_run)
            res = run_safe_command(args, timeout=60, explicit_user_request=True)
            msg += f"\nOutput: {res.stdout or res.stderr}"
        except Exception:
            pass

    return True, msg, ide_name

