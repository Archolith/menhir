"""Tests for the /oauth/authorize endpoint (Phase 6): admin-gated consent + PKCE."""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from archolith_oauth import AuthorizationCodeStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import auth_code_store, oauth_authorize, oauth_client_store
from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.client_policy import (
    ClientPolicy,
    ClientPolicyAuthority,
    OAuthClientRegistration,
)
from menhir.api.oauth_authorize import router as oauth_authorize_router
from menhir.api.oauth_client_store import OAuthClient, get_client_store, new_client_id

pytestmark = pytest.mark.unit

_ENABLED = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    operator_key="s3cret",
)
_DISABLED = SimpleNamespace(oauth_as_enabled=False)
_CB = "https://app.example.com/cb"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setenv("MENHIR_OAUTH_AS_CONSENT_SECRET", "test-consent-secret")
    # Hermeticity: an operator key leaking in from the repo .env (loaded into the
    # process) would make `operator_key=""` on the test settings fall through to
    # os.getenv("MENHIR_OPERATOR_KEY") (see api/oauth._get_setting), so the "no
    # admin secret configured" tests would resolve a real key. Clear it so the
    # settings value is authoritative.
    monkeypatch.delenv("MENHIR_OPERATOR_KEY", raising=False)
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)


def _pkce() -> tuple[str, str]:
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _register_client(
    *,
    redirect_uris=(_CB,),
    scopes=("menhir:read", "menhir:write", "menhir:admin"),
    client_name="Test App",
) -> str:
    client_id = new_client_id()
    get_client_store().register(
        OAuthClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=tuple(redirect_uris),
            scopes=tuple(scopes),
            client_secret_hash="",
            created_at=0.0,
            token_endpoint_auth_method="none",
        )
    )
    return client_id


def _policy(
    client_id: str,
    *,
    scopes: frozenset[str],
    protocol_scopes: frozenset[str] = frozenset(),
) -> ClientPolicyAuthority:
    return ClientPolicyAuthority(
        version=1,
        digest="test",
        clients={
            client_id: ClientPolicy(
                client_id=client_id,
                label="chatgpt-chat",
                scopes=scopes,
                maximum_tier="agent",
                namespace="",
                allowed_tools=frozenset({"recall_memories"}),
                denied_tools=frozenset({"add_memory"}),
                registration=(
                    OAuthClientRegistration(
                        client_name="Test App",
                        redirect_uris=(_CB,),
                        protocol_scopes=protocol_scopes,
                    )
                    if protocol_scopes
                    else None
                ),
            )
        },
    )


def _client(
    settings=_ENABLED,
    *,
    policy: ClientPolicyAuthority | None = None,
) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    if policy is not None:
        app.state.client_policy = policy
    app.include_router(oauth_authorize_router)
    return TestClient(app, follow_redirects=False)


def _extract_hidden(html_text: str) -> dict[str, str]:
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', html_text))


