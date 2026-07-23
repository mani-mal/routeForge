# Serving Plane UI image — node build stage → static nginx.
# nginx proxies /api and /v1 to the `api` service (see nginx.conf) so the
# browser talks to a single origin, same as the Vite dev proxy.
# Hermetic build for normal Docker daemons (GreenLake CI, Linux hosts).
#
# NOTE: On Docker Desktop's WSL2 backend, `npm ci` reproducibly aborts with
# "Exit handler never called!" for EVERY node base (20/22, slim/alpine) — and
# some bases exit 0 while leaving node_modules half-populated, so the failure
# only surfaces later as "tsc: not found". It's an npm-process-teardown bug in
# that specific sandbox, not something a Dockerfile flag fixes. For the DGX lab
# built from such a laptop, use Dockerfile.prebuilt (packages a host-built
# dist/) instead. This hermetic path stays the source of truth for the cloud.
#
# The test -x guards below convert the silent-partial-install case into a hard
# failure at THIS layer rather than a confusing error downstream.
ARG NODE_VERSION=22

# ---- build -----------------------------------------------------------------
FROM node:${NODE_VERSION}-slim AS build
WORKDIR /ui
# Cache deps on lockfile changes only.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund \
    && test -x node_modules/.bin/tsc \
    && test -x node_modules/.bin/vite
COPY . .
RUN npm run build   # tsc -b && vite build -> dist/

# ---- serve -----------------------------------------------------------------
FROM nginx:1.27-alpine AS serve
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/serving-plane.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
