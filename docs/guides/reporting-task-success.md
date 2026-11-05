# Reporting task success

The Metrics tab can show your team lead **cost per successful task** and **successful
(reported)** only if your application tells RouteForge which tasks succeeded. RouteForge
cannot judge that itself: it sees requests, not outcomes. The completed-without-failure
figure next to it is not a quality measure, it only means the request did not error.

## The call

After a task ends, send one label for it with `POST /v1/feedback`, using the same API token
you use for `/v1/chat/completions`:

| field | value |
|---|---|
| `score_name` | `"task_success"` |
| `value` | `1` (the task succeeded) or `0` (it failed). Any other value is ignored. |
| `request_id` | the `request_id` of **any** request that belonged to the task |
| `comment` | optional free text |

`request_id` is the `id` field of the chat completion response (`req-` followed by 32 hex
characters).
`trace_id` is also accepted, but `request_id` is what the metrics use. The response is
`{"stored": ..., "recorded": ...}`; `recorded: true` means the label was kept for metrics.
`recorded: false` means the request id was not found for your application (or outcome
recording is disabled).

**The latest label wins.** Send a new label for the same task and it replaces the earlier
one, so you can correct a mistake.

## How requests become a task

A task is a group of requests, and the label applies to the whole group:

- Send the same `x-routeforge-trajectory` header on every call of one task. 1-128
  characters of `A-Z a-z 0-9 . _ : -`; it is stored only as a hash scoped to your
  application. See [`trajectory_mode`](../reference/per-app-config.md#trajectory_mode--carrying-a-restriction-along-a-conversation),
  which must be set to `header` (or `sticky`) on the application for the header to be used.
- Label any one request of the task; the label covers all of them.
- Without a trajectory header every request is its own task, and the Metrics tab says
  "per request" instead of "per task".

## Minimum data

Numbers appear once there are **at least 20 labelled tasks** in the window. Below that the
card says `insufficient data (n=...)`, because a rate over a handful of tasks is noise.

## curl

```bash
curl -s https://routeforge.example.com/v1/feedback \
  -H "Authorization: Bearer $ROUTEFORGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"request_id": "req-0123456789abcdef0123456789abcdef", "score_name": "task_success", "value": 1}'
```

## Python

```python
import os
import uuid

import httpx

BASE = "https://routeforge.example.com"
HEADERS = {"Authorization": f"Bearer {os.environ['ROUTEFORGE_TOKEN']}"}

task_id = f"task:{uuid.uuid4()}"  # one id for every call of this task

resp = httpx.post(
    f"{BASE}/v1/chat/completions",
    headers={**HEADERS, "x-routeforge-trajectory": task_id},
    json={"model": "auto", "messages": [{"role": "user", "content": "..."}]},
)
resp.raise_for_status()
request_id = resp.json()["id"]  # keep any one request id of the task

# ... the task runs, you find out whether it worked ...

httpx.post(
    f"{BASE}/v1/feedback",
    headers=HEADERS,
    json={"request_id": request_id, "score_name": "task_success", "value": 1},
).raise_for_status()
```

See also the [Metrics API](../reference/metrics-api.md) for how the figures are computed and
the [UI guide](ui-guide.md) for the Metrics tab.
