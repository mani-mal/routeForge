"""RouteForge runs no agent of its own (2026-09-17 removal): tools belong to
the calling application. Guards against the NAT agent creeping back in."""
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def test_no_module_imports_nat_or_app_agent():
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            if (stripped.startswith(("import nat", "from nat"))
                    or "app.agent" in stripped):
                offenders.append(f"{path.relative_to(APP.parent)}: {stripped}")
    assert offenders == []


def test_app_agent_package_is_gone():
    assert not (APP / "agent").exists()


def test_nat_is_not_a_dependency():
    pyproject = (APP.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "nvidia-nat" not in pyproject
    assert "nat_tools" not in pyproject
