"""External-prerequisite and source-fence receipt validation.

Extracted from ``menhir_schema``: the independently signed public-network
prerequisite observations and the Ed25519-signed source-writer fence evidence,
including their canonical signature payloads. Import through ``menhir_schema``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from menhir_schema_release import validate_release
from menhir_schema_support import (
    SCHEMA_VERSION,
    _decode_b64url,
    _decode_ed25519_public_key,
    _parse_utc,
    _require_exact_keys,
    _require_fresh,
    _require_key_id,
    _require_str,
    _sha256_file,
    _validate_release_binding,
    load_strict,
)

_PREREQUISITE_KEYS = frozenset({
    "schema", "kind", "release_id", "release_manifest_sha256", "checked_utc",
    "route_version", "observations",
})
_PREREQUISITE_CHECKS = frozenset({
    "firewall", "proxied_dns", "full_strict", "hostname_aop",
    "external_scan", "console_recovery", "caddy_volume_permissions",
})
_PREREQUISITE_OBSERVATION_KEYS = frozenset({
    "worker_id", "network_id", "observed_utc", "route_version", "checks", "signature",
})

_SOURCE_FENCE_KEYS = frozenset({
    "schema", "kind", "release_id", "release_manifest_sha256", "checked_utc",
    "expires_utc", "source_id", "source_writer_stopped",
    "source_mutation_probe_denied", "source_service_disabled",
    "source_firewall_persistent", "signing_key_id", "signature",
})

# Canonical source-fence claims that are covered by the HMAC-SHA256 signature.
# The signature binds release identity + the fenced source identity + the
# explicit window so a tampered or replayed fence cannot be substituted.
_SOURCE_FENCE_SIGNED = frozenset({
    "release_id", "release_manifest_sha256", "checked_utc", "expires_utc",
    "source_id", "source_writer_stopped", "source_mutation_probe_denied",
    "source_service_disabled", "source_firewall_persistent", "signing_key_id",
})


def validate_prerequisite(path: str) -> dict:
    """Validate independently signed public-network prerequisite observations."""
    receipt = load_strict(path)
    _require_exact_keys(receipt, _PREREQUISITE_KEYS, "prerequisite receipt")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("prerequisite receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != "external-prerequisite":
        raise ValueError("prerequisite receipt kind must be external-prerequisite")
    _validate_release_binding(receipt, "prerequisite receipt")
    _require_fresh(receipt.get("checked_utc"), "prerequisite receipt.checked_utc", 900)
    route_version = _require_str(receipt.get("route_version"), "route_version")
    observations = receipt.get("observations")
    if not isinstance(observations, list) or len(observations) < 2:
        raise ValueError("prerequisite receipt requires at least two observations")
    workers = set()
    networks = set()
    for observation in observations:
        _require_exact_keys(
            observation, _PREREQUISITE_OBSERVATION_KEYS,
            "prerequisite observation",
        )
        worker = _require_key_id(observation.get("worker_id"), "worker_id")
        network = _require_key_id(observation.get("network_id"), "network_id")
        if worker in workers or network in networks:
            raise ValueError("prerequisite observations must use distinct workers and networks")
        workers.add(worker)
        networks.add(network)
        _require_fresh(observation.get("observed_utc"), "observed_utc", 900)
        if observation.get("route_version") != route_version:
            raise ValueError("prerequisite observation route_version mismatch")
        checks = observation.get("checks")
        _require_exact_keys(checks, _PREREQUISITE_CHECKS, "prerequisite checks")
        if any(checks.get(key) is not True for key in _PREREQUISITE_CHECKS):
            raise ValueError("every prerequisite observation check must be true")
        _decode_b64url(observation.get("signature"), "observation signature", 64)
    return receipt


def prerequisite_observation_payload(receipt: dict, observation: dict) -> bytes:
    value = {
        "release_id": receipt["release_id"],
        "release_manifest_sha256": receipt["release_manifest_sha256"],
        "worker_id": observation["worker_id"],
        "network_id": observation["network_id"],
        "observed_utc": observation["observed_utc"],
        "route_version": observation["route_version"],
        "checks": observation["checks"],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def source_fence_payload(receipt: dict) -> str:
    """Deterministic canonical payload covered by the source Ed25519 signature.

    The signature binds every signed claim (release identity, the exact fenced
    source identity, and the active window) so a tampered or re-substituted
    fence cannot be passed off as belonging to this release/source.
    """
    signed = {key: receipt[key] for key in sorted(_SOURCE_FENCE_SIGNED)
              if key in receipt}
    return json.dumps(signed, sort_keys=True, separators=(",", ":"))


def verify_source_fence(path: str, release_path: str) -> dict:
    """Validate a receipt signed by the source-only Ed25519 private key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    receipt = validate_source_fence(path)
    release = validate_release(release_path)
    if receipt["release_id"] != release["release_id"]:
        raise ValueError("source-fence release_id differs from release authority")
    if receipt["release_manifest_sha256"] != _sha256_file(release_path):
        raise ValueError("source-fence release digest differs from release authority")
    expected_key_id = release["source_fence_key_id"]
    if receipt["signing_key_id"] != expected_key_id:
        raise ValueError("source-fence signing_key_id differs from release authority")
    public_key = _decode_ed25519_public_key(
        release["source_fence_public_key"], "source_fence_public_key"
    )
    signature = _decode_b64url(receipt["signature"], "source-fence signature", 64)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, source_fence_payload(receipt).encode("utf-8")
        )
    except Exception as exc:
        raise ValueError("source-fence signature is invalid") from exc
    return receipt


