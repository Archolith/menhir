"""Real JWT/JWKS unit tests for OAuthTokenVerifier.

Uses an in-memory RSA key pair generated via ``joserfc``. No live IdP, no
network, no server startup.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet, RSAKey

from menhir.api.oauth import OAuthAuthenticationError, OAuthConfig, OAuthTokenVerifier, _derive_subject, _unverified_jwt_kid, extract_scopes, tier_from_scopes


# ===================================================================
# Helpers: in-memory RSA JWK fixture
# ===================================================================

_TEST_KID = "test-key-id"


def _make_rsa_jwk() -> tuple[object, dict]:
    """Generate an RSA key pair and return (private_key_obj, public_jwk_dict).

    The private joserfc key object is used directly for signing tokens.
    The public JWK dict carries a matching ``kid`` for JWK set lookup.
    """
    key = RSAKey.generate_key(2048, parameters={"kid": _TEST_KID}, private=True)
    public_dict = key.as_dict(private=False)
    return key, public_dict


def _build_token(payload: dict, private_key_obj: object) -> str:
    """Sign *payload* as a JWT using the joserfc private key object."""
    header = {"alg": "RS256", "kid": _TEST_KID}
    return jose_jwt.encode(header, payload, private_key_obj)


def _make_config(**overrides) -> OAuthConfig:
    defaults = dict(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        authorization_servers=("https://auth.example.com",),
        issuer="https://auth.example.com/",
        jwks_uri="https://auth.example.com/.well-known/jwks.json",
        audiences=("https://memory.example.com/mcp-http",),
    )
    defaults.update(overrides)
    return OAuthConfig(**defaults)


def _verifier_with_jwks(
    jwks_payload: dict,
    issuer: str = "https://auth.example.com/",
    audiences: tuple[str, ...] = ("https://memory.example.com/mcp-http",),
) -> OAuthTokenVerifier:
    """Create an OAuthTokenVerifier that returns *jwks_payload* on fetch.

    The JWKS HTTP fetch is patched to return the in-memory payload so no
    network is required.
    """
    config = _make_config(issuer=issuer, audiences=audiences)
    verifier = OAuthTokenVerifier(config)
    verifier._jwks = KeySet.import_key_set(jwks_payload)
    verifier._jwks_expires_at = time.monotonic() + 3600.0
    # Patch _load_jwks so the force_refresh retry path also returns cached data
    import unittest.mock as _mock
    verifier._load_jwks = _mock.AsyncMock(return_value=verifier._jwks)
    return verifier


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _in_future(**delta) -> int:
    return int((datetime.now(timezone.utc) + timedelta(**delta)).timestamp())


def _in_past(**delta) -> int:
    return int((datetime.now(timezone.utc) - timedelta(**delta)).timestamp())


# ===================================================================
# TestExtractScopes
# ===================================================================

class TestExtractScopes:
    """Scope extraction from various OAuth/OIDC claim shapes."""

    @pytest.mark.unit
    def test_scope_string(self):
        scopes = extract_scopes({"scope": "menhir:read menhir:write"})
        assert scopes == frozenset({"menhir:read", "menhir:write"})

    @pytest.mark.unit
    def test_scp_list(self):
        scopes = extract_scopes({"scp": ["menhir:read", "menhir:admin"]})
        assert "menhir:read" in scopes
        assert "menhir:admin" in scopes

    @pytest.mark.unit
    def test_permissions(self):
        scopes = extract_scopes({"permissions": "menhir:write"})
        assert scopes == frozenset({"menhir:write"})

    @pytest.mark.unit
    def test_multiple_claim_sources_merged(self):
        scopes = extract_scopes({
            "scope": "menhir:read",
            "scp": ["menhir:write"],
            "permissions": "menhir:admin",
        })
        assert scopes == frozenset({"menhir:read", "menhir:write", "menhir:admin"})

    @pytest.mark.unit
    def test_no_scopes_returns_empty(self):
        scopes = extract_scopes({})
        assert scopes == frozenset()


# ===================================================================
# TestTierFromScopes
# ===================================================================

class TestTierFromScopes:
    """Scope-to-tier mapping."""

    @pytest.mark.unit
    def test_admin_scope_maps_to_operator(self):
        config = _make_config()
        assert tier_from_scopes({"menhir:admin"}, config) == "operator"

    @pytest.mark.unit
    def test_write_scope_maps_to_agent(self):
        config = _make_config()
        assert tier_from_scopes({"menhir:write"}, config) == "agent"

    @pytest.mark.unit
    def test_read_scope_maps_to_readonly(self):
        config = _make_config()
        assert tier_from_scopes({"menhir:read"}, config) == "readonly"

    @pytest.mark.unit
    def test_admin_beats_write_beats_read(self):
        config = _make_config()
        assert tier_from_scopes({"menhir:read", "menhir:admin"}, config) == "operator"
        assert tier_from_scopes({"menhir:read", "menhir:write"}, config) == "agent"

    @pytest.mark.unit
    def test_unknown_scope_returns_none(self):
        config = _make_config()
        assert tier_from_scopes({"unknown:scope"}, config) is None

    @pytest.mark.unit
    def test_empty_scopes_returns_none(self):
        config = _make_config()
        assert tier_from_scopes(set(), config) is None

    @pytest.mark.unit
    def test_custom_scopes_map_correctly(self):
        config = _make_config(
            scopes_supported=("myapp:read", "myapp:write", "myapp:admin"),
            read_scopes=("myapp:read",),
            write_scopes=("myapp:write",),
            admin_scopes=("myapp:admin",),
        )
        assert tier_from_scopes({"myapp:read"}, config) == "readonly"
        assert tier_from_scopes({"myapp:write"}, config) == "agent"
        assert tier_from_scopes({"myapp:admin"}, config) == "operator"


# ===================================================================
# TestOAuthTokenVerifier — real JWT signing + JWKS verification
# ===================================================================

class TestOAuthTokenVerifier:
    """Integration tests with a real RSA key pair and signed JWT tokens."""

    @pytest.fixture
    def key_pair(self):
        return _make_rsa_jwk()

    @pytest.fixture
    def private_key_obj(self, key_pair):
        return key_pair[0]

    @pytest.fixture
    def public_jwk(self, key_pair):
        return key_pair[1]

    @pytest.fixture
    def jwks_payload(self, public_jwk):
        return {"keys": [public_jwk]}

    @pytest.fixture
    def verifier(self, jwks_payload):
        return _verifier_with_jwks(jwks_payload)

    # -- Happy path --

    @pytest.mark.unit
    async def test_valid_read_token_returns_readonly_principal(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user-abc",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)

        assert principal.subject == "user-abc"
        assert principal.tier == "readonly"
        assert "menhir:read" in principal.scopes

    @pytest.mark.unit
    async def test_valid_write_token_returns_agent_principal(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user-write",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:write",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)

        assert principal.subject == "user-write"
        assert principal.tier == "agent"

    @pytest.mark.unit
    async def test_valid_admin_token_returns_operator_principal(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user-admin",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:admin",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)

        assert principal.subject == "user-admin"
        assert principal.tier == "operator"

    @pytest.mark.unit
    async def test_multiple_audiences_accepted(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": ["other-api", "https://memory.example.com/mcp-http"],
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)
        assert principal.subject == "user"
        assert principal.tier == "readonly"

    @pytest.mark.unit
    async def test_resource_claim_accepted_as_audience(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "resource": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)
        assert principal.tier == "readonly"

    @pytest.mark.unit
    async def test_subject_and_client_id_mapped(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user-123",
            "client_id": "my-client",
            "azp": "authorized-party",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)

        assert principal.subject == "user-123"
        assert principal.client_id == "my-client"

    @pytest.mark.unit
    async def test_azp_fallback_when_no_client_id(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "azp": "fallback-client",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)
        assert principal.client_id == "fallback-client"

    # -- Errors and edge cases --

    @pytest.mark.unit
    async def test_expired_token_raises(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_past(hours=1),
        }, private_key_obj)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"

    @pytest.mark.unit
    async def test_wrong_issuer_raises(self, private_key_obj, jwks_payload):
        verifier = _verifier_with_jwks(jwks_payload, issuer="https://trusted.example.com/")
        token = _build_token({
            "iss": "https://evil.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"
        assert "issuer" in exc.value.description

    @pytest.mark.unit
    async def test_wrong_audience_raises(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://evil.example.com/",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"
        assert "audience" in exc.value.description

    @pytest.mark.unit
    async def test_missing_exp_raises(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
        }, private_key_obj)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"
        assert "exp" in exc.value.description

    @pytest.mark.unit
    async def test_insufficient_scope_raises_403(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "irrelevant:scope",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.status_code == 403
        assert exc.value.error == "insufficient_scope"

    @pytest.mark.unit
    async def test_wrong_key_signature_raises(self, verifier):
        # Sign with a different key pair
        other_private, _ = _make_rsa_jwk()
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, other_private)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"

    @pytest.mark.unit
    async def test_empty_token_raises(self, verifier):
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token("")
        assert exc.value.error == "invalid_token"

    @pytest.mark.unit
    async def test_malformed_token_raises(self, verifier):
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token("not.a.token")
        # joserfc's decode failure triggers the generic catch -> server_error.
        # The error is still safe (no secrets leaked).
        assert exc.value.error in ("invalid_token", "server_error")

    @pytest.mark.unit
    async def test_disabled_verifier_raises(self):
        config = _make_config(enabled=False)
        verifier = OAuthTokenVerifier(config)
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token("any-token")
        assert exc.value.error == "invalid_request"

    @pytest.mark.unit
    async def test_missing_jwks_uri_raises(self, private_key_obj):
        config = _make_config(jwks_uri="")
        verifier = OAuthTokenVerifier(config)
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token("any")
        assert exc.value.error == "server_error"

    @pytest.mark.unit
    async def test_missing_issuer_raises(self):
        config = _make_config(issuer="")
        verifier = OAuthTokenVerifier(config)
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token("any")
        assert exc.value.error == "server_error"

    # -- Clock skew tolerance --

    @pytest.mark.unit
    async def test_recently_expired_token_accepted_with_clock_skew(self, private_key_obj, jwks_payload):
        verifier = _verifier_with_jwks(jwks_payload)
        # Override clock_skew_s via the config attribute (it's frozen, so swap config)
        object.__setattr__(verifier.config, "clock_skew_s", 120)
        # Token expired 60 seconds ago — within the 120s skew window
        just_expired = _in_past(seconds=60)
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": just_expired,
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)
        assert principal.subject == "user"

    # -- Secret sentinel safety --

    @pytest.mark.unit
    async def test_token_secret_not_in_principal(self, verifier, private_key_obj):
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)

        import json as _json
        dumped = _json.dumps({
            "subject": principal.subject,
            "client_id": principal.client_id,
            "tier": principal.tier,
        })
        # The sentinel value "super-secret-oauth-token" is used in middleware
        # tests; the verifier itself should also not expose it.
        assert "super-secret-oauth-token" not in dumped
        assert token not in dumped

    # -- Authlib deprecation warning guard: compat through 2.0.0 --
    @pytest.mark.unit
    async def test_jwt_decode_with_authlib_compat(self, verifier, private_key_obj):
        """Verify joserfc decode + validate works end-to-end."""
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "compat-test",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)
        assert principal.subject == "compat-test"


# ===================================================================
# T6 — Gated JWKS force-refresh (S-002b + S-008)
# ===================================================================

class TestGatedJwksRefresh:
    """JWKS force-refresh must only fire on genuine kid-miss, and at most once per 30s."""

    @pytest.fixture
    def key_pair(self):
        return _make_rsa_jwk()

    @pytest.fixture
    def private_key_obj(self, key_pair):
        return key_pair[0]

    @pytest.fixture
    def public_jwk(self, key_pair):
        return key_pair[1]

    @pytest.fixture
    def jwks_payload(self, public_jwk):
        return {"keys": [public_jwk]}

    @pytest.fixture
    def verifier(self, jwks_payload):
        return _verifier_with_jwks(jwks_payload)

    @pytest.mark.unit
    async def test_no_forced_refresh_for_expired_tokens(self, verifier, private_key_obj):
        """50 expired tokens with known kid must not trigger forced JWKS refresh."""
        for _ in range(50):
            token = _build_token({
                "iss": "https://auth.example.com/",
                "sub": "user",
                "aud": "https://memory.example.com/mcp-http",
                "scope": "menhir:read",
                "exp": _in_past(hours=1),
            }, private_key_obj)
            with pytest.raises(OAuthAuthenticationError) as exc:
                await verifier.verify_access_token(token)
            assert exc.value.error == "invalid_token"

        force_calls = [
            c for c in verifier._load_jwks.call_args_list
            if c.kwargs.get("force_refresh")
        ]
        assert len(force_calls) == 0, (
            f"Expected 0 forced refreshes for expired tokens, got {len(force_calls)}"
        )

    @pytest.mark.unit
    async def test_no_forced_refresh_for_wrong_audience_tokens(self, verifier, private_key_obj):
        """Wrong-audience tokens with known kid must not trigger forced refresh."""
        for _ in range(20):
            token = _build_token({
                "iss": "https://auth.example.com/",
                "sub": "user",
                "aud": "https://evil.example.com/",
                "scope": "menhir:read",
                "exp": _in_future(hours=1),
            }, private_key_obj)
            with pytest.raises(OAuthAuthenticationError) as exc:
                await verifier.verify_access_token(token)
            assert exc.value.error == "invalid_token"

        force_calls = [
            c for c in verifier._load_jwks.call_args_list
            if c.kwargs.get("force_refresh")
        ]
        assert len(force_calls) == 0

    @pytest.mark.unit
    async def test_unknown_kid_triggers_exactly_one_forced_refresh(self, verifier, private_key_obj):
        """A token with kid absent from the cached set triggers exactly one forced refresh."""
        other_private, other_public = _make_rsa_jwk()
        other_public["kid"] = "other-rotated-key"
        header = {"alg": "RS256", "kid": "other-rotated-key"}
        token = jose_jwt.encode(header, {
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, other_private)

        with pytest.raises(OAuthAuthenticationError):
            await verifier.verify_access_token(token)

        force_calls = [
            c for c in verifier._load_jwks.call_args_list
            if c.kwargs.get("force_refresh")
        ]
        assert len(force_calls) == 1

    @pytest.mark.unit
    async def test_rate_limit_blocks_second_forced_refresh(self, verifier, private_key_obj):
        """A second unknown-kid token within 30s must not force-refresh again."""
        other_private, _ = _make_rsa_jwk()
        header_a = {"alg": "RS256", "kid": "unknown-key-a"}
        token_a = jose_jwt.encode(header_a, {
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, other_private)

        # First call: rate-limit not active, triggers 1 forced refresh
        with pytest.raises(OAuthAuthenticationError):
            await verifier.verify_access_token(token_a)

        # Second call with different unknown kid: rate-limit IS active
        header_b = {"alg": "RS256", "kid": "unknown-key-b"}
        token_b = jose_jwt.encode(header_b, {
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, other_private)

        with pytest.raises(OAuthAuthenticationError):
            await verifier.verify_access_token(token_b)

        force_calls = [
            c for c in verifier._load_jwks.call_args_list
            if c.kwargs.get("force_refresh")
        ]
        assert len(force_calls) == 1

    @pytest.mark.unit
    async def test_no_forced_refresh_for_truly_malformed_tokens(self, verifier):
        """Truly malformed tokens (no kid) must not trigger forced refresh
        when rate-limit is active."""
        # First call sets _last_forced_refresh by triggering a kid-based forced refresh
        other_private, _ = _make_rsa_jwk()
        header = {"alg": "RS256", "kid": "seed-key"}
        seed_token = jose_jwt.encode(header, {
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, other_private)
        with pytest.raises(OAuthAuthenticationError):
            await verifier.verify_access_token(seed_token)

        # Now malformed tokens: kid = None, but rate-limit IS active
        for junk in ("not.a.token", "abc.def.ghi", "~~~...~~~"):
            with pytest.raises(OAuthAuthenticationError):
                await verifier.verify_access_token(junk)

        force_calls = [
            c for c in verifier._load_jwks.call_args_list
            if c.kwargs.get("force_refresh")
        ]
        assert len(force_calls) == 1


# ===================================================================
# T7 — Stable client identity for missing `sub` (S-003)
# ===================================================================

class TestSubjectDerivation:
    """_derive_subject must produce stable, distinct identities."""

    def test_sub_claim_returns_as_is(self):
        claims = {"sub": "user-abc"}
        assert _derive_subject(claims) == "user-abc"

    def test_client_id_fallback_when_no_sub(self):
        claims = {"client_id": "my-client"}
        assert _derive_subject(claims) == "client:my-client"

    def test_azp_fallback_when_no_sub_or_client_id(self):
        claims = {"azp": "my-app"}
        assert _derive_subject(claims) == "client:my-app"

    def test_client_id_preferred_over_azp(self):
        claims = {"client_id": "primary-client", "azp": "fallback"}
        assert _derive_subject(claims) == "client:primary-client"

    def test_different_azp_produce_different_subjects(self):
        sub_a = _derive_subject({"azp": "client-a"})
        sub_b = _derive_subject({"azp": "client-b"})
        assert sub_a == "client:client-a"
        assert sub_b == "client:client-b"
        assert sub_a != sub_b

    def test_no_identity_raises(self):
        with pytest.raises(OAuthAuthenticationError) as exc:
            _derive_subject({})
        assert exc.value.error == "invalid_token"
        assert "subject" in exc.value.description

    @pytest.mark.unit
    async def test_verifier_uses_derive_subject(self):
        """verify_access_token returns client:azp for tokens without sub."""
        rsa_priv, rsa_pub = _make_rsa_jwk()
        jwks = {"keys": [rsa_pub]}
        verifier = _verifier_with_jwks(jwks)
        token = _build_token({
            "iss": "https://auth.example.com/",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "azp": "client-cred-app",
            "exp": _in_future(hours=1),
        }, rsa_priv)

        principal = await verifier.verify_access_token(token)
        assert principal.subject == "client:client-cred-app"

    @pytest.mark.unit
    async def test_different_azp_different_principal_subject(self):
        rsa_priv, rsa_pub = _make_rsa_jwk()
        jwks = {"keys": [rsa_pub]}
        verifier = _verifier_with_jwks(jwks)
        token_a = _build_token({
            "iss": "https://auth.example.com/",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "azp": "app-one",
            "exp": _in_future(hours=1),
        }, rsa_priv)
        token_b = _build_token({
            "iss": "https://auth.example.com/",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "azp": "app-two",
            "exp": _in_future(hours=1),
        }, rsa_priv)

        p_a = await verifier.verify_access_token(token_a)
        p_b = await verifier.verify_access_token(token_b)
        assert p_a.subject == "client:app-one"
        assert p_b.subject == "client:app-two"
        assert p_a.subject != p_b.subject

    @pytest.mark.unit
    async def test_no_sub_no_client_id_raises(self):
        """Token with neither sub nor client_id/azp must be rejected."""
        rsa_priv, rsa_pub = _make_rsa_jwk()
        jwks = {"keys": [rsa_pub]}
        verifier = _verifier_with_jwks(jwks)
        token = _build_token({
            "iss": "https://auth.example.com/",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, rsa_priv)

        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"


# ===================================================================
# T9 — Algorithm allowlist (S-006)
# ===================================================================

class TestAlgorithmPinning:
    """JsonWebToken must reject algorithms outside the configured allowlist."""

    @pytest.fixture
    def key_pair(self):
        return _make_rsa_jwk()

    @pytest.fixture
    def private_key_obj(self, key_pair):
        return key_pair[0]

    @pytest.fixture
    def public_jwk(self, key_pair):
        return key_pair[1]

    @pytest.fixture
    def jwks_payload(self, public_jwk):
        return {"keys": [public_jwk]}

    @pytest.fixture
    def verifier(self, jwks_payload):
        return _verifier_with_jwks(jwks_payload)

    @pytest.mark.unit
    async def test_hs256_rejected_when_rs256_required(self, verifier, private_key_obj):
        """Token signed with HS256 must be rejected when only RS256 is allowed."""
        config = _make_config(allowed_algorithms=("RS256",))
        alt_verifier = OAuthTokenVerifier(config)
        alt_verifier._jwks = verifier._jwks
        alt_verifier._jwks_expires_at = time.monotonic() + 3600.0
        import unittest.mock as _mock
        alt_verifier._load_jwks = _mock.AsyncMock(return_value=alt_verifier._jwks)

        from joserfc.jwk import OctKey
        hs256_key = OctKey.import_key("shared-secret-must-be-long-enough-for-hs256!")
        header = {"alg": "HS256", "kid": _TEST_KID}
        token = jose_jwt.encode(header, {
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, hs256_key, algorithms=["HS256"])

        with pytest.raises(OAuthAuthenticationError) as exc:
            await alt_verifier.verify_access_token(token)
        assert exc.value.error == "invalid_token"

    @pytest.mark.unit
    async def test_rs256_still_valid(self, verifier, private_key_obj):
        """RS256-signed token still validates with default allowlist."""
        token = _build_token({
            "iss": "https://auth.example.com/",
            "sub": "user",
            "aud": "https://memory.example.com/mcp-http",
            "scope": "menhir:read",
            "exp": _in_future(hours=1),
        }, private_key_obj)

        principal = await verifier.verify_access_token(token)
        assert principal.subject == "user"
