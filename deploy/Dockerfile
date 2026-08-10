# syntax=docker/dockerfile:1
#
# Menhir runtime image — self-contained.
#
# Build context is the menhir repo root, e.g.:
#     docker build -f deploy/Dockerfile -t menhir:test .
# or via docker compose (see deploy/docker-compose.test.yml).
#
# menhir depends on archolith-mcp-framework and archolith-oauth, neither of which is
# published to PyPI; pyproject pulls both from public GitHub over git+https. pip needs
# a real `git` binary to resolve those, and python:3.12-slim has none, so the build
# runs in two stages: a builder that has git and turns the whole dependency closure
# into wheels, and a runtime stage that installs from those wheels offline. git never
# reaches the runtime image.
#
# This supersedes the older deploy/vendor/ wheel approach, which pinned the
# pre-rename cth.mcp.framework package and is no longer used.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE THIRD-PARTY-LICENSES.txt ./
COPY src/ ./src/

# Build menhir and every runtime dependency into wheels. The git+https deps are
# resolved here, where git exists.
RUN pip wheel . --wheel-dir /wheels


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MENHIR_API_HOST=0.0.0.0 \
    MENHIR_API_PORT=8099 \
    MENHIR_STARTUP_SCOPE=auth-only \
    MENHIR_OAUTH_AS_DIR=/data \
    MENHIR_MCP_TELEMETRY_DB=/data/mcp_telemetry.db \
    WORKSPACE_ROOT=/data

# curl is only needed for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Install from the builder's wheels only. --no-index guarantees the runtime stage
# reaches neither PyPI nor GitHub, so the image is reproducible from the wheels the
# builder resolved.
# --no-deps is required, not an optimization: menhir's wheel metadata records the two
# first-party dependencies as direct git+https URLs, and pip always re-resolves a direct
# URL requirement even when a matching wheel is already present locally. Resolving would
# therefore demand git again. The builder already resolved the complete closure, so every
# wheel is installed directly with resolution turned off.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --no-deps /wheels/*.whl \
    && rm -rf /wheels

# Drop root. Pre-create the state dir (MENHIR_OAUTH_AS_DIR=/data) owned by the
# runtime user so the client-token / OAuth-AS SQLite stores are writable. When a
# named volume is mounted at /data, Docker seeds it from this directory, so the
# volume inherits the correct ownership (a root-owned volume would 403 the
# non-root user otherwise).
RUN useradd --create-home --uid 10001 menhir \
    && mkdir -p /data \
    && chown menhir:menhir /data
USER menhir
WORKDIR /home/menhir
VOLUME ["/data"]

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MENHIR_API_PORT}/api/health" || exit 1

# Default scope is auth-only (no Neo4j). The compose file selects the auth mode
# (client-token / OAuth) so a 0.0.0.0 bind is authenticated and the no-auth bind
# guard is satisfied. Override MENHIR_STARTUP_SCOPE=full for the backend-backed
# deployment (requires Neo4j).
CMD ["sh", "-c", "exec menhir serve --host \"$MENHIR_API_HOST\" --port \"$MENHIR_API_PORT\""]
