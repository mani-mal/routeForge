# Paper revision figures (2026-11-05)

Two scripts recompute figures added to `docs/research/paper/main.tex` in response to a
pre-arXiv review. Both read only files already in this repository. They need no GPU,
provider account or network access.

| Script | Recomputes |
|---|---|
| `replay_and_detector.py` | The replay-fidelity table (admission, first-attempt and final-endpoint agreement, runs 1–4). The 987-request breakdown in "Placement holds" (status, outbound attempts, cloud attempts). The detector table (against the generator's planted identifiers, runs 3–4). The empty-response rates in "Two measurements about tokens". |
| `policy_overhead.py` | The in-process timings in "Policy-side overhead" (compile, eligibility, conflicting-core search). |

Run both from the repository root:

```bash
export LITELLM_LOCAL_MODEL_COST_MAP=True
export SP_CONFIG_OVERLAY_DIR=$PWD/deploy/dgx_lab/config-overlay   # the lab's cloud provider + pool
uv run python docs/research/data/paper-revision/replay_and_detector.py
uv run python docs/research/data/paper-revision/policy_overhead.py
```

`*.out` holds the output these scripts produced for the paper. The replay figures are
deterministic. The timings vary between runs by tens of percent, and the machine was a
developer laptop under WSL2.

Ground truth for the detector is the generator's `pii` flag in each run's `client_log.ndjson`
(a planted SSN plus e-mail address). Rows join to `outcomes.ndjson` by request id. A row
counts as detected when any entity type was recorded.
