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
  `scripts/aa_fetch.py` calls AA. Timeout 20 s; at most one refresh per 10 minutes,
  measured from the newest snapshot (any origin).
- Each import/refresh stores a dated snapshot. Every metric is nullable: null means
  "not published", never 0. A malformed payload is refused whole (422), never partly stored.

## Environment

| Variable | Meaning |
|---|---|
| `AA_API_KEY` | AA API key. Unset: Refresh answers 409. |
| `SP_EXTERNAL_FETCH` | `off` disables Refresh (409) even with a key. |

## Air-gapped flow

1. On a connected machine: `AA_API_KEY=... python scripts/aa_fetch.py aa-models.json`.
2. Carry the file across, then import it: `POST /admin/external-metrics/import`
   (body = the file, max 10 MB) or the console's Import file control.

## Endpoints (admin session required)

- `POST /admin/external-metrics/import?index_version=` -> `{"snapshot_id","row_count"}`; 413 over 10 MB; 422 if not AA-shaped.
- `POST /admin/external-metrics/refresh?index_version=` -> same; 409 key unset or fetch disabled; 429 rate limited (`retry_after_s`); 502 AA error or timeout (nothing stored, rate-limit clock not consumed).
- `GET /admin/external-metrics/snapshots` -> snapshots newest first, plus `fetch_enabled` and `fetch_disabled_reason`.
- `GET /admin/external-metrics/map` -> pool models with `placement`, current `ext_model_slug` and up to 3 `suggestions` from the latest snapshot.
- `PUT /admin/external-metrics/map` body `{"model","ext_model_slug"|null}` -> 404 unknown model, 422 slug not in the latest snapshot; null clears the mapping.

AA measures hosted APIs with its own chosen provider; it says nothing about your own
on-prem deployment. The Models page labels comparability accordingly.
