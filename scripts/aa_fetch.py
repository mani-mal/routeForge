#!/usr/bin/env python
"""Fetch the Artificial Analysis model list to a local JSON file.

Standalone (does not import `app`): run it on a machine with internet access,
carry the file to an air-gapped RouteForge, and use the console's Import.

    AA_API_KEY=... python scripts/aa_fetch.py aa-models.json

Sends only the `x-api-key` header. Writes {"routeforge_fetched_at": <UTC ISO time>, "data": <raw AA payload>}.
Prints the number of models written.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

URL = "https://artificialanalysis.ai/api/v2/data/llms/models"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: AA_API_KEY=... aa_fetch.py <output.json>", file=sys.stderr)
        return 2
    key = os.environ.get("AA_API_KEY", "").strip()
    if not key:
        print("AA_API_KEY is not set", file=sys.stderr)
        return 2
    req = urllib.request.Request(URL, headers={"x-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https URL
            raw = r.read()
    except urllib.error.HTTPError as e:
        print(f"Artificial Analysis returned HTTP {e.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Artificial Analysis unreachable: {e}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except ValueError:
        print("response was not JSON", file=sys.stderr)
        return 1
    entries = payload.get("data") if isinstance(payload, dict) else payload
    # Wrapped so an import records when the data was fetched, not when it was uploaded.
    wrapped = {"routeforge_fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "data": payload}
    with open(argv[1], "w", encoding="utf-8") as f:
        json.dump(wrapped, f)
    print(len(entries) if isinstance(entries, list) else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
