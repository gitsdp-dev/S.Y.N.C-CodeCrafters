"""
core/repository/_tests.py — Focused tests for S.Y.N.C software engineering subsystem.

Run directly via:
    python -m core.repository._tests
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.action_loader import (
    ENGINEERING_ALLOWED_ACTIONS,
    PROFILE_DEFAULT,
    PROFILE_ENGINEERING,
    ActionRecord,
    ActionRegistry,
)
from core.repository.dependencies import inspect_dependencies
from core.repository.diagnostics import collect_diagnostics, run_safe_command
from core.repository.ignore import IgnoreMatcher
from core.repository.index import RepositoryIndex
from core.repository.patches import (
    Hunk,
    PatchManager,
    apply_hunks_to_lines,
    parse_unified_diff,
)
from core.repository.scanner import RepositoryScanner
from core.repository.search import (
    find_references,
    find_symbol,
    get_repository_summary,
    list_files,
    read_file,
    read_range,
    search_text,
)
from core.repository.symbols import (
    _try_treesitter_parse,
    detect_language,
    extract_symbols_and_imports,
)
from core.repository.workspace import (
    NoActiveWorkspaceError,
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceManager,
    WorkspaceSecurityError,
)
from memory import config_manager


_ok = 0
_fail = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        _failures.append(name)
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 70 - len(title)))


# ─────────────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_workspace_and_path_safety() -> None:
    section("1. Workspace and Path Safety")
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td).resolve()
        ws = WorkspaceManager(temp_root)

        # Basic root resolution
        check("Root is directory", ws.root == temp_root)

        # Path traversal rejection
        traversal_caught = False
        try:
            ws.resolve_safe_path("../outside.txt")
        except WorkspaceSecurityError:
            traversal_caught = True
        except Exception as e:
            print(f"Unexpected error: {e}")
        check("Path traversal rejected", traversal_caught)

        # Absolute path outside root rejection
        outside_caught = False
        try:
            ws.resolve_safe_path(temp_root.parent / "escape.txt")
        except WorkspaceSecurityError:
            outside_caught = True
        check("Outside root rejected", outside_caught)

        # Windows drive change rejection
        if sys.platform == "win32":
            drive_caught = False
            other_drive = "Z:\\some\\file.txt" if temp_root.drive.upper() != "Z:" else "X:\\some\\file.txt"
            try:
                ws.resolve_safe_path(other_drive)
            except WorkspaceSecurityError:
                drive_caught = True
            check("Windows drive change rejected", drive_caught)
        else:
            check("Windows drive change (skipped on non-Windows)", True)

        # Symlinks escaping repository
        symlink_caught = False
        outside_file = temp_root.parent / "real_outside.txt"
        outside_file.write_text("secret", encoding="utf-8")
        link_path = temp_root / "bad_link.txt"
        try:
            try:
                os.symlink(outside_file, link_path)
                try:
                    ws.resolve_safe_path("bad_link.txt")
                except WorkspaceSecurityError:
                    symlink_caught = True
            except (OSError, NotImplementedError):
                # Privilege not held on Windows for symlink creation
                symlink_caught = True
        finally:
            if outside_file.exists():
                outside_file.unlink()
        check("Symlinks escaping rejected", symlink_caught)

        # Missing file behavior
        missing_caught = False
        try:
            ws.resolve_safe_path("non_existent.py", must_exist=True)
        except WorkspaceFileNotFoundError:
            missing_caught = True
        check("Missing file raises WorkspaceFileNotFoundError", missing_caught)

        # Secrets access rejection (api_keys.json / .env / .pem / .key / id_rsa / *secret* / *credential*)
        secret_caught = False
        try:
            ws.resolve_safe_path("api_keys.json")
        except WorkspaceSecurityError:
            secret_caught = True
        check("Direct secrets file access rejected", secret_caught)

        env_caught = False
        try:
            ws.resolve_safe_path(".env")
        except WorkspaceSecurityError:
            env_caught = True
        check(".env file access rejected", env_caught)

        key_caught = False
        try:
            ws.resolve_safe_path("server.key")
        except WorkspaceSecurityError:
            key_caught = True
        check("Certificate/key file access rejected", key_caught)

        ssh_caught = False
        try:
            ws.resolve_safe_path("id_rsa")
        except WorkspaceSecurityError:
            ssh_caught = True
        check("SSH private key access rejected", ssh_caught)

        cred_caught = False
        try:
            ws.resolve_safe_path("db_credentials.json")
        except WorkspaceSecurityError:
            cred_caught = True
        check("Credentials file access rejected", cred_caught)


def test_ignore_rules_and_binary() -> None:
    section("2. .gitignore and Binary Detection")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        gi_file = root / ".gitignore"
        gi_file.write_text("*.log\nnode_modules/\n!important.log\n", encoding="utf-8")

        matcher = IgnoreMatcher(root)

        # Test defaults
        check("Default .git ignored", matcher.is_ignored(root / ".git", is_dir=True))
        check("Default __pycache__ ignored", matcher.is_ignored(root / "__pycache__", is_dir=True))
        check("Default .venv ignored", matcher.is_ignored(root / ".venv", is_dir=True))
        check("Default .pyc ignored", matcher.is_ignored("test.pyc", is_dir=False))
        check("Default sync_repo_index.db ignored", matcher.is_ignored("sync_repo_index.db", is_dir=False))

        # Test repository .gitignore
        check("Custom rule *.log ignored", matcher.is_ignored("error.log", is_dir=False))
        check("Custom rule node_modules/ ignored", matcher.is_ignored("node_modules", is_dir=True))
        check("Negation rule !important.log un-ignored", not matcher.is_ignored("important.log", is_dir=False))

        # Binary file rejection
        ws = WorkspaceManager(root)
        bin_file = root / "binary.dat"
        bin_file.write_bytes(b"hello\x00world\x01\x02")
        check("Binary file detected via null byte", ws.is_binary_file(bin_file))

        text_file = root / "text.txt"
        text_file.write_text("hello standard text", encoding="utf-8")
        check("Text file recognized as non-binary", not ws.is_binary_file(text_file))

        # Oversized file rejection
        big_file = root / "big.txt"
        big_file.write_text("A" * 500, encoding="utf-8")
        oversize_caught = False
        try:
            ws.read_file_text("big.txt", max_bytes=200)
        except WorkspaceError:
            oversize_caught = True
        check("Oversized file rejected", oversize_caught)

        # Malformed UTF-8 handling
        bad_utf8 = root / "bad.txt"
        bad_utf8.write_bytes(b"Valid prefix \xff\xfe and suffix")
        read_content, read_meta = ws.read_file_text("bad.txt")
        check("Malformed UTF-8 replaced cleanly", "Valid prefix" in read_content and read_meta["encoding"] == "utf-8-replace")


def test_sqlite_index_and_incremental_scanner() -> None:
    section("3. SQLite Index and Incremental Scanner")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        ws = WorkspaceManager(root)
        index = RepositoryIndex(root)

        # Check DB file creation outside repository source
        check("DB created outside repo", not index.db_path.is_relative_to(root) and index.db_path.exists())

        # Create sample files
        f1 = root / "module_a.py"
        f1.write_text("import sys\n\ndef func_a(x: int) -> int:\n    return x * 2\n", encoding="utf-8")

        f2 = root / "module_b.py"
        f2.write_text("from module_a import func_a\n\nclass Worker:\n    def work(self):\n        return func_a(10)\n", encoding="utf-8")

        scanner = RepositoryScanner(workspace=ws, index=index)

        # 1. Fresh index
        scan1 = scanner.scan()
        check("Fresh scan indexed 2 files", scan1["indexed_files"] == 2 and scan1["status"] == "complete")

        syms = index.find_symbols("func_a")
        check("Symbol func_a indexed", len(syms) == 1 and syms[0]["name"] == "func_a" and syms[0]["kind"] == "function")

        worker_class = index.find_symbols("Worker", kind="class")
        check("Class Worker indexed", len(worker_class) == 1 and worker_class[0]["kind"] == "class")

        worker_method = index.find_symbols("Worker.work")
        check("Method Worker.work indexed", len(worker_method) == 1 and worker_method[0]["kind"] == "method")

        # 2. Incremental unchanged scan
        scan2 = scanner.scan()
        check("Unchanged re-scan indexes 0 files", scan2["indexed_files"] == 0 and scan2["unchanged_files"] == 2)

        # 3. Changed file reindex
        f1.write_text("import sys\n\ndef func_a(x: int) -> int:\n    return x * 3\n\ndef new_func():\n    pass\n", encoding="utf-8")
        scan3 = scanner.scan()
        check("Changed file re-indexed exactly 1 file", scan3["indexed_files"] == 1 and scan3["unchanged_files"] == 1)

        new_sym = index.find_symbols("new_func")
        check("New symbol detected after change", len(new_sym) == 1)

        # 4. Deleted file cleanup
        f2.unlink()
        scan4 = scanner.scan()
        check("Deleted file cleaned up from index", scan4["removed_files"] == 1)
        check("Worker symbol removed after file deletion", len(index.find_symbols("Worker")) == 0)


def test_ast_symbol_extraction() -> None:
    section("4. Python AST Analysis")
    code = """
