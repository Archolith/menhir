# Per-Client Token Tier — Enforced (Tamper-Proof) Provenance

Parent: `menhir-embedded-oauth-as-plan.md`. **This is the stated end goal** — tamper-proof
per-client identity. It also fills the ladder's **Rung 1 (self-issue mint)** slot.

**Project:** `projects/archolith/menhir/`.

## Status: IMPLEMENTED (2026-07-09)

Storage, enforcement, admin gate, endpoints, config, and TOFU bootstrap are done and
tested. Commits: `a9fe29a` (storage), `35e90f8` (verification core), `749da06` (wiring),
`fa0c764` (loopback mint-only), `563f353` (TOFU bootstrap).

Behavior as built:
- `client_token_store` on `BearerAuthMiddleware` owns protected auth when enabled
  (`MENHIR_CLIENT_TOKENS_ENABLED=1`); each bearer token resolves to a registered
  `client_id`/`client_name`/`tier`, bound with `trust_identity_headers=False`
  (tamper-proof). Unknown/revoked/missing -> 401.
- Admin gate for `/api/admin/*`: operator key OR operator-tier minted token for any admin
  action; loopback-no-token may ONLY mint, and ONLY while `has_active()` is False (trust on
  first use). Loopback-admin trusted only when the server is loopback-bound.
- REST: `POST /api/admin/clients` (mint), `POST /api/admin/clients/{id}/revoke`.
- Store persists sha256 hashes only; raw token returned once.

### Remaining / follow-ups
- **CHANGELOG + operator docs** (enable flag, bootstrap flow, lost-token recovery =
  delete `client_tokens.db`, TOFU reset via revoke-last-token). NOT yet written.
- **MCP `mint_client` / `revoke_client` tools** (user request 2026-07-09): expose client
  management as MCP tools gated to **operator tier** via the MCP op->tier map. NOTE: an MCP
  call is always already-authenticated, so these are for an operator adding *more* clients
  post-bootstrap — they are NOT a bootstrap path (the REST loopback bootstrap remains the
  only first-token path). Well-scoped delegation candidate.
- Token expiry/rotation; `GET /api/admin/clients` list endpoint.

## Objective

Give each client its own bearer token whose identity is bound **server-side**, so a client
cannot lie about who it is. Identity comes from a token->identity registry, not a self-declared
header. Works for local and private-server deployments **without a JWT/JWKS/IdP** — opaque
tokens in a hashed registry are sufficient for enforced *local* identity (federation is the
OAuth ladder's job, not this).

Tamper-proof property: client B presenting a forged `x-yawn-client-name` header is ignored;
to act as client A it must present A's actual token (which it does not have).

## Design

- **Storage (SQLite, hashed).** Table `client_tokens(token_hash TEXT PRIMARY KEY, client_id
  TEXT, client_name TEXT, tier TEXT, created_at REAL, revoked INTEGER DEFAULT 0)`. Tokens are
  stored **only as sha256 hashes**; the raw token is shown once at mint time and never
  persisted. Follow the SQLite pattern in `services/scheduler_lease.py` /
  `infrastructure/pending_actions.py`; path via `oauth_as_db_path()` (from the AS Phase-1/2
  helper) — reuse the client-store module if it already exists.
- **Bootstrap admin.** Reuse `MENHIR_OPERATOR_KEY` as the mint credential if set; else generate
  a one-time admin token on first run with this tier enabled and print it once to stderr.
- **Mint endpoint.** `POST /api/admin/clients {client_name, tier?}` (operator/admin tier only)
  -> generate a random opaque token (`secrets.token_urlsafe(32)`), store its hash + a new
  stable `client_id` + `client_name` + tier, return the raw token **once**.
- **Revoke endpoint.** `POST /api/admin/clients/{client_id}/revoke` -> set `revoked=1`.
- **Verification (middleware branch).** In `BearerAuthMiddleware`, add a tier selected when the
  token registry is enabled/non-empty (config flag, e.g. `MENHIR_CLIENT_TOKENS_ENABLED`):
  resolve the presented bearer token by hashing + constant-time registry lookup
  (`hmac.compare_digest` semantics; index by hash). On match and not revoked -> bind the
  **registered** `client_id`/`client_name` + tier via `bind_request_session` /
  `bind_request_tier`, with `trust_identity_headers=False` so headers cannot override. On no
  match / revoked -> 401. This mirrors the OAuth branch's structure, minus JWKS/JWT.

## Relationship to existing tiers

- **Generalizes static keys:** today there are 3 fixed tier-keys with no per-client identity;
  this is N per-client identity-tokens. Decide whether it coexists with or supersedes static
  keys (recommend coexist: static keys remain the simplest option, client-tokens the
  enforced-provenance option).
- **Same provenance surface** as cooperative loopback and OAuth — only the identity source
  differs (verified registry lookup).
- **Fills Rung 1 mint:** the same mint endpoint is the ladder's self-issue option for the
  private-server tier.

## Tests (new file `tests/test_per_client_token_tier.py`)

- `test_mint_then_use_roundtrip`: mint a token, present it, request binds the registered
  identity; token returned once.
- `test_header_cannot_override_registered_identity` (the tamper-proof test): present a valid
  token for `alpha` **plus** header `x-yawn-client-name: beta` -> bound identity is `alpha`.
- `test_unknown_token_rejected`: random/unknown bearer -> 401.
- `test_revoked_token_rejected`: mint, revoke, present -> 401.
- `test_two_clients_distinct`: two minted tokens bind two distinct client identities.
- `test_tokens_stored_hashed_only`: raw token bytes absent from the DB file; verification works
  via hash.
- `test_mint_requires_admin`: mint without operator/admin credential -> 401/403.

## Acceptance criteria

- A minted token binds a fixed server-side identity that self-declared headers cannot override.
- Tokens persist only as hashes; verification is constant-time; revocation takes effect.
- Existing static-key, loopback, and OAuth paths unchanged; the new tier is config-gated and
  off by default. No existing test modified.

## Out of scope

- JWT/JWKS/OAuth federation (that is the AS ladder / pluggable issuer).
- Token expiry/rotation and refresh (v2 of this tier).
- Per-client fine-grained scopes beyond the existing tier mapping.

## Verify

`pytest -p no:cacheprovider -q tests/test_per_client_token_tier.py tests/test_api_auth.py`
Commit: `feat(auth): enforced per-client token tier (tamper-proof provenance)`
