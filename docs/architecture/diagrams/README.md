# Architecture diagrams

Editable draw.io / diagrams.net files. Open any `.drawio` directly in
[app.diagrams.net](https://app.diagrams.net) or the VS Code Draw.io extension.

| File | What it shows |
|------|---------------|
| `routeforge-logical-architecture.drawio` | The ten Serving-Plane steps as layers, each box labelled with the **open-source library / component** that implements it (FastAPI, Presidio, Headroom, LiteLLM, OpenTelemetry → Langfuse, …). |
| `routeforge-physical-architecture.drawio` | The **Kubernetes pods, services and PVCs** actually deployed — RouteForge `api`/`ui`, the full Langfuse stack (web, worker, postgres, clickhouse, redis, minio) and the shared datastores. On-prem/cloud inference is drawn dashed: it is operator-run, **not** a RouteForge pod. |
| `routeforge-request-flow.drawio` | End-to-end **request/response flow** with numbered arrows (1–14) through the gate, task mode, optimization, router/gateway, backends, verification, and the Langfuse telemetry tap. |

## Regenerating

These are generated from declarative JSON specs in `specs/` via the
`drawio-diagrams` skill (no hand-placed coordinates — the layout engine keeps
boxes non-overlapping and routes edges through the gaps):

```bash
SK=~/.claude/skills/drawio-diagrams
python3 "$SK/scripts/drawio_gen.py" specs/logical-architecture.json  routeforge-logical-architecture.drawio  --page-width 2000
python3 "$SK/scripts/drawio_gen.py" specs/physical-architecture.json routeforge-physical-architecture.drawio --page-width 2000
python3 "$SK/scripts/drawio_gen.py" specs/request-flow.json          routeforge-request-flow.drawio          --page-width 1800

# Optional PNG/PDF overlap-check preview (needs matplotlib):
python3 "$SK/scripts/render_drawio.py" routeforge-request-flow.drawio preview.png
```

Edit the `specs/*.json` and regenerate rather than nudging boxes in draw.io.
For a crisp export into a doc or slide, open in draw.io → File → Export As → PDF.
