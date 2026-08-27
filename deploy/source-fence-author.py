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
import re
import ssl
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from menhir_schema import source_fence_payload, validate_release  # noqa: E402


MAX_JSON_BYTES = 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
NETWORK_TIMEOUT_SECONDS = 15
EVIDENCE_MAX_AGE = timedelta(minutes=5)
RECEIPT_VALIDITY = timedelta(minutes=5)

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DNS_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)

_CHALLENGE_KEYS = frozenset({
    "challenge", "instance_id", "key_id", "mutation_fence", "release_id",
    "runtime_mode", "signature",
})
_REFUSAL = {
    "error": "temporarily_unavailable",
    "error_description": "candidate-readonly mode does not admit authority mutations",
}
_EVIDENCE_COMMON_KEYS = frozenset({
    "schema", "kind", "release_id", "release_manifest_sha256", "source_id",
    "observed_utc",
})


class SourceFenceError(ValueError):
    """A safe, operator-facing source-fence refusal."""


class ProbeRequest(NamedTuple):
    method: str
    url: str
    headers: dict[str, str]
    body: bytes


class ProbeResponse(NamedTuple):
    status: int
    headers: Mapping[str, str]
    body: bytes


class TLSFiles(NamedTuple):
    ca: Path
    client_cert: Path
    client_key: Path


Transport = Callable[[ProbeRequest, TLSFiles, int], ProbeResponse]