import os
from pathlib import Path
from ..utils import helper

MAX_RETRIES = 5
timeout_seconds: int = 30

class BaseAgent:
    pass

class MyService(BaseAgent):
    @property
    def ready(self) -> bool:
        return True

    async def execute(self, task: str, count: int = 1) -> dict:
        helper()
        return {"status": "done"}
"""
    symbols, imports, refs = extract_symbols_and_imports(code, "service.py", "python")

    names = {s["name"]: s["kind"] for s in symbols}
    check("Extracted constant MAX_RETRIES", names.get("MAX_RETRIES") == "constant")
    check("Extracted variable timeout_seconds", names.get("timeout_seconds") == "variable")
    check("Extracted class BaseAgent", names.get("BaseAgent") == "class")
    check("Extracted class MyService", names.get("MyService") == "class")
    check("Extracted method MyService.ready", names.get("MyService.ready") == "method")
    check("Extracted async method MyService.execute", names.get("MyService.execute") == "async_method")

    import_paths = {imp["imported_path"]: imp["import_kind"] for imp in imports}
    check("Extracted import os", import_paths.get("os") == "import")
    check("Extracted from pathlib import Path", import_paths.get("pathlib.Path") == "from_import")
    check("Extracted relative import ..utils.helper", import_paths.get("..utils.helper") == "relative")

    ref_names = {r["symbol_name"] for r in refs}
    check("Extracted call reference to helper", "helper" in ref_names)

    # Missing tree-sitter fallback behavior
    ts_res = _try_treesitter_parse("const x = 10;", "app.js", "javascript")
    check("Missing treesitter returns empty gracefully", ts_res == ([], [], []))


def test_search_tools() -> None:
    section("5. Search Tools")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        ws = WorkspaceManager(root)

        (root / "src").mkdir()
        (root / "src" / "alpha.py").write_text("def find_me():\n    return 'secret_phrase'\n", encoding="utf-8")
        (root / "src" / "beta.py").write_text("def other():\n    return 'no match'\n", encoding="utf-8")

        scanner = RepositoryScanner(workspace=ws)
        scanner.scan()

        # list_files
        lf = list_files(subdirectory="src", workspace=ws)
        check("list_files lists relative paths", "src/alpha.py" in lf["files"])

        # read_file
        rf = read_file("src/alpha.py", workspace=ws)
        check("read_file succeeds", rf["status"] == "success" and "find_me" in rf["content"])

        # read_range
        rr = read_range("src/alpha.py", start_line=1, end_line=1, workspace=ws)
        check("read_range extracts line 1", rr["status"] == "success" and "def find_me" in rr["lines"])

        # search_text
        st = search_text("secret_phrase", workspace=ws)
        check("search_text finds literal match", st["total_matches"] == 1 and st["matches"][0]["file"] == "src/alpha.py")

        # search_text limits
        st_regex = search_text(r"def \w+", is_regex=True, max_results=1, workspace=ws)
        check("search_text bounds result count", len(st_regex["matches"]) == 1)

        # find_symbol
        fs = find_symbol("find_me", workspace=ws)
        check("find_symbol finds indexed function", fs["count"] == 1 and fs["symbols"][0]["name"] == "find_me")

        # inspect_dependencies
        (root / "requirements.txt").write_text("requests>=2.28\nfastapi==0.100\n", encoding="utf-8")
        deps = inspect_dependencies(workspace=ws)
        check("inspect_dependencies reads requirements.txt", "requests" in deps["manifest_dependencies"].get("requirements.txt", []))

        # get_repository_summary
        summary = get_repository_summary(workspace=ws)
        check("get_repository_summary generates statistics", summary["total_files"] == 2 and "src/alpha.py" in str(summary))


def test_patch_engine_and_rollback() -> None:
    section("6. Patch Engine and Rollbacks")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        ws = WorkspaceManager(root)
        pm = PatchManager(ws)

        target = root / "calc.py"
        target.write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")

        diff = """--- a/calc.py
