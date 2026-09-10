"""The docs link + vocabulary gate, as a pytest failure.

scripts/check_docs_links.py is the implementation; this makes it run in the
normal suite so a stale doc path fails CI like any other regression.
"""
import subprocess
import sys
from pathlib import Path

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
