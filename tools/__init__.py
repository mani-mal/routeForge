"""Offline operator tooling, OUTSIDE the serving graph.

Nothing under `app/` may import `tools.*` (see CLAUDE.md rule 4 — the job
mines -> recommends, offline + human-gated; it never becomes part of request
serving). `tools.*` importing `app.*` is fine and expected (one-way): these
jobs read the same config/DB the server uses, they just never run inside it.
"""