def validate_source_fence(path: str) -> dict:
    """Validate evidence that the old/local authority can no longer write."""
    receipt = load_strict(path)
    _require_exact_keys(receipt, _SOURCE_FENCE_KEYS, "source-fence receipt")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("source-fence receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != "source-writer-fence":
        raise ValueError("source-fence receipt kind must be source-writer-fence")
    _validate_release_binding(receipt, "source-fence receipt")
    checked = _require_fresh(receipt.get("checked_utc"), "source-fence receipt.checked_utc", 300)
    expires = _parse_utc(receipt.get("expires_utc"), "source-fence receipt.expires_utc")
    now = datetime.now(timezone.utc)
    if expires <= now or expires > checked + timedelta(minutes=10):
        raise ValueError(
            "source-fence receipt expires outside the active window; "
            "expiry may be no more than 10 minutes after check"
        )
    _require_str(receipt.get("source_id"), "source-fence receipt.source_id")
    for key in ("source_writer_stopped", "source_mutation_probe_denied",
                "source_service_disabled", "source_firewall_persistent"):
        if receipt.get(key) is not True:
            raise ValueError("source-fence receipt.%s must be true" % key)
    _require_key_id(receipt.get("signing_key_id"),
                    "source-fence receipt.signing_key_id")
    signature = receipt.get("signature")
    _decode_b64url(signature, "source-fence signature", 64)
    return receipt


def validate_prerequisite_binding(path: str, release_path: str) -> dict:
    """Validate and bind an external prerequisite receipt to one release."""
    receipt = validate_prerequisite(path)
    release = validate_release(release_path)
    if receipt["release_id"] != release["release_id"]:
        raise ValueError("prerequisite release_id mismatch")
    if receipt["release_manifest_sha256"] != _sha256_file(release_path):
        raise ValueError("prerequisite release digest mismatch")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    keys = release["external_evidence_public_keys"]
    for observation in receipt["observations"]:
        worker_id = observation["worker_id"]
        if worker_id not in keys:
            raise ValueError("external observation worker is not release-pinned")
        public = _decode_ed25519_public_key(keys[worker_id], "external worker public key")
        signature = _decode_b64url(observation["signature"], "observation signature", 64)
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(
                signature, prerequisite_observation_payload(receipt, observation)
            )
        except Exception as exc:
            raise ValueError("external observation signature is invalid") from exc
    return receipt
