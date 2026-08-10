"""Shared target guards for operator scripts that can mutate Neo4j."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit

_NEO4J_SCHEMES = frozenset({"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"})


def target_host(uri: str) -> str:
    """Return a normalized host from a Neo4j/Bolt URI or raise ``ValueError``."""

    parsed = urlsplit(uri)
    host = parsed.hostname
    if parsed.scheme.casefold() not in _NEO4J_SCHEMES or not host:
        raise ValueError(f"invalid Neo4j URI: {uri!r}")
    return host.casefold()


def is_loopback_target(uri: str) -> bool:
    """Return whether *uri* targets localhost or a loopback IP address."""

    host = target_host(uri)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def write_refusal_reason(
    uri: str,
    *,
    production_markers: Iterable[str],
    allow_remote: bool,
    allow_production: bool,
) -> str | None:
    """Return a fail-closed reason for a mutating script target, or ``None``.

    Every non-loopback write requires an explicit remote acknowledgement. Targets
    matching an operator-supplied production marker require the stronger production
    acknowledgement, which also implies remote acknowledgement.
    """

    host = target_host(uri)
    normalized_uri = uri.casefold()
    marker = next(
        (
            candidate.strip()
            for candidate in production_markers
            if candidate.strip() and candidate.strip().casefold() in normalized_uri
        ),
        None,
    )
    if marker and not allow_production:
        return (
            f"target {host!r} matches production marker {marker!r}; "
            "re-run with --allow-production only after a verified backup"
        )
    if not is_loopback_target(uri) and not (allow_remote or allow_production):
        return (
            f"target {host!r} is non-loopback; re-run with --allow-remote, "
            "or --allow-production when this is production"
        )
    return None
