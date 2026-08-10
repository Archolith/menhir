# Menhir OAuth / Auth — Session Handoff (self-contained)

**Written 2026-07-09 for a fresh session with no prior context.** Read this top to bottom
before touching auth. Everything below is on `Archolith/menhir` branch `main`, pushed.

Menhir is a long-term graph-memory MCP server (Python, FastAPI + a pure-ASGI auth
middleware, Neo4j backend). "Auth" here protects the REST (`/api/*`) and MCP (`/mcp`,
`/mcp/*`, `/mcp-http`) surfaces.

---

## 1. Mental model — auth is a deployment-selected tier

Menhir stays an OAuth **resource server** (it validates tokens; it does not issue OAuth
login flows). How much auth a deployment uses scales with its exposure. Four tiers coexist
in one codebase; config selects which is active:

| Deployment | Auth tier | State |
|------------|-----------|-------|
| Local, stdio | none (client trusts its subprocess) | exists |
| Local, loopback HTTP | loopback guard + cooperative per-client labels | **DONE** |
| Private server, known clients | per-client opaque tokens (enforced identity) | **DONE** |
| Public server, one-click login | OAuth via a pluggable issuer / embedded AS | **NOT built** (gated on a decision) |

Master design doc: `.agent/plans/menhir-embedded-oauth-as-plan.md` (deployment-tiered spine
+ the "one-click" option ladder: 2a embedded AS / 2b SaaS IdP / 2c bundled Keycloak).

---

## 2. Key files

- `src/menhir/api/auth.py` — `BearerAuthMiddleware` (pure ASGI). Owns all protected-route
  auth. Contains: OAuth JWT path (`_call_with_oauth`), per-client token path
  (`_call_with_client_token` incl. the admin gate), loopback no-auth provenance branch,
  static-key path, and `_send_oauth_error` / `_send_auth_error`.
- `src/menhir/api/oauth.py` — OAuth resource-server: `OAuthConfig`, `build_oauth_config`,
  `OAuthTokenVerifier` (JWKS cache + `kid`-gated refresh), `OAuthAuthenticationError`,
  scope->tier mapping, preflight diagnostics. **Imports no JOSE library directly** — all
  JWT/JWKS work goes through `jose_provider` (see below).
- `src/menhir/api/jose_provider.py` — **library-neutral JOSE seam** (S-009 landed here).
  The ONLY module that imports the concrete crypto library (currently `joserfc`). Exposes
  `parse_jwks`, `jwks_has_kid`, `verify_jwt`, `generate_signing_key`, `serialize_key`,
  `load_key`, `sign_jwt`, and a provider-neutral `JoseError`. Swapping the library (e.g. to
  PyJWT) = one new provider impl, no changes to the verifier/keys/tests. Contract tests:
  `tests/test_jose_provider.py`.
- `src/menhir/api/oauth_keys.py` — embedded-AS local RSA signing key (Phase 1): persisted
  private JWK (`oauth_as_db_path()/oauth_signing_key.json`, 0o600, stable thumbprint `kid`),
  `public_jwks()`, `get_signing_key()` singleton. Goes through `jose_provider`.
- `src/menhir/api/oauth_client_store.py` — embedded-AS registered-client store (Phase 2):
  `OAuthClientStore` (`register`/`get`/`all`/`verify_secret`) in shared `menhir_oauth_as.db`;
  secrets stored as sha256, verified constant-time; `get_client_store()` singleton.
- `src/menhir/api/oauth_metadata.py` — `/.well-known/oauth-protected-resource` discovery
  (unauthenticated) + `/.well-known/jwks.json` (Phase 1, serves `public_jwks(get_signing_key())`).
- `src/menhir/api/client_token_store.py` — SQLite hashed token registry (`ClientTokenStore`:
  `mint`/`resolve`/`revoke`/`get`/`all`/`has_active`) + `get_client_token_store()` singleton.
- `src/menhir/api/routes.py` — REST endpoints incl. `/api/admin/clients` (mint/list/revoke),
  `_require_tier`.
- `src/menhir/mcp/tools/ops/{mint,revoke,list}_client.py` — operator-tier MCP tools.
- `src/menhir/config/settings.py` — `validate_no_auth_bind_safety`, all `MENHIR_OAUTH_*`,
  `MENHIR_CLIENT_TOKENS_ENABLED`, `MENHIR_OPERATOR_KEY`, etc.
- `src/menhir/api/server.py` — `create_app` wiring (middleware order, CORS, store build).
- `src/menhir/operator_diagnostics.py` — offline auth-mode diagnostics.
- `docs/runbooks/client-token-tier.md` — operator runbook for the token tier.

Env flags: `MENHIR_OAUTH_ENABLED`, `MENHIR_OAUTH_ISSUER`, `MENHIR_OAUTH_JWKS_URI`,
`MENHIR_PUBLIC_BASE_URL`, `MENHIR_AUTHORIZATION_SERVERS`, `MENHIR_OAUTH_ALLOWED_ALGORITHMS`
(default `RS256`); `MENHIR_CLIENT_TOKENS_ENABLED`; `MENHIR_OPERATOR_KEY`/`_AGENT_KEY`/
`_READONLY_KEY`; `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH`; `MENHIR_OAUTH_AS_DIR` (token db dir).

