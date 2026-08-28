# syntax=docker/dockerfile:1
#
# Menhir production runtime image - assembled from pre-built artifacts only.
#
# Controlled-CI contract:
#   * Base image is a digest-pinned ARG. A mutable tag (no @sha256:) is refused
#     at build time, so an unpinned base can never reach a release registry.
#   * Python dependencies come from a pre-built offline wheelhouse. This
#     Dockerfile performs no live git / PyPI / apt resolution; the wheelhouse is
#     produced once, separately, in a network-enabled CI job:
#         pip wheel . --wheel-dir deploy/wheelhouse
#     (that is the only step where the git+https first-party deps are resolved).
#   * Fixed non-root UID/GID (MENHIR_UID / MENHIR_GID, default 10001/10001).
#   * Healthcheck probes /livez, which the production route surface serves when
#     MENHIR_STARTUP_SCOPE=production.
#
# Build (from the repo root, after the wheelhouse exists):
#   docker build -f deploy/Dockerfile \
#       --build-arg PYTHON_BASE=python:3.12-slim@sha256:<digest> \
#       -t <registry>/menhir:<version> .
#
# The compose contract (deploy/docker-compose.production.yml) supplies the
# image reference as the required MENHIR_IMAGE variable; there is no source
# build on the VPS.

# Declared before FROM so CI can override it; re-checked inside the stage so
# the build fails closed with a clear message when it is not digest-pinned.
ARG PYTHON_BASE
FROM ${PYTHON_BASE} AS runtime

ARG PYTHON_BASE
ARG MENHIR_UID=10001
ARG MENHIR_GID=10001
ARG WHEELHOUSE_DIR=deploy/wheelhouse
ARG RELEASE_COMMIT=unknown
ARG RELEASE_VERSION=unknown
ARG WHEEL_MANIFEST_SHA256
ARG OAUTH_WHEEL_SHA256

LABEL org.opencontainers.image.revision="${RELEASE_COMMIT}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.archolith.menhir.wheel-manifest.sha256="${WHEEL_MANIFEST_SHA256}" \
      org.archolith.oauth.wheel.sha256="${OAUTH_WHEEL_SHA256}"

RUN case "$PYTHON_BASE" in \
        *@sha256:*) ;; \
        *) echo "PYTHON_BASE must be digest-pinned (python:3.12-slim@sha256:<digest>)" >&2; exit 1 ;; \
    esac

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    WORKSPACE_ROOT=/tmp/menhir \
    MENHIR_API_HOST=0.0.0.0 \
    MENHIR_API_PORT=8099 \
    MENHIR_STARTUP_SCOPE=production \
    MENHIR_RUNTIME_MODE=production \
    MENHIR_ALLOW_SYSTEM_PYTHON=1 \
    MENHIR_SECRETS_DIR=/run/secrets/menhir \
    MENHIR_OAUTH_AS_DIR=/srv/menhir/production/state/oauth \
    MENHIR_OAUTH_SIGNING_KEY_PATH=/run/secrets/oauth/oauth_signing_key.json \
    MENHIR_OAUTH_REFRESH_RETRY_KEYRING_PATH=/run/secrets/oauth/retry-response-keyring.json \
    MENHIR_MCP_TELEMETRY_DB=/srv/menhir/state/telemetry/mcp_telemetry.db

