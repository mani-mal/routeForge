#!/usr/bin/env python3
"""Link + vocabulary gate for docs/.

Three checks:
  1. internal   — every relative markdown link inside docs/ resolves (inline
                  `](target)` links and `[ref]: target` reference-style link
                  definitions; a leading-slash target resolves against the
                  repo root, not the file's own directory).
  2. inbound    — every `docs/...` path mentioned elsewhere in the repo resolves.
  3. vocabulary — vocabulary purged by the 2026-08-03 pure-router pivot does not
                  appear outside docs/archive/ and docs/superpowers/.

A line that needs to *name* a purged identifier in order to explain that it no
longer exists (e.g. "There is no `inference_backend` axis") is legitimate,
load-bearing prose, not a violation — mark that one line exempt with a
trailing `<!-- purged-ok -->` HTML comment. The marker is line-scoped, not
file-scoped: everything else in the file stays checked, so a genuine new
misuse elsewhere is still caught.

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
VOCAB_EXEMPT = ("docs/archive/", "docs/superpowers/")

# The SDD build log is append-only: never link-checked, never edited.
SKIP_PREFIXES = ("docs/superpowers/",)

# A line-level exemption for check_vocabulary(): true "it no longer exists"
# statements need to name the purged identifier to be meaningful prose.
VOCAB_LINE_MARKER = "<!-- purged-ok -->"

SELF = Path(__file__).resolve()

# ](target) or ](target "title") — markdown inline links.
MD_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?\s*(?:\"[^\"]*\")?\s*\)")
# [ref]: target — markdown reference-style link definitions.
LINK_DEF = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)
# docs/foo/bar.md mentioned in prose or a code comment.
DOCS_FILE = re.compile(r"(?<![\w/.-])docs/[A-Za-z0-9_][A-Za-z0-9_./-]*\.md")
# docs/foo/ or docs/foo/bar/ mentioned as a directory (one or more segments).
DOCS_DIR = re.compile(
    r"(?<![\w/.-])docs/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/(?![A-Za-z0-9_.-])"
)

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


def _link_target(md: Path, raw: str) -> Path | None:
    """Resolve a link's target to a filesystem path, or None if it isn't one
    (a pure anchor, or an external scheme like http:/https:/mailto:)."""
    target = raw.split("#", 1)[0].strip()
    if not target:
        return None  # pure anchor, e.g. ](#section)
    if re.match(r"^[a-z][a-z0-9+.-]*:", target):
        return None  # http:, https:, mailto:, …
    if target.startswith("/"):
        return ROOT / target.lstrip("/")
    return md.parent / target


def check_internal() -> list[str]:
    problems: list[str] = []
    for md in sorted(walk(DOCS)):
        if md.suffix != ".md":
            continue
        if md.relative_to(ROOT).as_posix().startswith(SKIP_PREFIXES):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        raw_targets = list(MD_LINK.findall(text))
        raw_targets += [target for _label, target in LINK_DEF.findall(text)]
        for raw in raw_targets:
            resolved = _link_target(md, raw)
            if resolved is not None and not resolved.exists():
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
            if VOCAB_LINE_MARKER in line:
                continue  # explicitly marked: names the term to say it's gone
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
