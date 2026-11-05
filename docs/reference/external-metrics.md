# External (published) metrics

The Models page can show metrics **published by Artificial Analysis (AA)** next to the
ones RouteForge measures. It is display-only: nothing in routing, policy or the gateway
reads it, and AA is never called while serving a request.

Attribution: wherever AA data is shown the page reads "Published data: Artificial
Analysis" and links to <https://artificialanalysis.ai/>.

## What is fetched, and when

- Endpoint: `GET https://artificialanalysis.ai/api/v2/data/llms/models`.
- The request carries only the `x-api-key` header: no tenant, usage or request data.
- Only an admin pressing Refresh (`POST /admin/external-metrics/refresh`) or
  `scripts/aa_fetch.py` calls AA. Whole-call deadline 20 s (not per phase) and a 10 MB
  response cap, both answering 502; at most one refresh per 10 minutes, measured from the
  newest snapshot's `retrieved_at` (any origin).
- Refresh holds no database session or lock while AA is called, so a slow AA never delays
  serving requests. It is three steps: a short rate-limit check, the fetch, then a short
  re-check + insert (on Postgres under an advisory lock). If another refresh stored a
  snapshot meanwhile, this one is discarded with 429. A 429 carries a `Retry-After` header.
- Each import/refresh stores a dated snapshot. Every metric is nullable: null means
  "not published", never 0. A malformed payload is refused whole (422), never partly stored.

## Environment

| Variable | Meaning |
|---|---|
| `AA_API_KEY` | AA API key. Unset: Refresh answers 409. |
| `SP_EXTERNAL_FETCH` | `off` disables Refresh (409) even with a key. |

## Air-gapped flow

1. On a connected machine: `AA_API_KEY=... python scripts/aa_fetch.py aa-models.json`. The
   file is `{"routeforge_fetched_at": "<UTC ISO time>", "data": <raw AA payload>}`.
2. Carry the file across, then import it: `POST /admin/external-metrics/import`
   (body = the file, max 10 MB) or the console's Import file control.

### Import dating

An imported snapshot is dated by, in order: the file's top-level `routeforge_fetched_at`;
the `?retrieved_at=` query (ISO-8601, naive = UTC); else the upload time, stored with
`dated_at_upload: true` and shown in the console as "date of upload, not of fetch". A
future date is refused (422). The stale alert (30 days) and the refresh clock use this date,
so an old file does not look fresh when its date is known.

## Endpoints (admin session required)

- `POST /admin/external-metrics/import?index_version=&retrieved_at=` -> `{"snapshot_id","row_count","dated_at_upload"}`; 413 over 10 MB; 422 if not AA-shaped, a slug is over 128 characters, or a date is invalid or in the future.
- `POST /admin/external-metrics/refresh?index_version=` -> same; 409 key unset or fetch disabled; 429 rate limited (`retry_after_s`); 502 AA error or timeout (nothing stored, rate-limit clock not consumed).
- `GET /admin/external-metrics/snapshots` -> snapshots newest first (each with `dated_at_upload`), plus `fetch_enabled` and `fetch_disabled_reason`.
- `GET /admin/external-metrics/map` -> pool models with `placement`, current `ext_model_slug` and up to 3 `suggestions` from the latest snapshot.
- `PUT /admin/external-metrics/map` body `{"model","ext_model_slug"|null}` -> 404 unknown model, 422 slug not in the latest snapshot; null clears the mapping.

A model AA lists as free (price 0) against a nonzero `pricing.yaml` price is flagged as
`price_drift`; a missing AA price is not.

AA measures hosted APIs with its own chosen provider; it says nothing about your own
on-prem deployment. The Models page labels comparability accordingly.
