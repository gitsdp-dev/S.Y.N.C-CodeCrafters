"""
core/repository/dependencies.py — Project dependency inspection.

Extracts dependencies from manifest files (requirements.txt, package.json,
Cargo.toml, pyproject.toml) and cross-references them against imported modules.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from core.repository.index import RepositoryIndex
from core.repository.workspace import WorkspaceManager, get_workspace


def inspect_dependencies(
    workspace: Optional[WorkspaceManager] = None,
    index: Optional[RepositoryIndex] = None,
) -> dict:
    """
    Inspect dependencies from project manifests and the index imports table.
    """
    ws = workspace or get_workspace()
    root = ws.root
    if not root:
        return {"error": "No active workspace configured."}

    manifest_deps: dict[str, list[str]] = {}

    # 1. Python requirements.txt
    req_file = root / "requirements.txt"
    if req_file.is_file():
        try:
            lines = req_file.read_text(encoding="utf-8", errors="replace").splitlines()
            deps = []
            for line in lines:
                l = line.strip()
                if l and not l.startswith("#"):
                    # Strip comments and environment markers
                    pkg = re.split(r"[;<=>~!#]", l)[0].strip()
                    if pkg:
                        deps.append(pkg)
            if deps:
                manifest_deps["requirements.txt"] = deps
        except Exception:
            pass

    # 2. Python pyproject.toml
    pyproject_file = root / "pyproject.toml"
    if pyproject_file.is_file():
        try:
            content = pyproject_file.read_text(encoding="utf-8", errors="replace")
            # Extract dependencies list loosely without requiring tomli
            matches = re.findall(r'["\']([a-zA-Z0-9_\-\.]+)(?:[>=<~^!].*?)?["\']', content)
            if matches:
                manifest_deps["pyproject.toml"] = matches[:50]
        except Exception:
            pass

    # 3. Node package.json
    pkg_file = root / "package.json"
    if pkg_file.is_file():
        try:
            data = json.loads(pkg_file.read_text(encoding="utf-8", errors="replace"))
            deps = list(data.get("dependencies", {}).keys())
            dev_deps = list(data.get("devDependencies", {}).keys())
            if deps:
                manifest_deps["dependencies"] = deps
            if dev_deps:
                manifest_deps["devDependencies"] = dev_deps
        except Exception:
            pass

    # 4. Rust Cargo.toml
    cargo_file = root / "Cargo.toml"
    if cargo_file.is_file():
        try:
            content = cargo_file.read_text(encoding="utf-8", errors="replace")
            deps = re.findall(r"^([a-zA-Z0-9_-]+)\s*=", content, re.MULTILINE)
            if deps:
                manifest_deps["Cargo.toml"] = deps
        except Exception:
            pass

    # 5. Go go.mod
    go_file = root / "go.mod"
    if go_file.is_file():
        try:
            content = go_file.read_text(encoding="utf-8", errors="replace")
            deps = re.findall(r"require\s*\(\s*(.*?)\s*\)", content, re.DOTALL)
            if deps:
                items = [re.split(r"\s+", line.strip())[0] for line in deps[0].splitlines() if line.strip()]
                manifest_deps["go.mod"] = items
        except Exception:
            pass

    # Query index for top imported modules
    top_imported: list[dict] = []
    if index is None:
        try:
            index = RepositoryIndex(root)
        except Exception:
            index = None

    if index:
        try:
            top_imported = index.get_imports(limit=100)
        except Exception:
            pass

    return {
        "repository": root.name,
        "manifests_detected": list(manifest_deps.keys()),
        "manifest_dependencies": manifest_deps,
        "indexed_imports_sample": top_imported[:30],
    }
