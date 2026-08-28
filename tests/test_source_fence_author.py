from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "source-fence-author.py"
SPEC = importlib.util.spec_from_file_location("source_fence_author", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SCHEMA_PATH = Path(__file__).parents[1] / "deploy" / "lib" / "menhir_schema.py"
SCHEMA_SPEC = importlib.util.spec_from_file_location("source_fence_schema", SCHEMA_PATH)
assert SCHEMA_SPEC and SCHEMA_SPEC.loader
SCHEMA = importlib.util.module_from_spec(SCHEMA_SPEC)
SCHEMA_SPEC.loader.exec_module(SCHEMA)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="ascii")


def _response(status: int, value: object, headers: dict[str, str] | None = None):
    return MODULE.ProbeResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(value, separators=(",", ":")).encode("ascii"),
    )


@pytest.fixture
def author_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    source_id = "old-source-01"
    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    tls_ca = tmp_path / "source-ca.pem"
    tls_cert = tmp_path / "source-client.pem"
    tls_key = tmp_path / "source-client-key.pem"
    tls_ca.write_bytes(b"test source ca\n")
    tls_cert.write_bytes(b"test client certificate\n")
    tls_key.write_bytes(b"test client key\n")

    release = {
        "release_id": "menhir-prod-0.2.0-11",
        "source_fence_key_id": "source-fence-v11",
        "source_fence_public_key": _b64url(public_key),
        "source_fence_tls_ca_sha256": hashlib.sha256(tls_ca.read_bytes()).hexdigest(),
    }
    release_path = tmp_path / "release.json"
    _json(release_path, release)
    release_digest = hashlib.sha256(release_path.read_bytes()).hexdigest()

    evidence_common = {
        "schema": 1,
        "release_id": release["release_id"],
        "release_manifest_sha256": release_digest,
        "source_id": source_id,
        "observed_utc": (now - timedelta(seconds=10)).isoformat(),
    }
    service_evidence = tmp_path / "service-disabled.json"
    firewall_evidence = tmp_path / "firewall-persistent.json"
    _json(service_evidence, {
        **evidence_common,
        "kind": "source-service-disabled",
        "source_service_disabled": True,
    })
    _json(firewall_evidence, {
        **evidence_common,
        "kind": "source-firewall-persistent",
        "source_firewall_persistent": True,
    })

    private_key = tmp_path / "source-fence-private.pem"
    private_key.write_bytes(signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    private_key.chmod(0o400)
    token = tmp_path / "source-fence-token"
    token.write_bytes(b"dedicated-test-token\n")
    output = tmp_path / "source-writer-fence.json"

    monkeypatch.setattr(MODULE, "validate_release", lambda path: json.loads(Path(path).read_text()))
    monkeypatch.setattr(MODULE, "_require_root_owned_nonwritable", lambda path, label: None)

    values = {
        "release_path": release_path,
        "private_key_path": private_key,
        "token_path": token,
        "service_disabled_evidence_path": service_evidence,
        "firewall_evidence_path": firewall_evidence,
        "probe_base": "https://old-source.example.test:8443",
        "tls_ca_path": tls_ca,
        "tls_cert_path": tls_cert,
        "tls_key_path": tls_key,
        "output_path": output,
        "clock": lambda: now,
        "challenge_factory": lambda: "C" * 43,
    }
    return values, release, signing_key, source_id, now


def _successful_transport(signing_key, release, source_id, seen):
    def transport(request, tls, timeout):
        seen.append((request, tls, timeout))
        assert request.headers["Authorization"] == "Bearer dedicated-test-token"
        if request.url.endswith("/internal/source-fence"):
            claims = {
                "challenge": request.headers["X-Menhir-Fence-Challenge"],
                "instance_id": source_id,
                "key_id": release["source_fence_key_id"],
                "mutation_fence": True,
                "release_id": release["release_id"],
                "runtime_mode": "candidate-readonly",
            }
            payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
            return _response(200, {**claims, "signature": _b64url(signing_key.sign(payload))})
        assert request.url.endswith("/oauth/token")
        assert request.body == (
            b"grant_type=client_credentials&client_id=source-fence-probe-wrong-identity"
        )
        return _response(
            503,
            {
                "error": "temporarily_unavailable",
                "error_description": (
                    "candidate-readonly mode does not admit authority mutations"
                ),
            },
            {"Retry-After": "60", "Cache-Control": "no-store"},
        )

    return transport


def test_authors_release_and_source_bound_receipt_only_from_both_live_checks(
    author_fixture,
) -> None:
    values, release, signing_key, source_id, now = author_fixture
    seen = []

    receipt = MODULE.produce_source_fence(
        **values,
        transport=_successful_transport(signing_key, release, source_id, seen),
    )

    assert [item[0].url for item in seen] == [
        values["probe_base"] + "/internal/source-fence",
        values["probe_base"] + "/oauth/token",
    ]
    assert all(item[0].url.startswith(values["probe_base"] + "/") for item in seen)
    assert all(item[2] == MODULE.NETWORK_TIMEOUT_SECONDS for item in seen)
    assert seen[0][1] == seen[1][1] == MODULE.TLSFiles(
        values["tls_ca_path"], values["tls_cert_path"], values["tls_key_path"]
    )
    assert receipt == json.loads(values["output_path"].read_text(encoding="ascii"))
    assert receipt["release_id"] == release["release_id"]
    assert receipt["release_manifest_sha256"] == hashlib.sha256(
        values["release_path"].read_bytes()
    ).hexdigest()
    assert receipt["source_id"] == source_id
    assert receipt["source_writer_stopped"] is True
    assert receipt["source_mutation_probe_denied"] is True
    assert receipt["source_service_disabled"] is True
    assert receipt["source_firewall_persistent"] is True
    checked = datetime.fromisoformat(receipt["checked_utc"])
    expires = datetime.fromisoformat(receipt["expires_utc"])
    assert checked == now
    assert timedelta(0) < expires - checked <= timedelta(minutes=5)
    signature = base64.urlsafe_b64decode(receipt["signature"] + "==")
    signing_key.public_key().verify(
        signature, SCHEMA.source_fence_payload(receipt).encode("utf-8")
    )
    if os.name == "posix":
        assert stat.S_IMODE(values["output_path"].stat().st_mode) == 0o400


def test_binary_file_reader_preserves_release_bytes_on_windows(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    raw = b'{"release_id":"menhir-prod-0.2.0-11"}\r\n'
    path.write_bytes(raw)
    assert MODULE._read_regular(path, "authority", MODULE.MAX_JSON_BYTES) == raw


@pytest.mark.parametrize(
    "origin",
    [
        "http://old-source.example.test",
        "https://user@old-source.example.test",
        "https://old-source.example.test/path",
        "https://old-source.example.test?query=1",
        "https://old-source.example.test#fragment",
        "https://OLD-source.example.test",
        "https://old-source.example.test:443",
        "https://old-source.example.test/",
    ],
)
def test_probe_base_must_be_one_canonical_https_origin(origin: str) -> None:
    with pytest.raises(MODULE.SourceFenceError, match="canonical HTTPS origin"):
        MODULE._canonical_https_origin(origin)


def test_redirect_is_not_accepted_as_a_live_source_check(author_fixture) -> None:
    values, _release, _key, _source_id, _now = author_fixture

    def redirect(request, tls, timeout):
        return MODULE.ProbeResponse(302, {"Location": "https://other.example.test/"}, b"")

    with pytest.raises(MODULE.SourceFenceError, match="status 200"):
        MODULE.produce_source_fence(**values, transport=redirect)
    assert not values["output_path"].exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda claims: claims.update(release_id="menhir-prod-9.9.9-9"), "claims"),
        (lambda claims: claims.update(key_id="other-key"), "claims"),
        (lambda claims: claims.update(runtime_mode="production"), "claims"),
        (lambda claims: claims.update(mutation_fence=False), "claims"),
        (lambda claims: claims.update(instance_id=""), "instance_id"),
        (lambda claims: claims.update(instance_id="different-source"), "same source_id"),
        (lambda claims: claims.update(unexpected=True), "keys"),
    ],
)
def test_challenge_response_requires_exact_release_pinned_claims(
    author_fixture, change, message
) -> None:
    values, release, signing_key, source_id, _now = author_fixture

    def transport(request, tls, timeout):
        claims = {
            "challenge": request.headers["X-Menhir-Fence-Challenge"],
            "instance_id": source_id,
            "key_id": release["source_fence_key_id"],
            "mutation_fence": True,
            "release_id": release["release_id"],
            "runtime_mode": "candidate-readonly",
        }
        change(claims)
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        return _response(200, {**claims, "signature": _b64url(signing_key.sign(payload))})

    with pytest.raises(MODULE.SourceFenceError, match=message):
        MODULE.produce_source_fence(**values, transport=transport)