---

## 3. What is DONE (all on `main`, pushed)

### 3a. OAuth resource-server hardening (S-001..S-009)
Findings from `.agent/reviews/menhir-oauth-security-audit-results.md`, remediated and then
independently re-audited (Fable) in `.agent/reviews/menhir-oauth-e2e-reaudit-results.md`
(no new vulnerabilities; 9/9 fixed). Commits `804dd15`, `4f6e2ca`, `50ef61a`, `66901dc`
(+ earlier `1ca13a2` "make oauth own protected http auth").
Fixed: OAuth-aware bind guard; `kid`-gated + rate-limited JWKS refresh (DoS fix); stable
`client:<id>` subject for missing `sub`; `x-yawn-client-name` ignored in OAuth mode; pinned
RS256 alg allowlist; closed wildcard CORS default; issuer/exp/audience validation.

### 3a-bis. JOSE library: S-009 migration + provider seam (2026-07-09)
Commits `9ee2b30` (migrate `authlib.jose` -> `joserfc`; S-009 closed; authlib removed from
`pyproject.toml`) and `d1a7f8c` (isolate the library behind `api/jose_provider.py`).
**Decision context (Charles, 2026-07-09):** Phase 0 chose `joserfc` (Authlib's own successor,
by the same author `lepture`); Charles raised a supply-chain trust concern about it being
newer/less battle-tested than the most ubiquitous option. Resolution: **stay on joserfc but
put it behind a one-file seam** so the choice is reversible cheaply — not a code-wide bet.
- `jose_provider.py` is the only module importing a JOSE library. Verifier + signing-key
  code call the neutral interface and catch `JoseError`.
- **Vetted drop-in alternative if the trust question ever tips: PyJWT** (`pyjwt` +
  `cryptography` + `PyJWKClient`) — the most widely deployed Python JWT lib; covers both
  verify (now) and sign (Phase 7). Swap = write one `PyJwtProvider`, no verifier/keys changes;
  test helpers (which still mint tokens with joserfc) are the only test-side touch.
- Full OAuth suite 200 passed / 1 skipped; `tests/test_jose_provider.py` adds 8 contract tests.

### 3d. Embedded-AS build — Phases 1-5 done, 6-10 remaining (2026-07-09)
Non-interactive AS pieces are built and behind `MENHIR_OAUTH_AS_ENABLED` (off until Phase 9):
Phase 1 signing keys (`df8366b`), Phase 2 client store (`e5df4f9`), Phase 3 AS metadata
(`d5864bb`), Phase 5 auth-code store (`fb6875d`), Phase 4 DCR `/oauth/register` (`170b0e8`).
**Remaining (the interactive, security-critical core): Phase 6 `/authorize`+consent, 7
`/token`, 8 consent-session, 9 resource self-wiring+E2E, 10 audit.**
**To continue, read the self-contained build handoff:**
`.agent/plans/menhir-oauth-as-build-handoff.md`. Master status table:
`.agent/plans/menhir-embedded-oauth-as-plan.md`.

### 3b. Loopback multi-client provenance (cooperative labels)
Commit `25dba9a`. In loopback no-auth mode, self-declared `x-yawn-client-name` /
`?client_name=` are bound for telemetry provenance without changing access. Cooperative,
not enforced. Plan: `.agent/plans/menhir-loopback-multiclient-provenance.md`.

### 3c. Per-client token tier (enforced, tamper-proof) — the private-server tier
Commits `a9fe29a`, `35e90f8`, `749da06`, `fa0c764`, `563f353`, `cc36809`, `a90e774`,
`67c4aba`. Enabled by `MENHIR_CLIENT_TOKENS_ENABLED=1`. Each bearer token resolves via the
hashed registry to a registered `client_id`/`client_name`/`tier`, bound with
`trust_identity_headers=False` so a caller cannot relabel itself (tamper-proof). Admin gate
for `/api/admin/*`: operator key OR operator-tier token; loopback-no-token may ONLY mint,
and only while no active token exists (trust on first use); loopback-admin trusted only when
the server is loopback-bound. REST mint/list/revoke + MCP `mint_client`/`revoke_client`/
`list_clients`. Plan (marked IMPLEMENTED): `.agent/plans/menhir-per-client-token-tier.md`.
Runbook: `docs/runbooks/client-token-tier.md`.

**Nothing blocks use today** for local and private-server deployments.

---

## 4. What is OPEN (verified 2026-07-09)

### Resource-server polish (small; from the Fable re-audit)
- **N-003 (Low, recommended):** IdP outage surfaces as HTTP `401` instead of `503`.
  `OAuthAuthenticationError.__init__` defaults `status_code=401` (`oauth.py` ~L136);
  `_send_oauth_error` passes `exc.status_code` (`auth.py` ~L468). Fix: map the
  `server_error` OAuth error to `503` in `_send_oauth_error`. ~5-line change + a test.
