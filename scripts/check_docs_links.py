#!/usr/bin/env python3
"""Link + vocabulary gate for docs/.

Three checks:
  1. internal   — every relative markdown link inside docs/ resolves.
  2. inbound    — every `docs/...` path mentioned elsewhere in the repo resolves.
  3. vocabulary — vocabulary purged by the 2026-08-03 pure-router pivot does not
                  appear outside docs/archive/ and docs/superpowers/.

Usage:  python3 scripts/check_docs_links.py
Exit:   0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".worktrees",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".superpowers",
}

SCAN_SUFFIXES = {
    ".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".toml", ".example",
}

# Docs allowed to contain purged vocabulary: they are historical records.
# docs/architecture-review.md is a pre-pivot snapshot that legitimately discusses
# the removed inference_backend/serving_paths axes and the removed local-8b model;
# Task 10 moves it under docs/archive/ and removes this entry.
VOCAB_EXEMPT = ("docs/archive/", "docs/superpowers/", "docs/architecture-review.md")

# The SDD build log is append-only: never link-checked, never edited.
SKIP_PREFIXES = ("docs/superpowers/",)

SELF = Path(__file__).resolve()

# ](target) or ](target "title") — markdown inline links.
MD_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?\s*(?:\"[^\"]*\")?\s*\)")
# docs/foo/bar.md mentioned in prose or a code comment.
DOCS_FILE = re.compile(r"(?<![\w/.-])docs/[A-Za-z0-9_][A-Za-z0-9_./-]*\.md")
# docs/foo/ mentioned as a directory.
DOCS_DIR = re.compile(r"(?<![\w/.-])docs/[A-Za-z0-9_-]+/(?![A-Za-z0-9_.-])")

FORBIDDEN = {
    "inference_backend": "the inference_backend axis was removed by the pure-router pivot",
    "SP_INFERENCE_BACKEND": "the inference_backend env override no longer exists",
    "serving_paths": "there is no serving_paths config block",
    "local-8b": "the bundled demo model was removed; the on-prem pool boots empty",
}


def walk(base: Path):
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def check_internal() -> list[str]:
    problems: list[str] = []
    for md in sorted(walk(DOCS)):
        if md.suffix != ".md":
            continue
        if md.relative_to(ROOT).as_posix().startswith(SKIP_PREFIXES):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for raw in MD_LINK.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target:
                continue  # pure anchor, e.g. ](#section)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # http:, https:, mailto:, …
            if not (md.parent / target).exists():
                problems.append(f"{md.relative_to(ROOT)}: broken link -> {raw}")
    return problems


def check_inbound() -> list[str]:
    problems: list[str] = []
    for path in sorted(walk(ROOT)):
        if path.suffix not in SCAN_SUFFIXES:
            continue
        if DOCS in path.parents:
            continue  # internal links are check 1's job
        if path == SELF:
            continue  # this file names docs/ paths as data, not as references
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT)
        for match in sorted(set(DOCS_FILE.findall(text))):
            if not (ROOT / match).exists():
                problems.append(f"{rel}: stale docs path -> {match}")
        for match in sorted(set(DOCS_DIR.findall(text))):
            if not (ROOT / match).is_dir():
                problems.append(f"{rel}: stale docs dir -> {match}")
    return problems


def check_vocabulary() -> list[str]:
    problems: list[str] = []
    for md in sorted(walk(DOCS)):
        if md.suffix != ".md":
            continue
        rel = md.relative_to(ROOT).as_posix()
        if rel.startswith(VOCAB_EXEMPT):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for term, why in FORBIDDEN.items():
                if term in line:
                    problems.append(f"{rel}:{line_no}: '{term}' — {why}")
    return problems


def main() -> int:
    internal = check_internal()
    inbound = check_inbound()
    vocabulary = check_vocabulary()

    for problem in internal:
        print(f"INTERNAL    {problem}")
    for problem in inbound:
        print(f"INBOUND     {problem}")
    for problem in vocabulary:
        print(f"VOCABULARY  {problem}")

    total = len(internal) + len(inbound) + len(vocabulary)
    print(
        f"\n{len(internal)} broken internal link(s), "
        f"{len(inbound)} stale inbound path(s), "
        f"{len(vocabulary)} purged-vocabulary hit(s)."
    )
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