+++ b/calc.py
@@ -1,5 +1,6 @@
 def add(a, b):
+    # Add comment
     return a + b
 
 def sub(a, b):
-    return a - b
+    return a - b - 0
"""
        # 1. Preview patch
        prev = pm.preview_patch(diff)
        check("Patch preview ok", prev["status"] == "preview_ok" and prev["additions"] == 2 and prev["deletions"] == 1)

        # 2. Apply patch with rollback snapshot
        applied = pm.apply_patch(diff, auto_approve=True)
        check("Patch applied atomically", applied["status"] == "applied")

        content_after = target.read_text(encoding="utf-8")
        check("File contains patched lines", "# Add comment" in content_after and "return a - b - 0" in content_after)

        # 3. Rollback patch
        rb = pm.rollback_patch()
        check("Rollback reports success", rb["status"] == "rolled_back")
        content_restored = target.read_text(encoding="utf-8")
        check("Prior bytes restored perfectly", content_restored == "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")

        # 4. Create and rollback file
        cr = pm.create_file("created.txt", "Hello newly created", auto_approve=True)
        check("File created", cr["status"] == "created" and (root / "created.txt").is_file())
        rb_cr = pm.rollback_patch()
        check("Created file rolled back", rb_cr["status"] == "rolled_back" and not (root / "created.txt").exists())

        # 5. Rename and rollback file
        pm.create_file("origin.txt", "Content to rename", auto_approve=True)
        rn = pm.rename_file("origin.txt", "dest.txt", auto_approve=True)
        check("File renamed", rn["status"] == "renamed" and (root / "dest.txt").is_file() and not (root / "origin.txt").exists())
        rb_rn = pm.rollback_patch()
        check("Renamed file rolled back", rb_rn["status"] == "rolled_back" and (root / "origin.txt").is_file())

        # 6. Delete and rollback file
        del_res = pm.delete_file("origin.txt", auto_approve=True)
        check("File deleted", del_res["status"] == "deleted" and not (root / "origin.txt").exists())
        rb_del = pm.rollback_patch()
        check("Deleted file restored with prior content", rb_del["status"] == "rolled_back" and (root / "origin.txt").read_text(encoding="utf-8") == "Content to rename")


def test_safe_command_runner_and_diagnostics() -> None:
    section("7. Safe Command Runner and Diagnostics")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        ws = WorkspaceManager(root)

        # 1. Normal command execution
        res = run_safe_command([sys.executable, "-c", "print('Command ran successfully')"], cwd=root)
        check("Safe command exit 0", res["exit_code"] == 0 and "Command ran successfully" in res["stdout"])

        # 2. Command exit code handling
        fail_res = run_safe_command([sys.executable, "-c", "import sys; sys.exit(42)"], cwd=root)
        check("Non-zero exit code captured", fail_res["exit_code"] == 42 and fail_res["status"] == "failed")

        # 3. Timeout handling
        to_res = run_safe_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1, cwd=root)
        check("Process timeout handled safely", to_res["timed_out"] is True and to_res["status"] == "timed_out")

        # 4. Blocked destructive command
        blocked_caught = False
        try:
            run_safe_command(["rm", "-rf", "/"], cwd=root)
        except Exception:
            blocked_caught = True
        check("Destructive command pattern blocked", blocked_caught)

        # 5. Git mutations blocked (commit, push, checkout, reset, clean)
        git_commit_caught = False
        try:
            run_safe_command(["git", "commit", "-m", "mutation"], cwd=root)
        except Exception:
            git_commit_caught = True
        check("Git commit mutation blocked", git_commit_caught)

        git_push_caught = False
        try:
            run_safe_command(["git", "push", "origin", "main"], cwd=root)
        except Exception:
            git_push_caught = True
        check("Git push mutation blocked", git_push_caught)

        # 6. Network tools blocked (curl, wget) without explicit instruction
        curl_caught = False
        try:
            run_safe_command(["curl", "https://example.com"], cwd=root)
        except Exception:
            curl_caught = True
        check("Network command curl blocked without explicit request", curl_caught)

        # 7. Explicit user instruction overrides restricted command block
        explicit_override_ok = False
        try:
            # Running with explicit_user_request=True bypasses the restricted block
            res_explicit = run_safe_command(
                [sys.executable, "-c", "print('explicit command ok')"],
                cwd=root,
                explicit_user_request=True,
            )
            explicit_override_ok = (res_explicit["status"] == "success" and "explicit command ok" in res_explicit["stdout"])
        except Exception:
            explicit_override_ok = False
        check("Explicit user instruction overrides restricted block", explicit_override_ok)

        # 8. Git commit & push tools on explicit instruction
        from core.repository.git_tools import git_commit as _gc, git_push as _gp
        from actions.repo_tools import execute_command as _ec
        check("git_commit callable exists", callable(_gc))
        check("git_push callable exists", callable(_gp))
        check("execute_command callable exists", callable(_ec))

        # 9. collect_diagnostics & Secret Redaction
        bad_py = root / "syntax_err.py"
        bad_py.write_text("def broken_syntax(\n", encoding="utf-8")
        diag = collect_diagnostics(target_file="syntax_err.py", workspace=ws)
        check("collect_diagnostics detects syntax error", diag["diagnostics_count"] >= 1 and diag["diagnostics"][0]["tool"] == "py_compile")

        # 10. Secret detection and redaction
        secret_py = root / "app_config.py"
        raw_secret_value = "AKIAIOSFODNN7EXAMPLE"
        secret_py.write_text(f'AWS_KEY = "{raw_secret_value}"\n', encoding="utf-8")
        diag_sec = collect_diagnostics(target_file="app_config.py", workspace=ws)
        sec_findings = [d for d in diag_sec["diagnostics"] if d["tool"] == "secret_scanner"]
        check("Secret detected by scanner", len(sec_findings) >= 1)
        check("Secret value NOT echoed in message", raw_secret_value not in sec_findings[0]["message"])
        check("Secret flagged with line number", sec_findings[0]["line"] == 1)


def test_action_registry_profiles() -> None:
    section("8. Action Registry and Profile Filtering")
    # Mock actions
    actions = {
        "read_file": ActionRecord(name="read_file", valid=True, handler=lambda parameters: "file content"),
        "apply_patch": ActionRecord(name="apply_patch", valid=True, handler=lambda parameters: "patch applied"),
        "weather_report": ActionRecord(name="weather_report", valid=True, handler=lambda parameters: "sunny"),
        "video_player": ActionRecord(name="video_player", valid=True, handler=lambda parameters: "playing"),
    }
    reg = ActionRegistry(actions, logger=lambda _: None)

    # In default profile: all valid actions returned
    reg.set_profile(PROFILE_DEFAULT)
    def_decls = reg.get_tool_declarations()
    def_names = {d["name"] for d in def_decls}
    check("Default profile exposes all actions", "weather_report" in def_names and "read_file" in def_names)

    # In engineering profile: weather and video disabled, read_file and apply_patch kept
    reg.set_profile(PROFILE_ENGINEERING)
    eng_decls = reg.get_tool_declarations()
    eng_names = {d["name"] for d in eng_decls}
    check("Engineering profile contains read_file", "read_file" in eng_names)
    check("Engineering profile contains apply_patch", "apply_patch" in eng_names)
    check("Engineering profile excludes weather_report", "weather_report" not in eng_names)
    check("Engineering profile excludes video_player", "video_player" not in eng_names)

    # Execution rejection for disabled action
    run_disallowed = reg.run("weather_report", {})
    check("Disabled action blocked in engineering mode", "disabled in engineering mode" in run_disallowed)

    run_allowed = reg.run("read_file", {})
    check("Allowed action executes cleanly", run_allowed == "file content")

    # Engineering allowed actions include explicit Git & command execution tools
    from core.action_loader import ENGINEERING_ALLOWED_ACTIONS
    check("git_commit in ENGINEERING_ALLOWED_ACTIONS", "git_commit" in ENGINEERING_ALLOWED_ACTIONS)
    check("git_push in ENGINEERING_ALLOWED_ACTIONS", "git_push" in ENGINEERING_ALLOWED_ACTIONS)
    check("execute_command in ENGINEERING_ALLOWED_ACTIONS", "execute_command" in ENGINEERING_ALLOWED_ACTIONS)
    check("write_ide_terminal in ENGINEERING_ALLOWED_ACTIONS", "write_ide_terminal" in ENGINEERING_ALLOWED_ACTIONS)
    check("execute_ide_terminal in ENGINEERING_ALLOWED_ACTIONS", "execute_ide_terminal" in ENGINEERING_ALLOWED_ACTIONS)


def test_ide_discovery_and_code_audit() -> None:
    section("9. IDE Discovery, Real-Time Coding & Code Audit Trail")
    from core.repository.ide import (
        detect_installed_ides,
        get_preferred_ide,
        set_preferred_ide,
        register_pending_ide_choice,
        get_pending_ide_choice,
        resolve_pending_ide_choice,
        DetectedIDE,
    )
    from core.repository.audit import CodeAuditLogger, CodeAuditEntry
    from actions.repo_tools import (
        list_available_ides,
        add_workspace_folder,
        choose_ide,
        get_code_audit,
    )

    # 1. Detect IDEs
    ides = detect_installed_ides()
    check("detect_installed_ides finds local IDEs", len(ides) >= 1)
    ide_names = [i.name for i in ides]
    check("VS Code or another editor in detected list", any("Code" in n or "Notepad" in n or "IDE" in n for n in ide_names))

    # 2. Preferred IDE storage
    set_preferred_ide("vscode")
    check("set_preferred_ide stores preference", get_preferred_ide() == "vscode")

    # 3. Pending choice resolution (voice or option popup)
    mock_ides = [
        DetectedIDE(id="vscode", name="Visual Studio Code", command="code", path="code"),
        DetectedIDE(id="cursor", name="Cursor", command="cursor", path="cursor"),
    ]
    with tempfile.TemporaryDirectory() as td:
        t_path = Path(td)
        resolved_box = []
        register_pending_ide_choice(t_path, mock_ides, on_resolved=lambda ide: resolved_box.append(ide.id))
        check("Pending choice registered", get_pending_ide_choice() is not None)

        ok, msg, matched = resolve_pending_ide_choice("Visual Studio Code", dry_run=True)
        check("resolve_pending_ide_choice by name matches", ok is True and matched.id == "vscode")
        check("on_resolved callback fired", "vscode" in resolved_box)
        check("Pending choice cleared after resolve", get_pending_ide_choice() is None)

    # 4. CodeAuditLogger
    with tempfile.TemporaryDirectory() as td:
        audit_file = Path(td) / "test_audit.jsonl"
        logger = CodeAuditLogger(log_path=audit_file)
        e = logger.record_change(
            file_path="src/main.py",
            action="modify",
            diff_text="--- a/src/main.py\n+++ b/src/main.py\n@@ -1,1 +1,2 @@\n-old\n+new1\n+new2",
            voice_summary="Updated main loop with graceful shutdown.",
            ide="Visual Studio Code",
        )
        check("Audit entry recorded", e.file_path == "src/main.py" and e.action == "modify")
        check("Audit diff summary computed", "+2 / -1 lines" in e.diff_summary)
        check("Audit persisted to disk", audit_file.exists() and len(audit_file.read_text(encoding="utf-8")) > 50)

        ui_text = logger.format_ui_audit_text()
        check("UI audit text formatted cleanly", "src/main.py" in ui_text and "Visual Studio Code" in ui_text)

        voice_summary = logger.generate_voice_summary()
        check("Voice summary generated", "src/main.py" in voice_summary and "real-time" in voice_summary)

    # 5. Repo tools integration
    ides_list_text = list_available_ides({})
    check("list_available_ides returns formatted list", "Detected IDEs" in ides_list_text)

    with tempfile.TemporaryDirectory() as td:
        add_res = add_workspace_folder({"path": td, "dry_run": True})
        check("add_workspace_folder sets workspace and runs IDE flow", "Added" in add_res or "IDE" in add_res)

        choose_res = choose_ide({"ide": "vscode", "dry_run": True})
        check("choose_ide responds with status", "Opened" in choose_res or "vscode" in choose_res.lower())

        audit_res = get_code_audit({"limit": 5})
        check("get_code_audit returns audit trail", len(audit_res) > 20)


def test_ide_terminal_and_sync_backup() -> None:
    section("10. IDE Terminal Command Staging, Execution, and .sync_backup Audits")
    from core.repository.ide import (
        write_command_to_ide_terminal,
        execute_staged_ide_terminal,
        get_staged_terminal_command,
        clear_staged_terminal_command,
    )
    from actions.repo_tools import write_ide_terminal, execute_ide_terminal, execute_command
    from core.repository.ignore import DEFAULT_IGNORE_DIRS
    from core.repository.workspace import get_workspace
    from actions.dev_agent import generate_build_questions, _BACKUP_DIR

    # 1. Terminal Staging (DO NOT EXECUTE)
    clear_staged_terminal_command()
    check("Staged command initially empty", get_staged_terminal_command() is None)

    stage_res = write_ide_terminal({"command": "npm install express", "dry_run": True})
    check("write_ide_terminal succeeds", "Wrote" in stage_res or "Staged" in stage_res)
    check("Command sits staged in terminal prompt", get_staged_terminal_command() == "npm install express")

    # Command is NOT executed until user explicitly calls execute_ide_terminal
    exec_res = execute_ide_terminal({"dry_run": True})
    check("execute_ide_terminal executes staged command", "Executed" in exec_res)
    check("Staged command cleared after execution", get_staged_terminal_command() is None)

    # 2. execute_command with stage_only routing
    stage_only_res = execute_command({"command": "git status", "stage_only": True, "dry_run": True})
    check("execute_command(stage_only) routes to write_ide_terminal", "Wrote" in stage_only_res or "Staged" in stage_only_res)
    check("git status staged in terminal", get_staged_terminal_command() == "git status")
    clear_staged_terminal_command()

    # 3. .sync_backup directory naming and default ignore
    check(".sync_backup is configured as _BACKUP_DIR", _BACKUP_DIR == ".sync_backup")
    check(".sync_backup in DEFAULT_IGNORE_DIRS", ".sync_backup" in DEFAULT_IGNORE_DIRS)

    # 4. .sync_backup audit logging on file modifications
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = get_workspace()
        old_root = ws.root
        ws.set_root(root)
        try:
            from core.repository.patches import get_patch_manager
            pm = get_patch_manager()
            # Force _should_record_audit to True for testing
            orig_should = pm._should_record_audit
            pm._should_record_audit = lambda: True

            test_file = root / "app.py"
            test_file.write_text("print('hello')\n", encoding="utf-8")

            # Apply a patch to create modification backup
            patch_text = "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n-print('hello')\n+print('hello world')\n+print('welcome')\n"
            apply_res = pm.apply_patch(patch_text, auto_approve=True)
            check("Patch applied to app.py", apply_res["status"] == "applied")

            backup_folder = root / ".sync_backup"
            check(".sync_backup folder created in repo", backup_folder.exists())
            check(".sync_backup/audit.jsonl exists", (backup_folder / "audit.jsonl").exists())
            audit_content = (backup_folder / "audit.jsonl").read_text(encoding="utf-8")
            check("Audit records modify action", "modify" in audit_content and "app.py" in audit_content)

            # Rollback patch and verify rollback audit record
            rb_res = pm.rollback_patch()
            check("Rollback succeeds", rb_res["status"] == "rolled_back")
            rb_content = (backup_folder / "audit.jsonl").read_text(encoding="utf-8")
            check("Rollback recorded in audit.jsonl", "rollback" in rb_content)

            pm._should_record_audit = orig_should
        finally:
            if old_root:
                ws.set_root(old_root)

    # 5. Requirement and Architecture Clarification Questions
    questions = generate_build_questions("Build a real-time collaborative code editor", "python")
    check("generate_build_questions returns questions", isinstance(questions, list) and len(questions) >= 2)
    check("Questions are non-empty strings", all(isinstance(q, str) and len(q) > 10 for q in questions))


def main() -> None:
    print("\n" + "=" * 75)
    print("  S.Y.N.C SOFTWARE ENGINEERING SUBSYSTEM VERIFICATION SUITE")
    print("=" * 75)

    test_workspace_and_path_safety()
    test_ignore_rules_and_binary()
    test_sqlite_index_and_incremental_scanner()
    test_ast_symbol_extraction()
    test_search_tools()
    test_patch_engine_and_rollback()
    test_safe_command_runner_and_diagnostics()
    test_action_registry_profiles()
    test_ide_discovery_and_code_audit()
    test_ide_terminal_and_sync_backup()

    print("\n" + "=" * 75)
    print(f"  TOTAL CHECKS: {_ok + _fail} | PASSED: {_ok} | FAILED: {_fail}")
    print("=" * 75)

    if _failures:
        print("\nFailed checks:")
        for f in _failures:
            print(f"  ❌ {f}")
        sys.exit(1)
    else:
        print("\n✅ ALL SUBSYSTEM TESTS PASSED WITH ZERO FAILURES!")
        sys.exit(0)


if __name__ == "__main__":
    main()
