"""
Action discovery, validation, and dispatch — the built-in twin of plugin_loader.

Every actions/*.py that exposes a module-level ``TOOL`` dict is auto-discovered
here, exactly like a drop-in plugin, so main.py never has to hardcode a tool
declaration or a dispatch branch for it. Adding a new bundled action is then the
same one-file operation as writing a plugin: define ``TOOL`` and a handler.

``TOOL`` shape (see actions/open_app.py for a live example):

    TOOL = {
        "name":        "open_app",              # unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
        "description":  "...",                   # what Gemini reads to route the call
        "parameters":  {"type": "OBJECT", ...}, # Gemini function-declaration schema
        "handler":      open_app,                # the callable to run
    }

The handler is invoked through signature introspection: it receives ``parameters``
plus whichever of ``player`` / ``speak`` / ``response`` / ``session_memory`` it
actually declares — so existing action signatures work unchanged.

Discovery runs once at startup; import errors, validation errors, and name
collisions are logged and the offending file is skipped — they NEVER raise out
of discover_actions() and never abort the scan of the remaining files.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}
_CTX_KEYS = ("player", "speak", "response", "session_memory")


# A tool may declare that the model should NOT be held up waiting for it.
# `behavior` goes to the API with the declaration; `scheduling` decides when the
# eventual result is allowed back into the conversation:
#   WHEN_IDLE  — wait for a gap in the speech (the sane default)
#   SILENT     — record it, do not prompt a reply (the tool already announced)
#   INTERRUPT  — cut in immediately (only when the answer cannot wait)
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")


# Keys this project puts on a declaration for its own bookkeeping. They live
# here because this file documents what a TOOL dict may contain, and both
# loaders produce them.
#
# THEY MUST NEVER REACH A VENDOR. Gemini validates function declarations
# against a fixed schema and an unknown field is an error, not something it
# ignores — so main.py calls strip_meta() before building the Live config.
#
# The definition is here rather than in providers/schema.py, which is the only
# other place that cares, because main.py must be able to strip them WITHOUT
# importing providers: a default install never loads that package, and the
# measured cost of deciding not to is the release's headline number.
META_KEYS = ("tier", "brief")


def strip_meta(decls):
    """Declarations without META_KEYS. Non-dict entries pass through untouched;
    a declaration carrying none is returned as the same object, so what an API
    receives is byte-for-byte what it received before these keys existed."""
    out = []
    for decl in decls:
        if isinstance(decl, dict) and any(k in decl for k in META_KEYS):
            out.append({k: v for k, v in decl.items() if k not in META_KEYS})
        else:
            out.append(decl)
    return out


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None

def _opt_tier(value) -> Optional[str]:
    """"core" or "extra" from a declaration, or None when it did not say.

    Anything else is treated as "did not say" rather than as an error: a typo
    should fall back to the default ranking, not stop a working tool loading.
    """
    text = str(value or "").strip().lower()
    return text if text in ("core", "extra") else None


def _opt_text(value) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


PROFILE_DEFAULT = "default"
PROFILE_ENGINEERING = "engineering"

# Explicit set of allowed actions in software engineering mode
ENGINEERING_ALLOWED_ACTIONS: set[str] = {
    # Core repository operations
    "list_files",
    "read_file",
    "read_range",
    "search_text",
    "find_symbol",
    "find_references",
    "inspect_dependencies",
    "get_repository_summary",
    # Patch and file editing operations
    "apply_patch",
    "preview_patch",
    "rollback_patch",
    "create_file",
    "rename_file",
    "delete_file",
    # Validation and diagnostics
    "run_tests",
    "run_linter",
    "run_formatter",
    "run_type_checker",
    "run_build",
    "collect_diagnostics",
    # Version control operations
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_commit",
    "git_push",
    "execute_command",
    "write_ide_terminal",
    "execute_ide_terminal",
    # Real-time IDE and Workspace operations
    "add_workspace_folder",
    "choose_ide",
    "list_available_ides",
    "get_code_audit",
    # Preserved engineering capabilities
    "dev_agent",
    "code_helper",
    "file_processor",
    "web_search",
    # Safe inline session tools
    "system_status",
    "undo",
    "screen_process",
    "close_camera",
}

ENGINEERING_DISABLED_ACTIONS: set[str] = {
    "weather_report",
    "send_message",
    "video_player",
    "youtube_video",
    "game_updater",
    "flight_finder",
    "desktop",
    "computer_control",
    "computer_settings",
    "routines",
    "proactive",
    "background_monitor",
    "background_tasks",
    "open_app",
    "reminder",
    "save_memory",
}


@dataclass
class ActionRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    handler: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    behavior: Optional[str] = None     # None = the API's default (blocking)
    scheduling: Optional[str] = None   # None = the API's default (WHEN_IDLE)
    # Read by providers/schema.py when a small model gets a tool budget; never
    # sent to any API — main.py strips both before the Gemini config is built.
    #   tier   "core" | "extra" — this file's own opinion of whether it should
    #          survive a tight budget, instead of that being decided by the
    #          order files happen to be discovered in.
    #   brief  a short description used in place of the long one when a budget
    #          is in force. Nothing is deleted: Gemini still gets the full text.
    tier: Optional[str] = None
    brief: Optional[str] = None


class ActionRegistry:
    def __init__(self, actions: dict[str, ActionRecord], logger: Callable[[str], None]):
        self._actions = actions          # name -> ActionRecord, VALID entries only
        self._all_records: list[ActionRecord] = []
        self._logger = logger
        self._active_profile: Optional[str] = None

    def set_profile(self, profile: Optional[str]) -> None:
        """Set the active tool profile ('default' or 'engineering' or None to use config)."""
        self._active_profile = profile

    def get_profile(self) -> str:
        """Return the active profile name."""
        if self._active_profile is not None:
            return self._active_profile
        try:
            from memory import config_manager
            if config_manager.get_engineering_mode():
                return PROFILE_ENGINEERING
        except Exception:
            pass
        return PROFILE_DEFAULT

    def register_action(self, rec: ActionRecord) -> None:
        """Register or update an ActionRecord dynamically."""
        if rec.valid:
            self._actions[rec.name] = rec
        self._all_records.append(rec)

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self, profile: Optional[str] = None) -> list[dict]:
        prof = profile or self.get_profile()
        is_engineering = (prof == PROFILE_ENGINEERING)
        out = []
        for rec in self._actions.values():
            if is_engineering and rec.name not in ENGINEERING_ALLOWED_ACTIONS:
                continue
            decl = {"name": rec.name, "description": rec.description,
                    "parameters": rec.parameters}
            if rec.behavior:
                decl["behavior"] = rec.behavior
            # Budget metadata. NOT part of any API's schema — main.py calls
            # providers.schema.strip_meta() before the Gemini config is built,
            # and budget() consumes them on the local-engine path.
            if rec.tier:
                decl["tier"] = rec.tier
            if rec.brief:
                decl["brief"] = rec.brief
            out.append(decl)
        return out

    def has(self, name: str, profile: Optional[str] = None) -> bool:
        prof = profile or self.get_profile()
        if prof == PROFILE_ENGINEERING:
            return name in self._actions and name in ENGINEERING_ALLOWED_ACTIONS
        return name in self._actions

    def scheduling(self, name: str) -> Optional[str]:
        """How this action's result should re-enter the conversation, if it said."""
        rec = self._actions.get(name)
        return rec.scheduling if rec else None

    def names(self, profile: Optional[str] = None) -> set[str]:
        prof = profile or self.get_profile()
        if prof == PROFILE_ENGINEERING:
            return set(self._actions.keys()) & ENGINEERING_ALLOWED_ACTIONS
        return set(self._actions.keys())

    # -- called by main.py from _execute_tool --
    def run(self, name: str, parameters: dict, ctx: dict | None = None) -> str:
        prof = self.get_profile()
        if prof == PROFILE_ENGINEERING and name not in ENGINEERING_ALLOWED_ACTIONS:
            return f"Action '{name}' is disabled in engineering mode."
        rec = self._actions.get(name)
        if rec is None or not rec.valid:
            return f"Action '{name}' is not available."
        try:
            return _call_handler(rec.handler, parameters, ctx or {}) or "Done."
        except Exception as e:
            self._logger(f"Action '{name}' crashed during run(): {e}")
            traceback.print_exc()
            return f"Tool '{name}' failed: {e}"


