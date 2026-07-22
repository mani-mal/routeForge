# Serving Plane UI image — node build stage → static nginx.
# nginx proxies /api and /v1 to the `api` service (see nginx.conf) so the
# browser talks to a single origin, same as the Vite dev proxy.
ARG NODE_VERSION=20

# ---- build -----------------------------------------------------------------
FROM node:${NODE_VERSION}-slim AS build
WORKDIR /ui
# Cache deps on lockfile changes only.
COPY package.json package-lock.json ./
RUN npm ci
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