def test_challenge_signature_must_match_release_public_key(author_fixture) -> None:
    values, release, _signing_key, source_id, _now = author_fixture
    wrong_key = Ed25519PrivateKey.generate()
    seen = []
    with pytest.raises(MODULE.SourceFenceError, match="signature"):
        MODULE.produce_source_fence(
            **values,
            transport=_successful_transport(wrong_key, release, source_id, seen),
        )


@pytest.mark.parametrize(
    ("status", "body", "headers", "message"),
    [
        (200, {}, {}, "status 503"),
        (503, {"error": "temporarily_unavailable"}, {"Retry-After": "60", "Cache-Control": "no-store"}, "contract"),
        (503, {"error": "temporarily_unavailable", "error_description": "wrong"}, {"Retry-After": "60", "Cache-Control": "no-store"}, "contract"),
        (503, {"error": "temporarily_unavailable", "error_description": "candidate-readonly mode does not admit authority mutations"}, {}, "headers"),
    ],
)
def test_mutation_probe_requires_exact_structured_refusal(
    author_fixture, status, body, headers, message
) -> None:
    values, release, signing_key, source_id, _now = author_fixture
    good = _successful_transport(signing_key, release, source_id, [])

    def transport(request, tls, timeout):
        if request.url.endswith("/internal/source-fence"):
            return good(request, tls, timeout)
        return _response(status, body, headers)

    with pytest.raises(MODULE.SourceFenceError, match=message):
        MODULE.produce_source_fence(**values, transport=transport)


