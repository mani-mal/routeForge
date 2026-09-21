"""Serving isolation (spec §3, §10 test 8): nothing under `app/` outside
`app/eval/` may import `app.eval` -- the harness is operator-run tooling,
never part of the serving process."""
from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2] / "app"
EVAL_DIR = APP_DIR / "eval"


def _imports_app_eval(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names
                     if a.name == "app.eval" or a.name.startswith("app.eval.")]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "app.eval" or node.module.startswith("app.eval."):
                hits.append(node.module)
            elif node.module == "app" and any(a.name == "eval" for a in node.names):
                hits.append("app.eval")
    return hits


def test_no_serving_module_imports_app_eval():
    offenders = {}
    for path in APP_DIR.rglob("*.py"):
        if EVAL_DIR in path.parents:
            continue
        hits = _imports_app_eval(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders[str(path.relative_to(APP_DIR.parent))] = hits
    assert offenders == {}


def test_scanner_detects_an_import():
    """Falsify the scanner itself: each import spelling must be caught."""
    for src in ("import app.eval.runner", "from app.eval import runner",
                "from app.eval.oracle import MatrixRow", "from app import eval"):
        assert _imports_app_eval(ast.parse(src)), src
    assert not _imports_app_eval(ast.parse("from app.evaluation import x"))
