#!/usr/bin/env python3
"""Minimal routeForge client — a real application calling the router.

routeForge speaks the OpenAI Chat Completions API, so any OpenAI-compatible
client works: point it at the routeForge base URL and use the API token your
routeForge admin issued for your application. There is no routeForge SDK.

    pip install openai
    export ROUTEFORGE_BASE_URL="http://<host>:<port>/v1"
    export ROUTEFORGE_TOKEN="rf_live_..."          # from the admin dashboard
    python chat.py "What is the capital of France?"

The response is a normal OpenAI ChatCompletion. routeForge additionally decides
which model/tier serves the request (per your application's policy), keeps
sensitive requests on-prem, and records usage — none of which the client has to
think about.
"""
import os
import sys

from openai import OpenAI

BASE_URL = os.environ.get("ROUTEFORGE_BASE_URL", "http://localhost:8080/v1")
TOKEN = os.environ.get("ROUTEFORGE_TOKEN")

if not TOKEN:
    sys.exit("Set ROUTEFORGE_TOKEN to your application's API token "
             "(create one in the routeForge admin dashboard).")

client = OpenAI(base_url=BASE_URL, api_key=TOKEN)

prompt = " ".join(sys.argv[1:]) or "Say hello in one short sentence."

resp = client.chat.completions.create(
    # "auto" lets routeForge's router pick the model/tier; you may also request a
    # specific model your application is allowed to use.
    model="auto",
    messages=[{"role": "user", "content": prompt}],
)

print(resp.choices[0].message.content)