- **N-002 (Low, defer):** CORS preflight is blocked on protected routes. `CORSMiddleware`
  lives inside the FastAPI stack while `BearerAuthMiddleware` wraps it outside and has no
  `OPTIONS` exemption, so a browser preflight (no `Authorization`) 401s before CORS answers.
  Fails closed; only matters if/when browser clients are added. Fix: exempt
  `scope["method"] == "OPTIONS"` requests carrying an `Origin` header in the middleware.
- **S-009 (maintenance) — DONE 2026-07-09.** Migrated `oauth.py` + `oauth_keys.py` off the
  deprecated `authlib.jose` to `joserfc`; `pyproject.toml` now pins `joserfc>=1.0,<2` (authlib
  removed). Behavior-preserving; full OAuth suite 200 passed / 1 skipped. AS endpoints (Phase
  7 `/token`) will sign on joserfc, consistent with the verifier.
- **N-004 (info):** WebSocket denial path emits HTTP frames. No WS routes exist today; only
  real work if WS routes are added.

### The real gap and the big decision
- **Live IdP validation — local pass DONE 2026-07-09** (see addendum in
  `.agent/reviews/menhir-oauth-e2e-reaudit-results.md`, workspace root copy). A second
  Menhir instance on `127.0.0.1:8091` with OAuth enabled was driven against a local mock
  IdP (real JWKS over HTTP, real RS256 tokens): 19/19 checks — tier mapping per scope,
  client-credentials identity, expired/wrong-iss/wrong-aud/HS256/malformed rejection,
  JWKS refresh gating + 30s rate limit observed live (4 total fetches), **real key
  rotation picked up via kid-gated refresh**, and MCP `initialize` over `/mcp-http` with
  a bearer token. Mock IdP harness: `%TEMP%\menhir-oauth-live\idp.py` (uncommitted).
  **Still open:** the same pass against a real SaaS IdP / connector (discovery, DCR,
  PKCE) — that part stays gated on the IdP/deployment decision below.
- **One-click login (public-server tier) — NOT started.** Gated behind **Phase 0**, a
  research-only interop + decision doc that was **never run**:
  `.agent/plans/menhir-oauth-as-phase0-interop.md`. It must produce a GO/NO-GO and a
  recommended "rung": 2a embedded AS in Menhir, 2b bring-your-own SaaS IdP, or 2c bundled
  self-hosted Keycloak (auto-provisioned by an installer). Key facts already established:
  ChatGPT's connector supports discovery + Dynamic Client Registration + PKCE (one-click
  achievable); **Claude's API MCP connector is paste-a-token by design and no rung changes
  that**. Rung 1 (self-issue tokens) is effectively already satisfied by the client-token
  tier (3c), just opaque instead of JWT.

**Both big items hinge on one unmade decision: the IdP / deployment direction** (local-only
vs a public server people connect to, and if public, which issuer). That decision is the
gate, not code.

---

## 5. Suggested next actions (pick based on intent)

1. **Just harden what's shipped:** do **N-003** (401->503) now; leave N-002/S-009 as tracked.
   Small, safe, no decision needed.
2. **Aim for one-click login:** run **Phase 0** (`menhir-oauth-as-phase0-interop.md`) — it is
   research-only, produces the rung recommendation, and needs the user's deployment intent.
   Do NOT start any embedded-AS / IdP code before Phase 0 lands.
3. **Trust the RS path in production:** pick an IdP and do the **live-IdP staging pass** (the
   real untested surface).

Ask the user for the IdP / deployment direction before doing 2 or 3.

---

## 6. Verify current state

From `projects/archolith/menhir/`:

```
python -m pytest -p no:cacheprovider -q \
  tests/test_oauth_jwt_verifier.py tests/test_api_auth.py \
  tests/test_loopback_auth_safety.py tests/test_operator_diagnostics.py \
  tests/test_oauth_metadata.py tests/test_client_token_tier_auth.py \
  tests/test_client_token_store.py tests/test_mcp_client_tools.py \
  tests/test_loopback_multiclient_provenance.py
```

On Windows, always pass `-p no:cacheprovider` (avoids `.pytest_cache` ACL issues). There is
one KNOWN-UNRELATED failure elsewhere in the suite: `test_scored_memory_has_required_fields`
was fixed (commit on main); if a different unrelated test fails, classify before assuming
it is auth-related.

---

## 7. Reference index

- Reviews: `.agent/reviews/menhir-oauth-security-audit-results.md`,
  `.agent/reviews/menhir-oauth-e2e-reaudit-results.md`
- Plans: `.agent/plans/menhir-embedded-oauth-as-plan.md` (master),
  `menhir-oauth-as-phase0-interop.md` (decision gate, NOT run),
  `menhir-oauth-as-phase1-signing-keys.md`, `menhir-oauth-as-phase2-client-store.md`,
  `menhir-loopback-multiclient-provenance.md` (done),
  `menhir-per-client-token-tier.md` (done)
- Runbook: `docs/runbooks/client-token-tier.md`, `docs/runbooks/oauth-remote-mcp-checklist.md`
- GitHub issues: `Archolith/menhir#38` (token expiry/rotation),
  `Archolith/menhir#39` (todo subsystem makeover — adjacent, not auth)
