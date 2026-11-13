"""B100: only app/routing/switchyard/ (and its tests) may import upstream Switchyard.

Importing RouteForge's own adapter (`app.routing.switchyard`) is fine; the
upstream top-level modules `switchyard` and `switchyard_rust` are not.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCAN_DIRS = ("app", "scripts", "tests")
ALLOWED = (ROOT / "app/routing/switchyard", ROOT / "tests/routing/switchyard")
FORBIDDEN = {"switchyard", "switchyard_rust"}


def _top(name: str) -> str:
    return name.split(".")[0]


def _offending_imports(source: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if _top(a.name) in FORBIDDEN]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import inside RouteForge, never upstream.
            if node.level == 0 and node.module and _top(node.module) in FORBIDDEN:
                hits.append(node.module)
    return hits


def test_detector_flags_upstream_and_spares_adapter():
    assert _offending_imports("import switchyard") == ["switchyard"]
    assert _offending_imports("import switchyard.x as y, os") == ["switchyard.x"]
    assert _offending_imports("from switchyard.sub import a") == ["switchyard.sub"]
    assert _offending_imports("from switchyard_rust import a") == ["switchyard_rust"]
    assert _offending_imports("import app.routing.switchyard") == []
    assert _offending_imports("from app.routing.switchyard import SUPPORTED") == []
    assert _offending_imports("from app.routing import switchyard") == []
    assert _offending_imports("from . import switchyard") == []


def test_only_the_adapter_package_imports_switchyard():
    violations: list[str] = []
    scanned = 0
    for d in SCAN_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            if any(a in path.parents for a in ALLOWED):
                continue
            scanned += 1
            for mod in _offending_imports(path.read_text(encoding="utf-8")):
                violations.append(f"{path.relative_to(ROOT)} imports {mod}")
    assert scanned > 100  # the scan really walked the tree
    assert not violations, "\n".join(violations)