def _inspect_regular(path: Path, label: str, maximum: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceFenceError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SourceFenceError(f"{label} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > maximum:
        raise SourceFenceError(f"{label} has an invalid size")
    return info


def _read_regular(path: Path, label: str, maximum: int) -> bytes:
    _inspect_regular(path, label, maximum)
    flags = os.O_RDONLY
    # Windows CRT text mode translates CRLF to LF. These bytes are security
    # authority: release digests and one-line token framing must remain exact.
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceFenceError(f"cannot open {label}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > maximum:
            raise SourceFenceError(f"{label} has an invalid size or type")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > maximum:
            raise SourceFenceError(f"{label} has an invalid size")
        return raw
    finally:
        os.close(descriptor)


def _require_root_owned_nonwritable(path: Path, label: str) -> None:
    """Match Menhir's root-owned, non-group/other-writable authority rule."""
    if os.name != "posix":
        return
    info = path.lstat()
    if info.st_uid != 0:
        raise SourceFenceError(f"{label} must be root-owned")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise SourceFenceError(f"{label} must not be group/other writable")


def _strict_json_bytes(raw: bytes, label: str):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceFenceError(f"invalid {label}") from exc


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


def _decode_b64url(value: object, label: str, length: int) -> bytes:
    if not isinstance(value, str) or not value or "=" in value or not _B64URL_RE.fullmatch(value):
        raise SourceFenceError(f"{label} is not canonical unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise SourceFenceError(f"{label} is invalid") from exc
    if (
        len(raw) != length
        or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value
    ):
        raise SourceFenceError(f"{label} has an invalid encoding or length")
    return raw


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


def _canonical_https_origin(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SourceFenceError("probe base must be one canonical HTTPS origin") from exc
    if (
        not isinstance(value, str)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "%" in parsed.netloc
        or port == 443
    ):
        raise SourceFenceError("probe base must be one canonical HTTPS origin")
    hostname = parsed.hostname
    if ":" in hostname:
        import ipaddress
        try:
            host = f"[{ipaddress.IPv6Address(hostname).compressed}]"
        except ValueError as exc:
            raise SourceFenceError("probe base must be one canonical HTTPS origin") from exc
    else:
        if not _DNS_RE.fullmatch(hostname):
            raise SourceFenceError("probe base must be one canonical HTTPS origin")
        host = hostname
    canonical = f"https://{host}" + (f":{port}" if port is not None else "")
    if value != canonical:
        raise SourceFenceError("probe base must be one canonical HTTPS origin")
    return canonical


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SourceFenceError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceFenceError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SourceFenceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    current = clock()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise SourceFenceError("clock must return a timezone-aware datetime")
    return current.astimezone(timezone.utc)


def _assert_fresh(observed: datetime, now: datetime, label: str) -> None:
    if observed > now + timedelta(seconds=60):
        raise SourceFenceError(f"{label} is in the future")
    if observed < now - EVIDENCE_MAX_AGE:
        raise SourceFenceError(f"{label} is stale")


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


def _read_response_json(response: ProbeResponse, label: str):
    if len(response.body) > MAX_JSON_BYTES:
        raise SourceFenceError(f"{label} response is too large")
    return _strict_json_bytes(response.body, f"{label} response")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_transport(
    request: ProbeRequest, tls: TLSFiles, timeout: int
) -> ProbeResponse:
    try:
        context = ssl.create_default_context(cafile=str(tls.ca))
        context.load_cert_chain(certfile=str(tls.client_cert), keyfile=str(tls.client_key))
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _RejectRedirects()
        )
        wire_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method=request.method,
        )
        try:
            response = opener.open(wire_request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(MAX_JSON_BYTES + 1)
            if len(body) > MAX_JSON_BYTES:
                raise SourceFenceError("source probe response is too large")
            return ProbeResponse(
                int(response.status), dict(response.headers.items()), body
            )
    except SourceFenceError:
        raise
    except Exception as exc:
        raise SourceFenceError("source probe transport or mTLS setup failed") from exc


def _call(
    transport: Transport,
    request: ProbeRequest,
    tls: TLSFiles,
    expected_status: int,
    label: str,
) -> ProbeResponse:
    try:
        response = transport(request, tls, NETWORK_TIMEOUT_SECONDS)
    except SourceFenceError:
        raise
    except Exception as exc:
        raise SourceFenceError(f"{label} transport failed") from exc
    if not isinstance(response, ProbeResponse):
        raise SourceFenceError(f"{label} transport returned an invalid response")
    if response.status != expected_status:
        raise SourceFenceError(
            f"{label} must return exact status {expected_status}, got {response.status}"
        )
    return response


def _verify_challenge_response(
    response: ProbeResponse,
    *,
    challenge: str,
    release: dict,
    expected_source_id: str,
) -> bool:
    body = _read_response_json(response, "source-fence challenge")
    if not isinstance(body, dict) or set(body) != _CHALLENGE_KEYS:
        raise SourceFenceError("source-fence challenge response has invalid exact keys")
    instance_id = body.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise SourceFenceError("source-fence challenge instance_id must be nonempty")
    claims = {key: value for key, value in body.items() if key != "signature"}
    expected = {
        "challenge": challenge,
        "instance_id": expected_source_id,
        "key_id": release["source_fence_key_id"],
        "mutation_fence": True,
        "release_id": release["release_id"],
        "runtime_mode": "candidate-readonly",
    }
    if claims != expected:
        if instance_id != expected_source_id:
            raise SourceFenceError("live challenge does not identify the same source_id")
        raise SourceFenceError("source-fence challenge claims differ from release authority")
    signature = _decode_b64url(body.get("signature"), "source-fence challenge signature", 64)
    public = _decode_b64url(
        release["source_fence_public_key"], "release source_fence_public_key", 32
    )
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    except Exception as exc:
        raise SourceFenceError("source-fence challenge signature is invalid") from exc
    return True


def _header(response: ProbeResponse, name: str) -> str | None:
    wanted = name.lower()
    for key, value in response.headers.items():
        if key.lower() == wanted:
            return value
    return None


def _verify_mutation_refusal(response: ProbeResponse) -> bool:
    body = _read_response_json(response, "source mutation probe")
    if body != _REFUSAL:
        raise SourceFenceError("source mutation probe does not match the exact refusal contract")
    if _header(response, "Retry-After") != "60" or _header(response, "Cache-Control") != "no-store":
        raise SourceFenceError("source mutation probe refusal headers are invalid")
    return True


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
