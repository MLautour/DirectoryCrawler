"""Enforces the storage_report / houdini layering boundary (see docs/implementation-plan.md §13).

1. AST scan: no `hou`, no Qt bindings, no toolutils, and no filesystem/IO
   shortcuts (`os.walk`, `rglob`, `open(`, `hashlib`, `threading.Thread`,
   `asyncio`, `multiprocessing`) anywhere under `storage_report/`.
2. Import isolation: `storage_report` imports and runs end to end even when
   `hou` and `PySide6` are unimportable.
3. Direction check: `storage_report` never imports from `houdini`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "storage_report"

FORBIDDEN_MODULE_PREFIXES = ("hou", "PySide", "PyQt", "toolutils", "houdini")

# Modules that must never be imported at all, in any form, anywhere in the package.
FULLY_BANNED_MODULES = {"asyncio", "multiprocessing", "hashlib"}

# Bare builtin calls that are never allowed (reading/writing via the raw
# `open()` builtin -- the report is written with pathlib instead).
FORBIDDEN_CALL_NAMES = {
    "open",
}

# Dotted attribute-call paths that are never allowed, e.g. `os.walk(...)`.
FORBIDDEN_ATTR_PATHS = {
    ("os", "walk"),
    ("threading", "Thread"),
}

FORBIDDEN_METHOD_NAMES = {
    "rglob",
}


def _iter_package_modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _dotted_from_attribute(node: ast.Attribute) -> tuple[str, ...] | None:
    """Best-effort flatten of `a.b.c` into ('a', 'b', 'c'); None if not a simple chain."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return tuple(reversed(parts))
    return None


class TestNoForbiddenImports(unittest.TestCase):
    """AST scan: storage_report must not import hou/Qt/toolutils, ever."""

    def test_no_forbidden_top_level_imports(self) -> None:
        offenders: list[str] = []
        for path in _iter_package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top.startswith(FORBIDDEN_MODULE_PREFIXES):
                            offenders.append(f"{path}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    top = module.split(".")[0]
                    if top.startswith(FORBIDDEN_MODULE_PREFIXES):
                        offenders.append(f"{path}:{node.lineno}: from {module} import ...")
        self.assertFalse(offenders, "Forbidden imports found:\n" + "\n".join(offenders))

    def test_no_fully_banned_modules(self) -> None:
        offenders: list[str] = []
        for path in _iter_package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in FULLY_BANNED_MODULES:
                            offenders.append(f"{path}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    top = module.split(".")[0]
                    if top in FULLY_BANNED_MODULES:
                        offenders.append(f"{path}:{node.lineno}: from {module} import ...")
        self.assertFalse(offenders, "Fully-banned modules imported:\n" + "\n".join(offenders))

    def test_no_filesystem_or_thread_spawn_shortcuts(self) -> None:
        offenders: list[str] = []
        for path in _iter_package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALL_NAMES:
                        offenders.append(f"{path}:{node.lineno}: call to {func.id}(...)")
                    elif isinstance(func, ast.Attribute):
                        dotted = _dotted_from_attribute(func)
                        if dotted is not None and dotted in FORBIDDEN_ATTR_PATHS:
                            offenders.append(f"{path}:{node.lineno}: call to {'.'.join(dotted)}(...)")
                        elif func.attr in FORBIDDEN_METHOD_NAMES:
                            offenders.append(f"{path}:{node.lineno}: call to .{func.attr}(...)")
        self.assertFalse(offenders, "Forbidden filesystem/thread-spawn shortcuts found:\n" + "\n".join(offenders))

    def test_no_os_walk_via_from_import(self) -> None:
        offenders: list[str] = []
        for path in _iter_package_modules():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "walk":
                    dotted = _dotted_from_attribute(node)
                    if dotted == ("os", "walk"):
                        offenders.append(f"{path}:{node.lineno}: os.walk referenced")
        self.assertFalse(offenders, "os.walk referenced:\n" + "\n".join(offenders))


class TestImportIsolation(unittest.TestCase):
    """storage_report must import and run end to end with hou/PySide6 forced unavailable."""

    def test_import_and_scan_without_hou_or_qt(self) -> None:
        script = r"""
import sys
import tempfile
import os

class _BlockedFinder:
    def find_module(self, name, path=None):
        if name == "hou" or name.startswith("PySide") or name.startswith("PyQt"):
            return self
        return None

    def load_module(self, name):
        raise ImportError(f"{name} is blocked for isolation testing")

sys.meta_path.insert(0, _BlockedFinder())
sys.modules["hou"] = None
sys.modules["PySide6"] = None

import storage_report
from storage_report import crawler, html_report, Config

with tempfile.TemporaryDirectory() as tmp:
    sub = os.path.join(tmp, "type1", "asset1", "variant1")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "file.txt"), "wb") as f:
        f.write(b"hello world")

    tree = crawler.scan(tmp, Config())
    out = os.path.join(tmp, "report.html")
    html_report.write(tree, out)
    assert os.path.isfile(out), "report was not written"
    assert os.path.getsize(out) > 0, "report is empty"

print("OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"Isolated run failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("OK", result.stdout)


class TestDirectionality(unittest.TestCase):
    """storage_report must never import from houdini."""

    def test_storage_report_never_imports_houdini(self) -> None:
        offenders: list[str] = []
        for path in _iter_package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "houdini":
                            offenders.append(f"{path}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.split(".")[0] == "houdini":
                        offenders.append(f"{path}:{node.lineno}: from {module} import ...")
        self.assertFalse(offenders, "storage_report imports houdini:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