def _valid_get_params(client_id: str, *, challenge: str, state: str = "xyz-state", scope: str | None = None):
    params = {
        "client_id": client_id,
        "redirect_uri": _CB,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scope is not None:
        params["scope"] = scope
    return params


def _location_query(resp) -> dict[str, list[str]]:
    return parse_qs(urlsplit(resp.headers["location"]).query)


# ---------------------------------------------------------------------------
# Flag gate
# ---------------------------------------------------------------------------


def test_disabled_returns_404_get_and_post():
    c = _client(_DISABLED)
    assert c.get("/oauth/authorize").status_code == 404
    assert c.post("/oauth/authorize", data={}).status_code == 404


def test_consent_capacity_refusal_is_bounded_429(tmp_path, monkeypatch):
    client_id = _register_client(scopes=("menhir:read",))
    monkeypatch.setattr(
        auth_code_store,
        "_auth_code_store_singleton",
        AuthorizationCodeStore(
            tmp_path / "menhir_oauth_as.db",
            consent_global_limit=1,
            consent_per_client_limit=1,
        ),
    )
    _, challenge = _pkce()
    client = _client()

    first = client.get(
        "/oauth/authorize",
        params=_valid_get_params(
            client_id,
            challenge=challenge,
            state="first",
            scope="menhir:read",
        ),
    )
    second = client.get(
        "/oauth/authorize",
        params=_valid_get_params(
            client_id,
            challenge=challenge,
            state="second",
            scope="menhir:read",
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "5"


# ---------------------------------------------------------------------------
# GET validation
# ---------------------------------------------------------------------------


def test_get_unknown_client_id_returns_400_no_redirect():
    _, challenge = _pkce()
    resp = _client().get("/oauth/authorize", params=_valid_get_params("nonexistent", challenge=challenge))
    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_production_policy_rejects_unlisted_client_before_authorization():
    _, challenge = _pkce()
    cid = _register_client(scopes=("menhir:read", "menhir:write"))
    authority = _policy("different-client", scopes=frozenset({"menhir:read"}))

    resp = _client(policy=authority).get(
        "/oauth/authorize",
        params=_valid_get_params(
            cid,
            challenge=challenge,
            scope="menhir:read menhir:write",
        ),
    )

    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_production_policy_rejects_scope_narrowing():
    _, challenge = _pkce()
    cid = _register_client(scopes=("menhir:read", "menhir:write"))
    authority = _policy(
        cid,
        scopes=frozenset({"menhir:read", "menhir:write"}),
    )

    resp = _client(policy=authority).get(
        "/oauth/authorize",
        params=_valid_get_params(cid, challenge=challenge, scope="menhir:read"),
    )

    assert resp.status_code == 302
    assert _location_query(resp)["error"] == ["unauthorized_client"]


def test_production_policy_accepts_protocol_only_offline_access():
    _, challenge = _pkce()
    cid = _register_client(
        scopes=("menhir:read", "menhir:write", "offline_access")
    )
    authority = _policy(
        cid,
        scopes=frozenset({"menhir:read", "menhir:write"}),
        protocol_scopes=frozenset({"offline_access"}),
    )
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_as_refresh_tokens_enabled=True,
        operator_key="s3cret",
    )

    response = _client(settings, policy=authority).get(
        "/oauth/authorize",
        params=_valid_get_params(
            cid,
            challenge=challenge,
            scope="menhir:read menhir:write offline_access",
        ),
    )

    assert response.status_code == 200
    assert set(_extract_hidden(response.text)["scope"].split()) == {
        "menhir:read",
        "menhir:write",
        "offline_access",
    }


def test_get_redirect_uri_mismatch_returns_400():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    params["redirect_uri"] = "https://evil.example.com/cb"
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_get_bad_response_type_redirects_error():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    params["response_type"] = "token"
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["error"] == ["unsupported_response_type"]
    assert q["state"] == ["xyz-state"]


def test_get_missing_code_challenge_redirects_invalid_request():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    del params["code_challenge"]
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    assert _location_query(resp)["error"] == ["invalid_request"]


def test_get_plain_pkce_method_redirects_invalid_request():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    params["code_challenge_method"] = "plain"
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    assert _location_query(resp)["error"] == ["invalid_request"]


def test_get_scope_exceeds_grant_redirects_invalid_scope():
    _, challenge = _pkce()
    cid = _register_client(scopes=("menhir:read",))
    params = _valid_get_params(cid, challenge=challenge, scope="menhir:admin")
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    assert _location_query(resp)["error"] == ["invalid_scope"]


def test_persisted_client_cannot_request_scope_removed_from_current_policy():
    _, challenge = _pkce()
    cid = _register_client(scopes=("menhir:read", "menhir:admin", "offline_access"))
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_scopes_supported=("menhir:read",),
        oauth_write_scopes=(),
        oauth_admin_scopes=(),
        oauth_as_refresh_tokens_enabled=False,
        operator_key="s3cret",
    )
    c = _client(settings)

    stale = c.get(
        "/oauth/authorize",
        params=_valid_get_params(cid, challenge=challenge, scope="menhir:admin"),
    )
    assert stale.status_code == 302
    assert _location_query(stale)["error"] == ["invalid_scope"]

    current = c.get(
        "/oauth/authorize",
        params=_valid_get_params(cid, challenge=challenge),
    )
    assert current.status_code == 200
    assert _extract_hidden(current.text)["scope"] == "menhir:read"


def test_scope_removed_after_consent_get_is_rechecked_before_approval():
    _, challenge = _pkce()
    cid = _register_client(scopes=("menhir:read", "menhir:admin"))
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_scopes_supported=("menhir:read", "menhir:admin"),
        oauth_write_scopes=(),
        oauth_as_refresh_tokens_enabled=False,
        operator_key="s3cret",
    )
    c = _client(settings)
    form = _consent_form(c, cid, challenge=challenge)
    settings.oauth_scopes_supported = ("menhir:read",)
    settings.oauth_admin_scopes = ()
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"

    response = c.post("/oauth/authorize", data=form)

    assert response.status_code == 302
    assert _location_query(response)["error"] == ["invalid_scope"]


