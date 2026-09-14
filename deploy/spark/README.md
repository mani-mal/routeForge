# routeForge on a DGX Spark

Single-node install for an **NVIDIA DGX Spark** (GB10 Grace Blackwell, aarch64,
DGX OS + Docker). Everything is built from source on the Spark, so every image
is natively arm64.

**This is not the DGX lab path.** `deploy/dgx_lab/` targets the Run:ai
Kubernetes cluster — namespace `runai-routeforge`, a `ghcr-pull` secret,
NodePorts, an in-cluster vLLM. A Spark is a workstation with Docker and no
Kubernetes, so none of that applies. Use the files in this directory instead.

**routeForge deploys no inference.** It is a pure router: it points at
OpenAI-compatible endpoints you stand up yourself. The on-prem model pool boots
**empty**, and until you onboard an endpoint (step 5) every `onprem_only`
request is rejected — by design, not by failure.

| File | What it is |
|---|---|
| `docker-compose.yml` | the stack: `db` (Postgres) + `api` (:8080) + `ui` (:3001) |
| `docker-compose.langfuse.yml` | optional overlay wiring the api to a self-hosted Langfuse |
| `.env.example` | copy to `.env`; sets passwords, ports, detector |
| `install.sh` | preflight (arch, docker, arm64 images, ports, .env) then bring-up |

---

## 1. Prerequisites

- Docker Engine with the **compose v2 plugin** (`docker compose version` must
  work; the old `docker-compose` v1 will not). DGX OS ships Docker; if your user
  isn't in the `docker` group, `sudo usermod -aG docker $USER` and re-login.
- ~20 GB free disk for images and the build cache.
- Internet access **for the build** (PyPI, npm, Docker Hub). After that the
  router runs offline — no GPU, no API keys, no cloud provider needed.

## 2. Configure

```bash
cd /path/to/routeForge
cp deploy/spark/.env.example deploy/spark/.env
$EDITOR deploy/spark/.env        # ADMIN_PASSWORD and POSTGRES_PASSWORD are required
```

Set `SPARK_HOST` to the Spark's hostname or IP if you'll open the console from
another machine.

## 3. Install

```bash
bash deploy/spark/install.sh              # router only
bash deploy/spark/install.sh --langfuse   # + self-hosted Langfuse
bash deploy/spark/install.sh --check      # preflight only, starts nothing
```

Or drive compose directly:

```bash
docker compose -p routeforge-spark -f deploy/spark/docker-compose.yml up -d --build

# with Langfuse — three files, in this order (the overlay must come last):
docker compose -p routeforge-spark \
  -f deploy/spark/docker-compose.yml \
  -f deploy/compose/langfuse.yml \
  -f deploy/spark/docker-compose.langfuse.yml \
  up -d --build
```

The first build compiles the full dependency stack for arm64 — **allow 10-20
minutes**. Subsequent builds hit the cache.

## 4. Verify

```bash
curl -s http://localhost:8080/health
```

Then open the console at `http://<spark>:3001` and log in with `ADMIN_EMAIL` /
`ADMIN_PASSWORD`. On a cold boot `/health` may report `models: []` — that is
correct, the pool is empty until step 5.

### Admin logins

There is **no sign-up page** — the console is admin-only. Applications don't log
in; they get API tokens (step 6). The api seeds one admin from `ADMIN_EMAIL` /
`ADMIN_PASSWORD` on boot, and only **if that email isn't already in the
database**. So:

- Changing `ADMIN_PASSWORD` after the first boot does **nothing** — the original
  password stays valid.
- Changing `ADMIN_EMAIL` to a new address and restarting the api adds a
  **second** admin; the first one remains.
- Sessions live in api process memory: **every api restart logs everyone out.**

Add another admin (no UI or API for this):

```bash
docker compose -p routeforge-spark -f deploy/spark/docker-compose.yml exec api python -c '
import asyncio
from app.apps.db import Database
from app.apps.admin_auth import create_admin
async def main():
    db = Database()
    async with db.session() as s:
        await create_admin(s, email="you@example.com", password="a-strong-password")
        await s.commit()
    await db.dispose()
asyncio.run(main())'
```

Forgot the password — delete the row, restart, and it is re-seeded from `.env`:

