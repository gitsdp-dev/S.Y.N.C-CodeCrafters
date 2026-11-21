"""
core/repository/symbols.py — AST-based source code symbol and dependency extraction.

Uses the Python standard library 'ast' module to extract classes, functions,
async functions, methods, decorators, constants, variables, imports, and calls.
Tree-sitter is optionally supported via lazy guarded imports without adding
hard dependencies.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sql": "sql",
}


def detect_language(path: Path | str) -> str:
    """Detect language name from file extension, defaulting to 'text'."""
    p = Path(path)
    return LANGUAGE_BY_EXT.get(p.suffix.lower(), "text")


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format a human-readable function signature with parameters and return annotation."""
    args = node.args
    parts: list[str] = []

    # Positional only args
    for a in getattr(args, "posonlyargs", []):
        arg_str = a.arg
        if a.annotation:
            arg_str += f": {ast.unparse(a.annotation)}"
        parts.append(arg_str)
    if getattr(args, "posonlyargs", []):
        parts.append("/")

    # Standard args
    defaults_offset = len(args.args) - len(args.defaults)
    for idx, a in enumerate(args.args):
        arg_str = a.arg
        if a.annotation:
            arg_str += f": {ast.unparse(a.annotation)}"
        if idx >= defaults_offset:
            default_val = args.defaults[idx - defaults_offset]
            arg_str += f"={ast.unparse(default_val)}"
        parts.append(arg_str)

    # Vararg (*args)
    if args.vararg:
        v_str = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            v_str += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(v_str)
    elif args.kwonlyargs:
        parts.append("*")

    # Keyword only args
    for idx, a in enumerate(args.kwonlyargs):
        arg_str = a.arg
        if a.annotation:
            arg_str += f": {ast.unparse(a.annotation)}"
        if idx < len(args.kw_defaults) and args.kw_defaults[idx] is not None:
            arg_str += f"={ast.unparse(args.kw_defaults[idx])}"
        parts.append(arg_str)

    # Kwarg (**kwargs)
    if args.kwarg:
        kw_str = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            kw_str += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(kw_str)

    sig = f"({', '.join(parts)})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


class _PythonASTVisitor(ast.NodeVisitor):
    """Walks Python AST to extract symbols, imports, and calls."""

    def __init__(self, rel_path: str):
        self.rel_path = rel_path
        self.symbols: list[dict] = []
        self.imports: list[dict] = []
        self.references: list[dict] = []
        self._class_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        name = node.name
        full_name = f"{'.'.join(self._class_stack)}.{name}" if self._class_stack else name

        decorators = [ast.unparse(d) for d in node.decorator_list]
        bases = [ast.unparse(b) for b in node.bases]
        bases_str = f"({', '.join(bases)})" if bases else ""

        sig = f"class {name}{bases_str}"
        if decorators:
            sig = f"@{', '.join(decorators)} {sig}"

        self.symbols.append({
            "name": full_name,
            "kind": "class",
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", node.lineno),
            "signature": sig,
        })

        self._class_stack.append(name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node, is_async=True)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        name = node.name
        is_method = bool(self._class_stack)
        full_name = f"{'.'.join(self._class_stack)}.{name}" if self._class_stack else name

        if is_method:
            kind = "async_method" if is_async else "method"
        else:
            kind = "async_function" if is_async else "function"

        try:
            param_sig = _format_signature(node)
        except Exception:
            param_sig = "()"

        prefix = "async def " if is_async else "def "
        decorators = [ast.unparse(d) for d in node.decorator_list]
        sig = f"{prefix}{name}{param_sig}"
        if decorators:
            sig = f"@{', '.join(decorators)} {sig}"

        self.symbols.append({
            "name": full_name,
            "kind": kind,
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", node.lineno),
            "signature": sig,
        })

        # Do not enter inner classes/functions for class stack, but visit body
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Check for top-level or class-level constants (UPPER_CASE)
        if not self._class_stack:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    val = ""
                    try:
                        val = ast.unparse(node.value)
                        if len(val) > 40:
                            val = val[:37] + "..."
                    except Exception:
                        pass
                    self.symbols.append({
                        "name": target.id,
                        "kind": "constant",
                        "line_start": node.lineno,
                        "line_end": getattr(node, "end_lineno", node.lineno),
                        "signature": f"{target.id} = {val}" if val else target.id,
                    })
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self._class_stack and isinstance(node.target, ast.Name):
            name = node.target.id
            kind = "constant" if name.isupper() else "variable"
            anno = ""
            try:
                anno = ast.unparse(node.annotation)
            except Exception:
                pass
            self.symbols.append({
                "name": name,
                "kind": kind,
                "line_start": node.lineno,
                "line_end": getattr(node, "end_lineno", node.lineno),
                "signature": f"{name}: {anno}" if anno else name,
            })
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append({
                "imported_path": alias.name,
                "import_kind": "import",
                "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        level = node.level or 0
        kind = "relative" if level > 0 else "from_import"
        prefix = "." * level

        for alias in node.names:
            full_imported = f"{prefix}{module}.{alias.name}" if module else f"{prefix}{alias.name}"
            self.imports.append({
                "imported_path": full_imported,
                "import_kind": kind,
                "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Extract function/class calls as references
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr

        if name and len(name) > 1 and not name.startswith("__"):
            self.references.append({
                "symbol_name": name,
                "line": node.lineno,
            })
        self.generic_visit(node)


def extract_symbols_and_imports(
    source_code: str,
    rel_path: str,
    language: Optional[str] = None,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Extract symbols, imports, and references from source code.

    Returns (symbols_list, imports_list, references_list).
    Never raises exceptions on syntax or parsing errors.
    """
    lang = language or detect_language(rel_path)
    symbols: list[dict] = []
    imports: list[dict] = []
    references: list[dict] = []

    if lang == "python":
        try:
            tree = ast.parse(source_code, filename=rel_path)
            visitor = _PythonASTVisitor(rel_path)
            visitor.visit(tree)
            return visitor.symbols, visitor.imports, visitor.references
        except (SyntaxError, ValueError, MemoryError):
            # Return empty on parse failure so scanner can continue
            return [], [], []

    # Optional Tree-sitter integration for non-python if available
    # Keep it entirely guarded so missing tree-sitter never breaks S.Y.N.C
    try:
        symbols, imports, references = _try_treesitter_parse(source_code, rel_path, lang)
    except Exception:
        pass

    return symbols, imports, references


def _try_treesitter_parse(
    source_code: str,
    rel_path: str,
    language: str,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Guarded optional Tree-sitter extractor. Returns empty if not installed."""
    # Lazy import to avoid any startup overhead or requirement
    try:
        import tree_sitter  # type: ignore # noqa: F401
    except ImportError:
        return [], [], []

    # If tree-sitter is present in the environment, additional language grammars
    # can be queried here without breaking environments that don't have it.
    return [], [], []