# Offline install from the pre-built wheelhouse only. --no-index --no-deps is
# required, not an optimization: menhir's wheel metadata records the two
# first-party deps as git+https URLs, and pip would otherwise try to re-resolve
# them (demanding git + network). The closure was already resolved when the
# wheelhouse was built. Fail closed if the wheelhouse is empty.
COPY deploy/lib/verify_wheelhouse.py /usr/local/libexec/verify_wheelhouse.py
COPY ${WHEELHOUSE_DIR}/ /wheelhouse/
RUN python /usr/local/libexec/verify_wheelhouse.py \
        /wheelhouse /wheelhouse/SHA256SUMS \
        "${WHEEL_MANIFEST_SHA256}" "${OAUTH_WHEEL_SHA256}" \
    && pip install --no-index --no-deps /wheelhouse/*.whl \
    && rm -rf /wheelhouse /usr/local/libexec/verify_wheelhouse.py

# Fixed non-root runtime identity, created before the state tree so ownership
# lines up for bind/named-volume seeding.
RUN groupadd --gid "${MENHIR_GID}" menhir \
    && useradd --uid "${MENHIR_UID}" --gid "${MENHIR_GID}" --no-create-home --shell /usr/sbin/nologin menhir \
    && mkdir -p \
        /srv/menhir/production/secrets \
        /srv/menhir/production/policy \
        /srv/menhir/production/state/oauth \
        /srv/menhir/state/telemetry \
    && chown -R menhir:menhir /srv/menhir/production/state /srv/menhir/state \
    && chmod 0700 /srv/menhir/production/secrets

# Entrypoint: map bind-mounted secret files to the environment variables the
# process reads, without ever printing their values, then exec the app.
COPY <<'EOF' /usr/local/bin/menhir-entrypoint.sh
#!/bin/sh
set -eu
secrets_dir="${MENHIR_SECRETS_DIR:-/srv/menhir/production/secrets}"
oauth_secrets_dir="${MENHIR_OAUTH_SECRETS_DIR:-/run/secrets/oauth}"
load_secret() {
    file="$secrets_dir/$1"
    var="$2"
    if [ -r "$file" ]; then
        value="$(tr -d '\r\n' < "$file")"
        if [ -n "$value" ]; then
            export "$var=$value"
        fi
    fi
}
load_required_secret() {
    file="$secrets_dir/$1"
    var="$2"
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
        echo "required secret file is missing or unreadable: $file" >&2
        exit 78
    fi
    value="$(tr -d '\r\n' < "$file")"
    if [ -z "$value" ]; then
        echo "required secret file is empty: $file" >&2
        exit 78
    fi
    export "$var=$value"
}
load_required_oauth_secret() {
    file="$oauth_secrets_dir/$1"
    var="$2"
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
        echo "required OAuth secret file is missing or unreadable: $file" >&2
        exit 78
    fi
    value="$(tr -d '\r\n' < "$file")"
    if [ -z "$value" ]; then
        echo "required OAuth secret file is empty: $file" >&2
        exit 78
    fi
    export "$var=$value"
}
require_readable_file() {
    file="$1"
    if [ ! -f "$file" ] || [ ! -r "$file" ] || [ ! -s "$file" ]; then
        echo "required production file is missing, empty, or unreadable: $file" >&2
        exit 78
    fi
}
load_required_secret neo4j-password NEO4J_PASSWORD
load_secret openai-api-key       OPENAI_API_KEY
load_secret gemini-api-key       GEMINI_API_KEY
load_secret local-llm-api-key    LOCAL_LLM_API_KEY
load_required_secret operator-key MENHIR_OPERATOR_KEY
load_required_secret source-fence-token MENHIR_SOURCE_FENCE_TOKEN
load_required_oauth_secret oauth-consent-secret MENHIR_OAUTH_AS_CONSENT_SECRET
load_secret api-key              MENHIR_API_KEY
load_secret agent-key            MENHIR_AGENT_KEY
load_secret readonly-key         MENHIR_READONLY_KEY
require_readable_file "${MENHIR_OAUTH_SIGNING_KEY_PATH:-/run/secrets/oauth/oauth_signing_key.json}"
if [ "${MENHIR_OAUTH_AS_REFRESH_RETRY_GRACE_S:-0}" != "0" ]; then
    require_readable_file "${MENHIR_OAUTH_REFRESH_RETRY_KEYRING_PATH:-/run/secrets/oauth/retry-response-keyring.json}"
fi
require_readable_file "${MENHIR_CLIENT_POLICY_PATH:-/srv/menhir/production/policy/client-policy.json}"
case "${LLM_CHAT_PROVIDER:-local},${GRAPHITI_LLM_PROVIDER:-local},${GRAPHITI_EMBED_PROVIDER:-}" in
    *openai*) load_required_secret openai-api-key OPENAI_API_KEY ;;
esac
case "${LLM_CHAT_PROVIDER:-local},${GRAPHITI_LLM_PROVIDER:-local},${GRAPHITI_EMBED_PROVIDER:-}" in
    *gemini*) load_required_secret gemini-api-key GEMINI_API_KEY ;;
esac
unset secrets_dir oauth_secrets_dir
exec "$@"
EOF
RUN chmod 0555 /usr/local/bin/menhir-entrypoint.sh

USER menhir
WORKDIR /tmp

EXPOSE 8099

# /livez is served by the production route surface (MENHIR_STARTUP_SCOPE=production).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['MENHIR_API_PORT']+'/livez', timeout=4)"]

ENTRYPOINT ["/usr/local/bin/menhir-entrypoint.sh"]
CMD ["menhir", "serve", "--host", "0.0.0.0", "--port", "8099"]
