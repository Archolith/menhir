#!/usr/bin/env python3
"""Author a short-lived, source-side writer-fence receipt.

The receipt's writer-stopped and mutation-denied claims are derived only from
an authenticated live challenge and the old source's exact mutation refusal.
The remaining two claims come from strict, fresh, root-controlled local
evidence.  The source-only signing key never leaves this process.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

# Keep the sibling parts package importable even when this module is loaded by
# path (importlib spec) rather than imported as a script or package member.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.append(str(SCRIPT_DIR))

from menhir_schema import source_fence_payload, validate_release  # noqa: E402
from source_fence_author_parts import (  # noqa: E402
    EVIDENCE_MAX_AGE,
    MAX_JSON_BYTES,
    MAX_KEY_BYTES,
    MAX_TOKEN_BYTES,
    NETWORK_TIMEOUT_SECONDS,
    ProbeRequest,
    ProbeResponse,
    RECEIPT_VALIDITY,
    SourceFenceError,
    TLSFiles,
    Transport,
    _B64URL_RE,
    _CHALLENGE_KEYS,
    _CHALLENGE_RE,
    _DNS_RE,
    _EVIDENCE_COMMON_KEYS,
    _REFUSAL,
    _RejectRedirects,
    _assert_fresh,
    _call,
    _canonical_https_origin,
    _decode_b64url,
    _default_transport,
    _header,
    _inspect_regular,
    _parse_utc,
    _read_regular,
    _read_response_json,
    _require_root_owned_nonwritable,
    _strict_json_bytes,
    _utc_now,
    _verify_challenge_response,
    _verify_mutation_refusal,
)


def _load_release(path: Path) -> tuple[dict, str]:
    _require_root_owned_nonwritable(path, "release authority")
    before = _read_regular(path, "release authority", MAX_JSON_BYTES)
    try:
        release = validate_release(str(path))
    except (OSError, ValueError) as exc:
        raise SourceFenceError("invalid release authority") from exc
    after = _read_regular(path, "release authority", MAX_JSON_BYTES)
    if before != after or _strict_json_bytes(after, "release authority") != release:
        raise SourceFenceError("release authority changed while it was loaded")
    return release, hashlib.sha256(after).hexdigest()


def _load_signing_key(path: Path, release: dict) -> Ed25519PrivateKey:
    if not path.is_absolute():
        raise SourceFenceError("private signing key path must be absolute")
    info = _inspect_regular(path, "private signing key", MAX_KEY_BYTES)
    _require_root_owned_nonwritable(path, "private signing key")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & ~0o400:
        raise SourceFenceError("private signing key mode must be 0400 or stricter")
    try:
        key = serialization.load_pem_private_key(
            _read_regular(path, "private signing key", MAX_KEY_BYTES), password=None
        )
    except (TypeError, ValueError) as exc:
        raise SourceFenceError("private signing key is not a usable Ed25519 PEM key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SourceFenceError("private signing key is not Ed25519")
    actual_public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    expected_public = _decode_b64url(
        release.get("source_fence_public_key"), "release source_fence_public_key", 32
    )
    if actual_public != expected_public:
        raise SourceFenceError("private signing key public key differs from release authority")
    return key


def _read_token(path: Path) -> str:
    _require_root_owned_nonwritable(path, "source-fence bearer token")
    raw = _read_regular(path, "source-fence bearer token", MAX_TOKEN_BYTES)
    if raw.endswith(b"\r\n"):
        raise SourceFenceError("source-fence bearer token must use LF, not CRLF")
    if raw.count(b"\n") != 1 or not raw.endswith(b"\n") or b"\r" in raw:
        raise SourceFenceError("source-fence bearer token must contain exactly one LF-terminated line")
    token_bytes = raw[:-1]
    if not token_bytes or any(byte < 0x21 or byte > 0x7E for byte in token_bytes):
        raise SourceFenceError("source-fence bearer token must contain exactly one nonempty printable line")
    return token_bytes.decode("ascii")


def _load_evidence(
    path: Path,
    *,
    kind: str,
    claim: str,
    release: dict,
    release_digest: str,
    now: datetime,
) -> tuple[str, datetime]:
    label = kind.replace("-", " ") + " evidence"
    _require_root_owned_nonwritable(path, label)
    body = _strict_json_bytes(_read_regular(path, label, MAX_JSON_BYTES), label)
    expected_keys = _EVIDENCE_COMMON_KEYS | {claim}
    if not isinstance(body, dict) or set(body) != expected_keys:
        raise SourceFenceError(f"{label} must contain exact keys")
    if body.get("schema") != 1 or body.get("kind") != kind:
        raise SourceFenceError(f"{label} schema or kind mismatch")
    if body.get("release_id") != release.get("release_id"):
        raise SourceFenceError(f"{label} release_id mismatch")
    if body.get("release_manifest_sha256") != release_digest:
        raise SourceFenceError(f"{label} release digest mismatch")
    source_id = body.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise SourceFenceError(f"{label} source_id must be nonempty")
    if body.get(claim) is not True:
        raise SourceFenceError(f"{label} {claim} must be true")
    observed = _parse_utc(body.get("observed_utc"), f"{label} observed_utc")
    _assert_fresh(observed, now, label)
    return source_id, observed


def _tls_files(
    ca_path: Path,
    cert_path: Path,
    key_path: Path,
    release: dict,
) -> TLSFiles:
    values = (
        (ca_path, "source-fence TLS CA"),
        (cert_path, "source-fence mTLS client certificate"),
        (key_path, "source-fence mTLS client key"),
    )
    for path, label in values:
        _inspect_regular(path, label, MAX_KEY_BYTES)
        _require_root_owned_nonwritable(path, label)
    ca_digest = hashlib.sha256(
        _read_regular(ca_path, "source-fence TLS CA", MAX_KEY_BYTES)
    ).hexdigest()
    if ca_digest != release.get("source_fence_tls_ca_sha256"):
        raise SourceFenceError("source-fence TLS CA differs from release authority")
    return TLSFiles(ca_path, cert_path, key_path)


def _atomic_write(output: Path, payload: bytes) -> None:
    if not output.is_absolute():
        raise SourceFenceError("receipt output path must be absolute")
    parent = output.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise SourceFenceError("cannot inspect receipt output directory") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SourceFenceError("receipt output directory must be a real directory")
    _require_root_owned_nonwritable(parent, "receipt output directory")
    if os.path.lexists(output):
        info = output.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SourceFenceError("receipt output must not replace a symlink or special file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".source-fence-", dir=parent)
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor_open = False
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, output)
        if os.name == "posix":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor_open:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()


def produce_source_fence(
    *,
    release_path: Path,
    private_key_path: Path,
    token_path: Path,
    service_disabled_evidence_path: Path,
    firewall_evidence_path: Path,
    probe_base: str,
    tls_ca_path: Path,
    tls_cert_path: Path,
    tls_key_path: Path,
    output_path: Path,
    transport: Transport | None = None,
    clock: Callable[[], datetime] | None = None,
    challenge_factory: Callable[[], str] | None = None,
) -> dict:
    """Verify source retirement live and author its release-bound receipt."""
    active_clock = clock or (lambda: datetime.now(timezone.utc))
    start = _utc_now(active_clock)
    origin = _canonical_https_origin(probe_base)
    release, release_digest = _load_release(Path(release_path))
    signing_key = _load_signing_key(Path(private_key_path), release)
    token = _read_token(Path(token_path))
    tls = _tls_files(
        Path(tls_ca_path), Path(tls_cert_path), Path(tls_key_path), release
    )

    source_id, service_observed = _load_evidence(
        Path(service_disabled_evidence_path),
        kind="source-service-disabled",
        claim="source_service_disabled",
        release=release,
        release_digest=release_digest,
        now=start,
    )
    firewall_source_id, firewall_observed = _load_evidence(
        Path(firewall_evidence_path),
        kind="source-firewall-persistent",
        claim="source_firewall_persistent",
        release=release,
        release_digest=release_digest,
        now=start,
    )
    if firewall_source_id != source_id:
        raise SourceFenceError("local evidence files must bind the same source_id")

    if challenge_factory is None:
        import secrets
        challenge = secrets.token_urlsafe(32)
    else:
        challenge = challenge_factory()
    if not isinstance(challenge, str) or not _CHALLENGE_RE.fullmatch(challenge):
        raise SourceFenceError("fresh challenge generator returned an invalid challenge")

    active_transport = transport or _default_transport
    authorization = f"Bearer {token}"
    challenge_response = _call(
        active_transport,
        ProbeRequest(
            "POST",
            origin + "/internal/source-fence",
            {
                "Accept": "application/json",
                "Authorization": authorization,
                "X-Menhir-Fence-Challenge": challenge,
            },
            b"",
        ),
        tls,
        200,
        "source-fence challenge",
    )
    source_writer_stopped = _verify_challenge_response(
        challenge_response,
        challenge=challenge,
        release=release,
        expected_source_id=source_id,
    )

    mutation_response = _call(
        active_transport,
        ProbeRequest(
            "POST",
            origin + "/oauth/token",
            {
                "Accept": "application/json",
                "Authorization": authorization,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            b"grant_type=client_credentials&client_id=source-fence-probe-wrong-identity",
        ),
        tls,
        503,
        "source mutation probe",
    )
    source_mutation_probe_denied = _verify_mutation_refusal(mutation_response)

    checked = _utc_now(active_clock)
    _assert_fresh(service_observed, checked, "source service disabled evidence")
    _assert_fresh(firewall_observed, checked, "source firewall persistent evidence")
    receipt = {
        "schema": 1,
        "kind": "source-writer-fence",
        "release_id": release["release_id"],
        "release_manifest_sha256": release_digest,
        "checked_utc": checked.isoformat(),
        "expires_utc": (checked + RECEIPT_VALIDITY).isoformat(),
        "source_id": source_id,
        "source_writer_stopped": source_writer_stopped,
        "source_mutation_probe_denied": source_mutation_probe_denied,
        "source_service_disabled": True,
        "source_firewall_persistent": True,
        "signing_key_id": release["source_fence_key_id"],
    }
    receipt["signature"] = base64.urlsafe_b64encode(
        signing_key.sign(source_fence_payload(receipt).encode("utf-8"))
    ).rstrip(b"=").decode("ascii")
    encoded = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    _atomic_write(Path(output_path), encoded)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Author a verified short-lived Menhir source-writer-fence receipt."
    )
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--service-disabled-evidence", required=True, type=Path)
    parser.add_argument("--firewall-evidence", required=True, type=Path)
    parser.add_argument("--probe-base", required=True)
    parser.add_argument("--tls-ca", required=True, type=Path)
    parser.add_argument("--tls-cert", required=True, type=Path)
    parser.add_argument("--tls-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        produce_source_fence(
            release_path=args.release,
            private_key_path=args.private_key,
            token_path=args.token_file,
            service_disabled_evidence_path=args.service_disabled_evidence,
            firewall_evidence_path=args.firewall_evidence,
            probe_base=args.probe_base,
            tls_ca_path=args.tls_ca,
            tls_cert_path=args.tls_cert,
            tls_key_path=args.tls_key,
            output_path=args.output,
        )
    except SourceFenceError as exc:
        parser.error(str(exc))
    print(f"wrote source-fence receipt: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
