# Menhir production Neo4j image. The upstream database image is fixed by
# digest; OS packages with available security fixes are upgraded to exact
# versions. Publish and deploy only the scanned output digest.
FROM neo4j:5.26-community@sha256:037cf5756f0135cbfd66b739b6df7c7c4bb100f9ce11602f6f9538e17e02c74d

LABEL org.opencontainers.image.base.digest="sha256:037cf5756f0135cbfd66b739b6df7c7c4bb100f9ce11602f6f9538e17e02c74d" \
      org.opencontainers.image.title="Menhir Neo4j 5.26.30 patched release image" \
      org.opencontainers.image.vendor="Archolith"

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y \
        libssl3t64=3.5.7-1~deb13u2 \
        linux-libc-dev=6.12.107-1 \
        openssl=3.5.7-1~deb13u2 \
        openssl-provider-legacy=3.5.7-1~deb13u2 \
    && rm -rf /var/lib/apt/lists/*
USER neo4j