def _call_handler(fn: Callable, parameters: dict, ctx: dict) -> str:
    """Invoke the handler passing only the context kwargs it actually declares
    (or all of them if it has **kwargs), so each action's existing signature
    works unchanged."""
    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for key in _CTX_KEYS:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)
    return fn(parameters=parameters, **kwargs)


def _validate_tool(tool: dict, filename: str) -> ActionRecord:
    """Returns an ActionRecord for a tool dict; .valid=False + .error set on any problem."""
    if not isinstance(tool, dict):
        return ActionRecord(name=Path(filename).stem, file=filename,
                            error="Invalid tool declaration (not a dict).")

    name = tool.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return ActionRecord(name=str(name or Path(filename).stem), file=filename,
                            error="TOOL['name'] missing or not a valid identifier.")

    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        return ActionRecord(name=name, file=filename,
                            error="TOOL['description'] missing or empty.")

    parameters = tool.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return ActionRecord(name=name, file=filename,
                            error="TOOL['parameters'] must be a dict with \"type\": \"OBJECT\".")

    handler = tool.get("handler")
    if not callable(handler):
        return ActionRecord(name=name, file=filename,
                            error="TOOL['handler'] missing or not callable.")

    return ActionRecord(name=name, description=description.strip(), parameters=parameters,
                        handler=handler, file=filename, valid=True, error="",
                        behavior=_opt_upper(tool.get("behavior"), _BEHAVIORS),
                        scheduling=_opt_upper(tool.get("scheduling"), _SCHEDULING),
                        tier=_opt_tier(tool.get("tier")),
                        brief=_opt_text(tool.get("brief")))


