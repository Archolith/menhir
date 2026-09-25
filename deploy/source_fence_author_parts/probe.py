"""mTLS probe transport, call plumbing, and live source-check verification."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .base import (
    _CHALLENGE_KEYS,
    _DNS_RE,
    _REFUSAL,
    MAX_JSON_BYTES,
    NETWORK_TIMEOUT_SECONDS,
    SourceFenceError,
)
from .fsio import _decode_b64url, _strict_json_bytes


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
