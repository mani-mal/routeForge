"""Minimal local JSONL dataset loader for the DSPy prompt optimizer.

No `{input, label}` dataset exists in this repo -- feedback lives only in
Langfuse, and assembling a local training file is an external, residency-
bound activity (see `docs/guides/exporting-eval-datasets.md`): raw-text
training data may only be exported from a `full`-capture, residency-local
Langfuse project. This loader itself does no network I/O and never reaches
into Langfuse in-process -- it just reads a caller-supplied local file.

Format: one JSON object per non-blank line, `{"text": str, "label":
"direct"|"multi_step"}`.

Fail-closed (CLAUDE.md rule 5): any malformed line, unparseable JSON, missing
field, or unknown label raises `ValueError` immediately -- never silently
dropped. A silently-shrunk training set is a correctness bug, not a
convenience.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

VALID_LABELS = ("direct", "multi_step")


class Example(BaseModel):
    text: str
    label: str


def load_dataset(path: str) -> list[Example]:
    """Read and validate every line of `path`. Raises `ValueError` naming the
    line number on any malformed/unparseable/mislabeled entry; raises
    `ValueError` if the file contains no examples at all (an empty training
    set is a config mistake, not a valid input)."""
    p = Path(path)
    if not p.exists():
        raise ValueError(f"{path}: dataset file not found")

    examples: list[Example] = []
    for lineno, raw_line in enumerate(p.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: unparseable JSON ({e})") from e
        if not isinstance(raw, dict):
            raise ValueError(f"{path}:{lineno}: line does not parse to a JSON object")
        if "text" not in raw or "label" not in raw:
            raise ValueError(f"{path}:{lineno}: missing required field(s) 'text'/'label'")
        text, label = raw["text"], raw["label"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{path}:{lineno}: 'text' must be a non-empty string")
        if label not in VALID_LABELS:
            raise ValueError(
                f"{path}:{lineno}: label {label!r} not in {VALID_LABELS}")
        examples.append(Example(text=text, label=label))

    if not examples:
        raise ValueError(f"{path}: no examples found (empty dataset)")
    return examples
