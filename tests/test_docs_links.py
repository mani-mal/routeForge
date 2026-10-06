"""The docs link + vocabulary gate, as a pytest failure.

scripts/check_docs_links.py is the implementation; this makes it run in the
normal suite so a stale doc path fails CI like any other regression.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_docs_links.py"


def _run() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_no_broken_internal_doc_links():
    result = _run()
    assert result.returncode == 0, (
        "docs link/vocabulary gate failed:\n" + result.stdout + result.stderr
    )


@pytest.fixture
def checker():
    """`scripts/check_docs_links.py` imported as a module (never executed as
    `__main__`), so its module-level `ROOT`/`DOCS` can be monkeypatched to a
    scratch tree instead of this repo's real `docs/`."""
    spec = importlib.util.spec_from_file_location("check_docs_links", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_code_span_link_syntax_is_not_flagged_but_a_real_broken_link_is(
        tmp_path, monkeypatch, checker):
    """F4a (final review): `MD_LINK` is backtick-blind, so a doc that shows
    `` `a[b](c)` `` as a CODE EXAMPLE of the dotted-path format used
    elsewhere (e.g. `scenarios[0](name).proposal...`) must not be reported
    as a broken link to a file named `c` -- while a real broken link on the
    same page still is."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "page.md").write_text(
        "The format looks like `a[b](c)`, a code span, not a link.\n\n"
        "See [broken](./missing.md) for details.\n"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "DOCS", docs)
    problems = checker.check_internal()
    assert not any("-> c" in p for p in problems), problems
    assert any("missing.md" in p for p in problems), problems
