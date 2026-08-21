"""Step 9, streaming variant: a hold-back leakage verifier for the
`incremental_verify` stream mode (see `app/policy/enums.py::STREAM_MODES`).

`incremental_verify` streams REAL backend tokens, but never releases un-redacted
text: it buffers a small hold-back window of the most recent characters and runs
leakage detection over the buffer on every delta. Text is released only once it
sits *behind* the window AND is not part of an entity that is still forming at
the boundary. The canonical failure this prevents: a US SSN arriving split as
`"...123-45-"` then `"6789 ok"` -- a naive forward-the-tokens streamer would
leak `123-45-` before it ever knew an SSN was forming. Here that raw prefix
stays buffered until the whole entity is visible, then it is redacted.

Limitation (documented on purpose, recorded in the trace as verification
`reason="incremental"`): this protects against **entity leakage** across chunk
boundaries only. A *holistic* whole-response verdict -- the `UNVERIFIABLE`
marker `app/output/verify.py` fails closed on -- CANNOT be enforced here, because
tokens have already streamed to the client by the time the tail arrives; there
is no complete-response gate left to reject at. `incremental_verify` is
therefore a DELIBERATELY WEAKER guarantee than `verified_then_stream` (which
buffers the whole answer and runs the full step-9 verdict before any byte
leaves). The residency/risk floor is unaffected: an `onprem_only` or
`require_verification` request never reaches this path -- the pipeline raises
`StreamFallback` and the route falls back to `verified_then_stream`.

Redaction uses the SAME `detector.analyze`/`detector.anonymize` API the
non-stream path uses, so egress placeholders match intake
(`<US_SSN_1>` etc.). Numbering restarts per released chunk (each `anonymize`
call counts from 1): a single entity therefore always renders `<TYPE_1>`, which
matches what intake produces for that entity; multiple entities landing in
*different* released chunks may repeat `_1` -- a cosmetic artifact, never a leak,
and never a case of one entity changing number across feeds (an entity is
redacted exactly once, at release).
"""
from __future__ import annotations

import re

from app.models import Entity, ExecutionPolicyContext
from app.policy.detectors import Detector

HOLDBACK_CHARS = 48
"""Characters kept buffered behind the release point. The window alone bounds
only *fixed/short* entities (SSN/credit-card/phone are all well under 48 chars),
so once such an entity is fully within the buffer `analyze` sees it and the cut
is pulled back before it. It CANNOT bound a length-unbounded pattern
(`EMAIL_ADDRESS = \\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b`): while an email's local
part has streamed but its `@domain` has not, `analyze` returns no entity, so a
fixed window would release the raw local-part prefix. The trailing-entity-ish-run
guard in `feed` covers exactly that case -- together they guarantee no raw entity
text is released across a chunk boundary. (A holistic whole-response verdict
still cannot be enforced once tokens have streamed -- see the module docstring.)"""

# A maximal run of entity-forming characters anchored to the END of the buffer.
# Covers every char the built-in patterns build entities from EXCEPT space (the
# only space-containing pattern, CREDIT_CARD, is length-bounded and handled by
# the window). A trailing run of these chars might still be *growing* into an
# entity on the next feed, so it is never released until the run is broken by a
# non-entity char (proving it is not PII) or completed into a detected entity.
_TRAILING_ENTITY_RUN = re.compile(r"[\w.@+\-]+$")


class StreamingVerifier:
    """Hold-back streaming redactor. Construct once per streamed request with
    the request's EFFECTIVE detector (per-app `pii_detection` base + custom
    recognizers -- the exact detector the gate used) and its `ctx`.

    Invariant: `self._buffer` always holds exactly the not-yet-released RAW tail
    of the completion. `feed` appends the new delta, releases the redacted safe
    prefix, and keeps the remainder (including any entity still straddling the
    hold-back boundary) buffered. `flush` releases whatever is left, redacted.
    """

    def __init__(self, detector: Detector, ctx: ExecutionPolicyContext) -> None:
        self._detector = detector
        self._ctx = ctx
        self._buffer = ""

    def feed(self, delta: str) -> str:
        """Append `delta`; return the redacted, releasable safe prefix (possibly
        empty). Everything within `HOLDBACK_CHARS` of the end -- and any entity
        that reaches into that window -- stays buffered for a later feed/flush."""
        self._buffer += delta
        entities = self._detector.analyze(self._buffer)

        safe = len(self._buffer) - HOLDBACK_CHARS
        if safe <= 0:
            return ""

        # Never cut through an entity that extends past `safe`: pull the release
        # point back to the start of any such entity so its raw prefix is not
        # released mid-formation. Detector entities are de-overlapped (dedupe),
        # so no released entity can straddle the resulting `cut`.
        cut = safe
        for e in entities:
            if e.end > safe:
                cut = min(cut, e.start)

        # General unbounded-entity guard: never release across a run of
        # entity-forming characters that reaches the END of the buffer -- it may
        # still be growing into an entity (e.g. an email local part before its
        # `@domain` arrives) that `analyze` cannot see yet. Holding it is safe
        # and general: released text and redacted placeholders only ever differ
        # INSIDE an entity, so a trailing run is released only once it is either
        # completed+detected (→ redacted above) or broken by a non-entity char
        # (→ provably not an entity). `min` only ever releases LESS, so this
        # never regresses the fixed-window cases; and a SHORT trailing run
        # (the common case) is already inside the window, leaving `cut`
        # unchanged -- the extra holding kicks in only for a run LONGER than the
        # window, i.e. exactly the leak class.
        m = _TRAILING_ENTITY_RUN.search(self._buffer)
        if m is not None:
            cut = min(cut, m.start())

        if cut <= 0:
            return ""

        release_raw = self._buffer[:cut]
        # Entities fully inside the released region (offsets are identical to the
        # buffer's, since `release_raw` is a prefix). Redact via the SAME
        # anonymize call the non-stream path uses.
        released_entities = [e for e in entities if e.end <= cut]
        redacted, _map = self._detector.anonymize(release_raw, released_entities)

        self._buffer = self._buffer[cut:]
        return redacted

    def flush(self) -> str:
        """End of stream: redact and release the ENTIRE remaining buffer.
        Nothing is held back once the backend stream is drained."""
        if not self._buffer:
            return ""
        entities: list[Entity] = self._detector.analyze(self._buffer)
        redacted, _map = self._detector.anonymize(self._buffer, entities)
        self._buffer = ""
        return redacted
