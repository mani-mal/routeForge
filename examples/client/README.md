# routeForge client example

A real application calling routeForge — the OpenAI SDK pointed at the router with an
admin-issued API token. No routeForge SDK needed.

```bash
pip install openai
export ROUTEFORGE_BASE_URL="http://<host>:<port>/v1"
export ROUTEFORGE_TOKEN="rf_live_..."     # create one in Admin → Applications
python chat.py "What is the capital of France?"
```

See [`docs/help/integrating-an-app.md`](../../docs/help/integrating-an-app.md) for the
full integration path (tokens, policy, quotas, errors, observability).