def test_get_valid_renders_consent_with_token():
    _, challenge = _pkce()
    cid = _register_client()
    resp = _client().get("/oauth/authorize", params=_valid_get_params(cid, challenge=challenge))
    assert resp.status_code == 200
    assert 'name="consent_token"' in resp.text
    assert "Test App" in resp.text


def test_get_escapes_client_name():
    _, challenge = _pkce()
    cid = _register_client(client_name="<script>x</script>")
    resp = _client().get("/oauth/authorize", params=_valid_get_params(cid, challenge=challenge))
    assert resp.status_code == 200
    assert "<script>x</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


# ---------------------------------------------------------------------------
# POST consent decision
# ---------------------------------------------------------------------------


def _consent_form(client: TestClient, cid: str, *, challenge: str, state: str = "xyz-state") -> dict[str, str]:
    resp = client.get("/oauth/authorize", params=_valid_get_params(cid, challenge=challenge, state=state))
    assert resp.status_code == 200
    return _extract_hidden(resp.text)


def test_post_approve_issues_code_and_redirects():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["state"] == ["xyz-state"]
    code = q["code"][0]
    record = get_auth_code_store().redeem(code=code, client_id=cid, redirect_uri=_CB)
    assert record is not None
    assert record.subject == "menhir-admin"
    assert record.code_challenge == challenge


def test_production_consent_group_uses_one_secret_for_distinct_clients():
    _, challenge = _pkce()
    scopes = frozenset({"menhir:read", "menhir:write"})
    cid_a = _register_client(scopes=tuple(scopes), client_name="Claude")
    cid_b = _register_client(scopes=tuple(scopes), client_name="Codex")

    def entry(client_id: str, label: str) -> ClientPolicy:
        return ClientPolicy(
            client_id=client_id,
            label=label,
            scopes=scopes,
            maximum_tier="agent",
            namespace="",
            allowed_tools=frozenset({"recall_memories"}),
            denied_tools=frozenset({"add_memory"}),
            consent_group="agent-smith",
        )

    authority = ClientPolicyAuthority(
        version=1,
        digest="test",
        clients={
            cid_a: entry(cid_a, "agent-smith-claude"),
            cid_b: entry(cid_b, "agent-smith-codex"),
        },
    )
    client = _client(policy=authority)
    first = client.get(
        "/oauth/authorize",
        params=_valid_get_params(
            cid_a, challenge=challenge, scope="menhir:read menhir:write"
        ),
    )
    form = _extract_hidden(first.text)
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    approved = client.post("/oauth/authorize", data=form)
    assert approved.status_code == 302
    session_cookie = re.search(
        r"menhir_as_session=([^;]+)", approved.headers["set-cookie"]
    ).group(1)
    verified = oauth_authorize._verify_session(session_cookie, _ENABLED)
    assert verified is not None
    assert set(verified[1]) == {cid_a, cid_b}

    second = client.get(
        "/oauth/authorize",
        params=_valid_get_params(
            cid_b, challenge=challenge, scope="menhir:read menhir:write"
        ),
        headers={"cookie": f"menhir_as_session={session_cookie}"},
    )
    assert second.status_code == 302
    assert "code" in _location_query(second)