@pytest.mark.parametrize(
    ("target", "mutation", "message"),
    [
        ("service_disabled_evidence_path", lambda body: body.update(extra=True), "exact keys"),
        ("service_disabled_evidence_path", lambda body: body.update(release_id="menhir-prod-9.9.9-9"), "release_id"),
        ("service_disabled_evidence_path", lambda body: body.update(release_manifest_sha256="0" * 64), "release digest"),
        ("firewall_evidence_path", lambda body: body.update(source_id="different-source"), "same source_id"),
        ("firewall_evidence_path", lambda body: body.update(observed_utc="2026-08-27T14:00:00+00:00"), "stale"),
        ("firewall_evidence_path", lambda body: body.update(source_firewall_persistent=False), "must be true"),
    ],
)
def test_local_evidence_is_strict_release_bound_source_bound_and_fresh(
    author_fixture, target, mutation, message
) -> None:
    values, release, signing_key, source_id, _now = author_fixture
    path = values[target]
    body = json.loads(path.read_text())
    mutation(body)
    _json(path, body)

    with pytest.raises(MODULE.SourceFenceError, match=message):
        MODULE.produce_source_fence(
            **values,
            transport=_successful_transport(signing_key, release, source_id, []),
        )


def test_private_key_must_match_release_authority(author_fixture) -> None:
    values, release, signing_key, source_id, _now = author_fixture
    other = Ed25519PrivateKey.generate()
    values["private_key_path"].chmod(0o600)
    values["private_key_path"].write_bytes(other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    values["private_key_path"].chmod(0o400)

    with pytest.raises(MODULE.SourceFenceError, match="public key"):
        MODULE.produce_source_fence(
            **values,
            transport=_successful_transport(signing_key, release, source_id, []),
        )


def test_private_key_requires_absolute_path_and_posix_0400(
    author_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, release, signing_key, source_id, _now = author_fixture
    relative = Path(values["private_key_path"].name)
    with pytest.raises(MODULE.SourceFenceError, match="absolute"):
        MODULE._load_signing_key(relative, release)

    if os.name == "posix":
        values["private_key_path"].chmod(0o600)
        monkeypatch.setattr(MODULE, "_require_root_owned_nonwritable", lambda path, label: None)
        with pytest.raises(MODULE.SourceFenceError, match="0400 or stricter"):
            MODULE._load_signing_key(values["private_key_path"], release)


def test_evidence_files_must_cross_root_nonwritable_check(
    author_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, release, signing_key, source_id, _now = author_fixture

    def custody(path, label):
        if Path(path) == values["service_disabled_evidence_path"]:
            raise MODULE.SourceFenceError("source service disabled evidence must be root-owned")

    monkeypatch.setattr(MODULE, "_require_root_owned_nonwritable", custody)
    with pytest.raises(MODULE.SourceFenceError, match="must be root-owned"):
        MODULE.produce_source_fence(
            **values,
            transport=_successful_transport(signing_key, release, source_id, []),
        )


def test_mtls_ca_is_bound_to_release_digest(author_fixture) -> None:
    values, release, signing_key, source_id, _now = author_fixture
    values["tls_ca_path"].write_bytes(b"different ca\n")
    with pytest.raises(MODULE.SourceFenceError, match="TLS CA differs"):
        MODULE.produce_source_fence(
            **values,
            transport=_successful_transport(signing_key, release, source_id, []),
        )


def test_receipt_output_requires_absolute_path() -> None:
    with pytest.raises(MODULE.SourceFenceError, match="output path must be absolute"):
        MODULE._atomic_write(Path("source-writer-fence.json"), b"{}\n")


def test_receipt_output_parent_crosses_root_nonwritable_check(
    author_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, release, signing_key, source_id, _now = author_fixture

    def custody(path, label):
        if label == "receipt output directory":
            raise MODULE.SourceFenceError(
                "receipt output directory must not be group/other writable"
            )

    monkeypatch.setattr(MODULE, "_require_root_owned_nonwritable", custody)
    with pytest.raises(MODULE.SourceFenceError, match="output directory"):
        MODULE.produce_source_fence(
            **values,
            transport=_successful_transport(signing_key, release, source_id, []),
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "invalid size"),
        (b"token", "exactly one"),
        (b"token\nsecond\n", "exactly one"),
        (b"token\r\n", "LF, not CRLF"),
    ],
)
def test_bearer_token_file_is_one_nonempty_lf_terminated_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, message: str
) -> None:
    path = tmp_path / "token"
    path.write_bytes(raw)
    monkeypatch.setattr(MODULE, "_require_root_owned_nonwritable", lambda path, label: None)
    with pytest.raises(MODULE.SourceFenceError, match=message):
        MODULE._read_token(path)
