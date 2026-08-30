# Menhir's release base is built separately, scanned, published, and then
# supplied to deploy/Dockerfile through its digest-pinned PYTHON_BASE argument.
# The upstream digest remains fixed while Debian security updates are applied;
# the published output digest and scan evidence are release-authority inputs.
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

LABEL org.opencontainers.image.base.digest="sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217" \
      org.opencontainers.image.title="Menhir Python 3.12 patched release base" \
      org.opencontainers.image.vendor="Archolith"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y \
        libssl3t64=3.5.7-1~deb13u2 \
        openssl=3.5.7-1~deb13u2 \
        openssl-provider-legacy=3.5.7-1~deb13u2 \
    && rm -rf /var/lib/apt/lists/*