```bash
docker compose -p routeforge-spark -f deploy/spark/docker-compose.yml exec db \
  psql -U routeforge -d routeforge -c "delete from admin_users where email='admin@routeforge.local';"
docker compose -p routeforge-spark -f deploy/spark/docker-compose.yml restart api
```

## 5. Bring your own model server, then onboard it

**Outside routeForge** — this stack starts no inference and never will (hard
rule 4). Run any OpenAI-compatible server on the Spark's GPU, e.g.:

```bash
# ollama, as a host process — simplest thing that works on GB10
ollama serve                      # exposes http://<spark>:11434/v1
ollama pull qwen2.5:7b
```

The only thing routeForge cares about: the model id the server reports in
`GET /v1/models` is the `slug` you onboard. See `docs/guides/onboarding-models.md` and
the `inference-backends` skill for vLLM / NIM / Dynamo on the Spark's GPU.

**Endpoint address.** If the server runs on the Spark **host** (not in this
compose network), the api container reaches it at
`http://host.docker.internal:11434/v1` — the base file adds that hostname
explicitly, since Docker on Linux doesn't provide it. If you run it as another
container on the same compose network, use its service name.

A host-run server bound to `127.0.0.1` (ollama's default) is **not** reachable
from the api container, and onboarding will save the row disabled with
`probe_status: "unreachable"`. Widen the bind and scope it with a firewall
rather than leaving it open on the network — see *Exposure notes* below.

**Onboard it** — console *Infrastructure → Onboard a model*, or the admin API.
The admin API is **cookie-authed**: the login and the write must share one
cookie jar.

```bash
JAR=$(mktemp)
curl -s -c "$JAR" -X POST http://localhost:8080/admin/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@routeforge.local","password":"<ADMIN_PASSWORD>"}'

curl -s -b "$JAR" -X POST http://localhost:8080/admin/infra/models \
  -H 'content-type: application/json' \
  -d '{
        "slug": "qwen2.5:7b",
        "display_name": "Qwen2.5 7B (Spark)",
        "tier": "cheap",
        "endpoint": "http://host.docker.internal:11434/v1",
        "price_input": 0.0,
        "price_output": 0.0
      }'
```

Onboarding is **probe-gated**: routeForge does `GET {endpoint}/models` and
requires your `slug` in the response. Unreachable, or slug absent → the row is
saved **disabled** with `probe_status: "unreachable"`. It takes effect with no
restart.

## 6. Register an application and make a call

```bash
curl -s -b "$JAR" -X POST http://localhost:8080/admin/applications \
  -H 'content-type: application/json' \
  -d '{"id":"demo","name":"Demo app","default_residency":"onprem_only"}'

# The plaintext token is returned ONCE.
curl -s -b "$JAR" -X POST http://localhost:8080/admin/applications/demo/tokens \
  -H 'content-type: application/json' -d '{"name":"laptop"}'

curl -s http://localhost:8080/v1/chat/completions \
  -H "authorization: Bearer <token>" -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in five words."}]}'
```

## 7. Operate

```bash
docker compose -p routeforge-spark logs -f api          # follow the router
docker compose -p routeforge-spark ps                   # status
docker compose -p routeforge-spark up -d --build api    # rebuild after a git pull
docker compose -p routeforge-spark down                 # stop, keep the database
docker compose -p routeforge-spark down -v              # stop and WIPE the database
```

`-p routeforge-spark` on every command — the project name is what ties them to this
stack, and it must match what you started with.

**Moving the checkout** (e.g. into `~/projects/`) is safe: the database is the
named volume `routeforge-spark_rf-db`, keyed by the project name, not the path,
and there are no bind mounts. Stop, move, start from the new path — `up -d`
reuses the already-built images, and `.env` moves with the directory:

```bash
cd ~/routeForge && docker compose -p routeforge-spark -f deploy/spark/docker-compose.yml down
mv ~/routeForge ~/projects/routeForge
cd ~/projects/routeForge && docker compose -p routeforge-spark -f deploy/spark/docker-compose.yml up -d
```

Onboarded models, applications and tokens survive; admin sessions don't, so log
in again.

**No schema migrations.** `create_all` creates missing tables but never `ALTER`s
an existing one. After pulling a version that adds a column, either apply the
`ALTER` by hand or `down -v` and re-onboard.

---

## Exposure notes — read before putting this on a network you share

This stack has an admin console and an admin API. Check what you are publishing
them on, especially on a workstation that sits on office or home wifi rather
than in a rack.

**`BIND_ADDR` controls the host address the api and ui ports publish on, and
naming an address explicitly is load-bearing.** With the host address omitted,
Docker binds `0.0.0.0` *and* `[::]` — so on any machine with a globally routable
IPv6 address (most consumer ISPs hand out a /64 these days), the console and the
admin API are published straight onto the public internet, filtered only by
whatever the upstream router happens to do. Naming an IPv4 address restricts the
publish to IPv4. The shipped default is an explicit `0.0.0.0`.

```bash
ss -ltn | grep -E ':(8080|3001)'      # a [::] line means IPv6-published
ip -o addr show scope global | grep inet6   # a 2000::/3 address is public
```

Set `BIND_ADDR=127.0.0.1` for loopback-only and reach the console over a tunnel:

```bash
ssh -L 3001:127.0.0.1:3001 -L 8080:127.0.0.1:8080 user@spark
```

**A host firewall will not save you here.** Docker's rules bypass ufw's INPUT
chain, so `ufw deny 8080` does nothing to a published container port — the bind
address is the control that works. ufw still matters for *host* services (sshd,
and a model server you run as a host process).

**If you run the model server on the host, scope it to Docker, not to the LAN.**
An OpenAI-compatible server usually has no authentication, and ollama's API can
pull and delete models and drive the GPU. It has to listen on more than loopback
for the api container to reach it, so pair that with a firewall rule:

```bash
# ollama: reachable from containers, not from the network
sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp
```

`172.16.0.0/12` — Docker's whole default pool — not just `172.17.0.0/16`.
`host.docker.internal` resolves to docker0's gateway (`172.17.0.1`), but a
Compose service runs on its own network and reaches it with a `172.18.x` **source**
address. A rule matching only `172.17.0.0/16` drops it, and onboarding then fails
with `probe_status: "unreachable"` for what looks like no reason.

**Testing from the host itself proves nothing.** `curl` from the machine to its
own LAN address goes over loopback, never traverses the firewall's INPUT chain,
and always succeeds. Test from a different machine.

---

## arm64 notes

- **Never pull `ghcr.io/mani-mal/serving-plane-{api,ui}`.** Those are built on an
  x86_64 laptop for the DGX lab and are amd64-only. This stack builds locally,
  which is why it doesn't reference them.
- Every third-party image this stack pulls (`postgres:16`, `redis:7`,
  `clickhouse/clickhouse-server:24`, `minio/minio`, `minio/mc`,
  `langfuse/langfuse:3`, `langfuse/langfuse-worker:3`) was confirmed to publish
  an arm64 variant on 2026-09-07. `install.sh` re-checks each with
  `docker manifest inspect` anyway, since tags move. A missing variant would
  surface as `no matching manifest for linux/arm64`; drop `--langfuse` and run
  the router alone if one of the observability images ever becomes the blocker.
- **The Python build needs no compiler on arm64.** Every package in `uv.lock`
  that ships binary wheels also ships a linux aarch64 wheel (checked across all
  197 locked packages) — so `uv sync --frozen` resolves to wheels rather than
  falling back to sdists, which the compiler-less `python:3.12-slim` builder
  could not build.
- **The `npm ci` failure documented in `ui/Dockerfile` does not apply here.** It
  is a Docker-Desktop/WSL2 process-teardown bug, so the Spark's native daemon
  builds the hermetic `ui/Dockerfile` normally — `ui/Dockerfile.prebuilt` is not
  needed.
- `SP_PII_DETECTION=presidio` requires `API_EXTRAS` to include `presidio`; the
  build then downloads the `en_core_web_lg` spaCy model (~600 MB, needs
  internet at build time). The default `regex` detector needs neither.

## Sizing

The router itself is CPU-only and small. The `--langfuse` overlay adds six
containers, ClickHouse being the hungry one — comfortable inside the Spark's
128 GB unified memory, but it is competing with whatever you're serving on the
GPU. If you only want routing, metering and the console, skip the overlay: the
exporter logs one warning at boot and stays disabled, and nothing in the request
path blocks on it.
