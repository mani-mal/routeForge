"""Compressor implementations. The DeterministicCompressor is the offline,
zero-download default (tests + laptop demo); HeadroomCompressor (Task 5) is the
real ML sibling behind the [compression] extra. Both satisfy `Compressor`."""
from __future__ import annotations

import re
from typing import Protocol


class Compressor(Protocol):
    name: str

    def compress_text(self, text: str) -> str: ...


_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+(\n)")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


class DeterministicCompressor:
    """Deterministic, offline reduction of verbose content: collapse blank-line
    runs, strip redundant intra-line whitespace, and drop consecutive duplicate
    lines. Real reduction, no model, no network, idempotent."""

    name = "deterministic"

    def compress_text(self, text: str) -> str:
        # Strip trailing whitespace BEFORE collapsing blank runs: whitespace-only
        # lines must become truly empty first, else _TRAILING_WS would create a
        # fresh \n{3,} run that _MULTI_BLANK already passed over — breaking
        # idempotence (a second call would collapse it and differ).
        text = _TRAILING_WS.sub(r"\1", text)
        text = _MULTI_BLANK.sub("\n\n", text)
        text = _MULTI_SPACE.sub(" ", text)
        out_lines: list[str] = []
        prev: str | None = None
        for line in text.split("\n"):
            if line == prev and line.strip() != "":
                continue  # drop consecutive duplicate non-blank lines
            out_lines.append(line)
            prev = line
        return "\n".join(out_lines)
