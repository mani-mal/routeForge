#!/usr/bin/env bash
# Tag the routeForge api/ui images and push them into PCAI's Harbor registry so
# the Import Framework can pull them. Run from a machine that can reach Harbor
# (your WSL box is fine — this needs network + Docker, NOT cluster SSH).
#
# Prereqs:
#   * The images exist locally (or are pullable), e.g. built by ../../../Dockerfile
#     or already at ghcr.io/mani-mal/serving-plane-{api,ui}:<tag>.
#   * A Harbor PROJECT exists (create it in the Harbor UI first).
#
# Usage:
#   HARBOR=harbor.ingress.pcai0104.ld7.hpecolo.net \
#   PROJECT=routeforge \
#   TAG=0.2.0 \
#   SRC_REGISTRY=ghcr.io/mani-mal \
#   ./build-and-push.sh
#
set -euo pipefail

HARBOR="${HARBOR:?set HARBOR to your PCAI Harbor host, e.g. harbor.ingress.<domain>}"
PROJECT="${PROJECT:-routeforge}"
TAG="${TAG:-0.2.0}"
# Where to pull the source images from if not already local. Empty => assume the
# images are present locally as serving-plane-api:$TAG / serving-plane-ui:$TAG.
SRC_REGISTRY="${SRC_REGISTRY:-}"

echo "==> Logging in to Harbor: $HARBOR"
docker login "$HARBOR"

for name in serving-plane-api serving-plane-ui; do
  if [[ -n "$SRC_REGISTRY" ]]; then
    src="${SRC_REGISTRY%/}/${name}:${TAG}"
    echo "==> Pulling source image $src"
    docker pull "$src"
  else
    src="${name}:${TAG}"
  fi
  dst="${HARBOR}/${PROJECT}/${name}:${TAG}"
  echo "==> Tagging $src -> $dst"
  docker tag "$src" "$dst"
  echo "==> Pushing $dst"
  docker push "$dst"
done

echo
echo "Done. In values.yaml set:"
echo "  image.registry: ${HARBOR}/${PROJECT}"
echo "  image.api.tag / image.ui.tag: ${TAG}"
echo "If the Harbor project is PRIVATE, also set imageCredentials.enabled=true"
echo "with registry=${HARBOR} and your Harbor username/password."