def test_consent_approval_survives_authorization_store_recreation(monkeypatch):
    _, challenge = _pkce()
    cid = _register_client()
    first_process = _client()
    form = _consent_form(first_process, cid, challenge=challenge)
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"

    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None)
    restarted_process = _client()
    response = restarted_process.post("/oauth/authorize", data=form)

    assert response.status_code == 302
    assert "code" in _location_query(response)


def test_simultaneous_consent_approval_issues_exactly_one_code():
    _, challenge = _pkce()
    cid = _register_client()
    first_process = _client()
    second_process = _client()
    form = _consent_form(first_process, cid, challenge=challenge)
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"

    def approve(client: TestClient):
        return client.post("/oauth/authorize", data=form)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(approve, (first_process, second_process)))

    assert sorted(response.status_code for response in responses) == [302, 400]
    success = next(response for response in responses if response.status_code == 302)
    assert "code" in _location_query(success)


def test_post_wrong_secret_401_no_code():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    form["admin_secret"] = "wrong"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 401
    assert "location" not in resp.headers
    # No code was issued: nothing to redeem.
    assert get_auth_code_store().purge_expired() == 0


def test_post_empty_operator_key_403():
    _, challenge = _pkce()
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        operator_key="",
    )
    cid = _register_client()
    c = _client(settings)
    form = _consent_form(c, cid, challenge=challenge)
    form["admin_secret"] = "anything"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 403


def test_post_deny_redirects_access_denied():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    form["admin_secret"] = "s3cret"
    form["decision"] = "deny"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["error"] == ["access_denied"]
    assert q["state"] == ["xyz-state"]


def test_post_bad_consent_token_400():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    form["consent_token"] = "garbage.sig"
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 400


def test_post_tampered_field_400():
    _, challenge = _pkce()
    second = "https://app.example.com/cb2"
    cid = _register_client(redirect_uris=(_CB, second))
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    # Swap redirect_uri to the client's OTHER registered URI (still valid) while
    # keeping the consent_token signed over the first — must be rejected.
    form["redirect_uri"] = second
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 400


def test_post_expired_consent_token_400(monkeypatch):
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    monkeypatch.setenv("MENHIR_OAUTH_AS_CONSENT_TTL_S", "-1")
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# RFC 9207: every trusted redirect carries exact iss + preserved state
# ---------------------------------------------------------------------------

_EXACT_ISS = "https://memory.example.com"


def test_unsupported_response_type_redirect_has_exact_iss_and_state():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    params["response_type"] = "token"
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["iss"] == [_EXACT_ISS]
    assert q["state"] == ["xyz-state"]
    assert q["error"] == ["unsupported_response_type"]


def test_pkce_error_redirect_has_exact_iss_and_state():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    params["code_challenge_method"] = "plain"
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["iss"] == [_EXACT_ISS]
    assert q["state"] == ["xyz-state"]
    assert q["error"] == ["invalid_request"]


def test_invalid_scope_redirect_has_exact_iss_and_state():
    _, challenge = _pkce()
    cid = _register_client(scopes=("menhir:read",))
    params = _valid_get_params(cid, challenge=challenge, scope="menhir:admin")
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["iss"] == [_EXACT_ISS]
    assert q["state"] == ["xyz-state"]
    assert q["error"] == ["invalid_scope"]


def test_deny_redirect_has_exact_iss_and_state():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    form["admin_secret"] = "s3cret"
    form["decision"] = "deny"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["iss"] == [_EXACT_ISS]
    assert q["state"] == ["xyz-state"]
    assert q["error"] == ["access_denied"]


def test_success_redirect_has_exact_iss_and_state():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    form = _consent_form(c, cid, challenge=challenge)
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    resp = c.post("/oauth/authorize", data=form)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["iss"] == [_EXACT_ISS]
    assert q["state"] == ["xyz-state"]
    assert q["code"][0]


def test_untrusted_target_still_direct_400_no_iss():
    _, challenge = _pkce()
    cid = _register_client()
    params = _valid_get_params(cid, challenge=challenge)
    params["redirect_uri"] = "https://evil.example.com/cb"
    resp = _client().get("/oauth/authorize", params=params)
    assert resp.status_code == 400
    assert "location" not in resp.headers