def _validate(module, filename: str) -> ActionRecord:
    """Backwards-compatible module-level validator for TOOL dict."""
    tool = getattr(module, "TOOL", None)
    if not isinstance(tool, dict):
        return ActionRecord(name=Path(filename).stem, file=filename,
                            error="No module-level TOOL dict (not a discoverable action).")
    return _validate_tool(tool, filename)


def discover_actions(actions_dir: Path, reserved_names: set[str] | None = None,
                     logger: Callable[[str], None] = print) -> ActionRegistry:
    """
    Scans actions_dir for *.py files (skips files starting with '_'). A file is
    only treated as an action if it exposes a module-level TOOL dict or TOOLS list;
    files without either are silently ignored.
    Import/validation errors and name collisions are logged and the file is
    skipped — they NEVER raise out of this function.
    """
    reserved = reserved_names or set()
    actions_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, ActionRecord] = {}
    all_records: list[ActionRecord] = []

    files = sorted(actions_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"actions.{path.stem}"
            # Reuse the already-imported module when present so handlers are the
            # same objects the rest of the app holds.
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not build import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

            tools_list = getattr(module, "TOOLS", None)
            if tools_list is None:
                single_tool = getattr(module, "TOOL", None)
                tools_list = [single_tool] if single_tool is not None else []

            if not tools_list:
                continue   # not an action file — a helper/capture-only module

            for tool_item in tools_list:
                rec = _validate_tool(tool_item, path.name)

                if rec.valid and rec.name in reserved:
                    rec = ActionRecord(name=rec.name, file=path.name,
                                       error=f"Name '{rec.name}' collides with a reserved core tool — rejected.")
                elif rec.valid and rec.name in valid:
                    other = valid[rec.name].file
                    rec = ActionRecord(name=rec.name, file=path.name,
                                       error=f"Name '{rec.name}' already used by action '{other}' — rejected.")

                all_records.append(rec)
                if rec.valid:
                    valid[rec.name] = rec
                    logger(f"Action loaded: {rec.name} ({path.name})")
                else:
                    logger(f"Action rejected: {path.name} ({rec.name}) — {rec.error}")

        except Exception as e:
            rec = ActionRecord(name=path.stem, file=path.name,
                               error=f"Failed to load: {e}")
            traceback.print_exc()
            all_records.append(rec)
            logger(f"Action rejected: {path.name} — {rec.error}")

    registry = ActionRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Action discovery complete: {len(valid)} active.")
    return registry
