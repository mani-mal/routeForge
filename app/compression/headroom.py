"""Real Headroom ML compressor — opt-in behind the [compression] extra.
Lazy: __init__ raises ImportError when headroom-ai isn't installed, so
select_compressor() cleanly falls back to the deterministic default."""
from __future__ import annotations


class HeadroomCompressor:
    name = "headroom"

    def __init__(self) -> None:
        from headroom import compress as _hr_compress  # ImportError if absent
        self._compress = _hr_compress

    def compress_text(self, text: str) -> str:
        result = self._compress(text)
        # headroom returns a result object/string depending on version; normalize
        return getattr(result, "text", None) or getattr(result, "compressed", None) or str(result)
