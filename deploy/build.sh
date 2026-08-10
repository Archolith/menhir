#!/usr/bin/env bash
#
# Build the Menhir runtime image.
#
# This builds from the menhir repo alone — no sibling checkout, no workspace layout.
# The first-party dependencies are resolved from public GitHub inside the Dockerfile's
# builder stage, which is the only stage that has git. (A new user just clones menhir and
# runs this, or `docker compose -f deploy/docker-compose.full.yml up --build`.)
#
# Usage:
#   deploy/build.sh                       # -> menhir:test
#   IMAGE=menhir:0.2.0 deploy/build.sh    # custom tag
set -euo pipefail

MENHIR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-menhir:test}"

echo "Building $IMAGE from $MENHIR_DIR ..."
docker build -f "$MENHIR_DIR/deploy/Dockerfile" -t "$IMAGE" "$MENHIR_DIR"
echo "Built $IMAGE"
