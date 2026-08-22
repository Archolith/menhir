# Menhir Security Posture

**Purpose:** a standing reference for Menhir's authentication/authorization architecture,
the controls in place, the deployment posture, and what is validated vs. still pending.
Point-in-time finding history lives in the workspace review
`.agent/reviews/menhir-oauth-security-consolidated.md` (archolith project); this document
is the durable "how it is built and why" reference.

**Last reviewed:** 2026-07-10, menhir `main` @ `5fd4f8b`.

---

## 1. Scope and threat model

Menhir is a long-term graph-memory MCP server. Its security-sensitive surface is the
**remote HTTP API + MCP endpoints** (`/api/*`, `/mcp`, `/mcp/*`, `/mcp-http`) and the
**embedded OAuth authorization server** (`/oauth/*`, `/.well-known/*`). The local **stdio
MCP** transport is a trusted in-process path (see §6).

Primary threats considered:

- Unauthenticated access to memory data or destructive operations.
- Token forgery / algorithm confusion against the OAuth resource server.
- Identity spoofing / attribution poisoning via caller-supplied headers.
- Credential-free privilege escalation during bootstrap windows.
- DoS / resource amplification (JWKS refetch, rate-limit table growth, open DCR).
- Reverse-proxy trust confusion (peer-address vs. real-client collapse).

Not in scope here: OS/network hardening of the VPS, TLS termination (delegated to the
front proxy), and supply-chain scanning (tracked separately; see §11).

## 2. Deployment topologies

Menhir binds **`127.0.0.1` by default**. Two supported production shapes:

1. **Local loopback** — single-user/agent host, no network exposure. No-auth is permitted
   *only* on a loopback bind (§7).
2. **Proxied VPS** — uvicorn bound to `127.0.0.1` behind a **same-host** TLS-terminating
   reverse proxy (nginx/caddy) that provides the `https` public base URL the OAuth AS
   needs. This topology is the reason for the reverse-proxy controls in §8.

A direct non-loopback bind (`0.0.0.0`) is allowed only when authenticated (OAuth, static
keys, or client tokens) or with an explicit insecure override (§7).

**Two adjacent local surfaces are unencrypted/unauthenticated by design and are kept safe by the
loopback posture, not by in-app controls** (operator checklist:
[`docs/runbooks/local-operator-hardening.md`](runbooks/local-operator-hardening.md)):

- **Neo4j transport (Finding A).** The driver connects with no TLS trust config
  (`infrastructure/neo4j.py`); the default `bolt://localhost:7687` is plaintext. Loopback = fine
  (traffic never leaves the host). A non-loopback `NEO4J_URI` **must** use an encrypted scheme
  (`bolt+s://` / `neo4j+s://`) or a tunnel — a bare remote `bolt://` would send credentials and
  memory content in the clear. No code change for MVP; local posture is safe.
- **Graph explorer (Finding B).** The explorer is mounted into the main app at `/explorer` on the
  API port (default `127.0.0.1:8090`), sharing the runtime Neo4j pool. `BearerAuthMiddleware` now
  gates `/explorer` and `/explorer/candidates/*` exactly like `/api/*`: on a loopback bind in
  `AuthMode.NONE` it is open (unauthenticated localhost inspection, as before); on a non-loopback
  bind it requires the same bearer/OAuth/client-token credential as the API. Only `/explorer/static/*`
  (assets) is exempt. The standalone `menhir-explorer` process and port `8787` were removed. Keep the
  server on loopback for open access; a non-loopback bind is already auth-protected.
- **Memory-content privacy (Finding B, cont.).** `MENHIR_PRIVACY_REDACT=true` hides memory
  *contents* (content, summary, previews, names, graph node labels) in the explorer UI and the
  console dashboard's log tail — a display-time mask for screen-sharing/demos; log files and the
  Neo4j graph are unchanged. A per-browser `menhir_reveal` cookie (or the header toggle) can
  un-redact, but only on a loopback bind — privacy cannot be defeated by a cookie on a remote bind.
  The console dashboard toggles the same state live with the `p` key.

  **The two surfaces are not equally strong, and this setting governs both (CF-96).** The explorer
  UI is masked **field-exactly**: every value under a memory-bearing field is hidden regardless of
  its shape. The console log tail is masked **heuristically** — it operates on already-rendered log
  strings and can only mask *quoted* spans, so memory content interpolated through an unquoted
  `%s`, or a quoted value shorter than 12 characters, passes through in the clear. Treat the log
  tail as a demo aid that removes most content, **not** as a guarantee that none is displayed. If
  you are screen-sharing where a leak would matter, raise the log level rather than relying on the
  mask.

## 3. Authentication modes and precedence

**Single source of truth.** The effective auth mode is resolved in exactly one place —
`resolve_auth_mode(settings)` in `api/auth_mode.py`, whose precedence is defined by the
one function `auth_mode_from(...)`. The ASGI middleware, the bind-safety guard
(`assert_bind_safe`), the CLI `serve` command, and operator diagnostics all resolve through
it, so the mode that is *enforced* can never drift from the mode that is *guarded* or
*reported* (the drift class behind findings S-001/S-005). `create_app` resolves the mode
once and hands it to the middleware.

`BearerAuthMiddleware` (`api/auth.py`) is a pure ASGI middleware wrapping the app. It
dispatches on the one resolved `AuthMode`, selected by this precedence:

1. **OAuth resource server** (`AuthMode.OAUTH`; `MENHIR_OAUTH_ENABLED=true`) — owns all protected HTTP auth.
2. **Per-client token tier** (`AuthMode.CLIENT_TOKEN`; `MENHIR_CLIENT_TOKENS_ENABLED=1`).
3. **Static bearer keys** (`AuthMode.STATIC`; `MENHIR_OPERATOR_KEY` / `MENHIR_AGENT_KEY` /
   `MENHIR_READONLY_KEY`; `MENHIR_API_KEY` is a backwards-compat alias for the operator key).
4. **Loopback no-auth** (`AuthMode.NONE`; no keys configured; only reachable on a loopback bind).

There is **no mixed-mode fallthrough**: exactly one mode is active and fully owns auth, so
e.g. a static key cannot bypass the OAuth path, and `?api_key=` is rejected under OAuth.
Exempt paths (no auth): `/api/health`, `/api/ready`. CORS preflights (`OPTIONS` + `Origin`)
are also exempt so the inner CORS middleware can answer them (N-002). The OAuth discovery
endpoint `/.well-known/oauth-protected-resource` is intentionally unauthenticated (required
for MCP client discovery) and emits no secrets.

**Tier model** (coarse RBAC): `readonly` < `agent` < `operator`. Tiers are enforced on REST
via `_require_tier(...)` per route (under `APIRouter(prefix="/api")`) and on MCP HTTP
dispatch via an explicit, test-guarded operation→tier map.

## 4. OAuth resource server (`api/oauth.py`)

Menhir validates access tokens issued by an external IdP (or its own embedded AS, §5); it
is a **resource server**, not (for the RS path) an authorization server.

- **Signature verification** against the IdP JWKS. JOSE operations are confined to
  `api/jose_provider.py` on **joserfc** (there are zero `authlib` imports in the codebase).
- **Algorithm allowlist** pinned — default `["RS256"]`, configurable via
  `MENHIR_OAUTH_ALLOWED_ALGORITHMS`. A token whose header `alg` is outside the set is
  rejected before key resolution (no alg-confusion / `alg=none`).
- **Claim validation:** exact `iss` match, `exp` required, `aud`/`resource` binding to the
  configured audience, bounded clock skew (60s).
- **Scope→tier mapping:** `menhir:read`→readonly, `menhir:write`→agent, `menhir:admin`→operator.
  A token with no Menhir scope gets `403 insufficient_scope`.
- **Subject derivation:** human tokens use `sub`; client-credentials tokens fall back to
  `client:<client_id|azp>`; a token with none is rejected (no collapse to a shared synthetic
  identity).
- **JWKS cache** (300s TTL) with **kid-gated, rate-limited forced refresh**: a decode
  failure only triggers an outbound JWKS fetch when the token's `kid` is absent from the
  cached set, and at most once per 30s. Malformed/expired/wrong-audience tokens never touch
  the network. Bounds the IdP-amplification / DoS surface.
- **Fail-closed config:** empty issuer / JWKS URI / audiences raise `server_error` before
  any decode. Operational failures (JWKS fetch failure, misconfig) surface as **503 with no
  Bearer challenge** — clients retry rather than entering a re-auth loop. Genuine credential
  failures surface as `401` with an RFC 6750 Bearer challenge.
- **JWKS is not SSRF-able:** `jwks_uri` is operator config, never taken from the token.
- **Identity is never the raw token:** session/user IDs derive from verified claims + path +
  UA + verified `client_id` via SHA-256; the raw access token is never persisted or used as
  a key.

## 5. Embedded authorization server (`api/oauth_as_*.py`, `oauth_authorize.py`, `oauth_token.py`)

Enabled by `MENHIR_OAUTH_AS_ENABLED`. Public clients only (PKCE, no client secrets).

- **Grant:** `authorization_code` only (advertised and implemented; no unimplemented
  `refresh_token` advertised).
- **PKCE** enforced at authorize/token time; authorization codes are single-use (atomic
  redeem).
- **Dynamic Client Registration (RFC 7591)** is unauthenticated by spec but bounded:
  per-IP rate limit (default 20/600s), a hard client cap (default 1000,
  `MENHIR_OAUTH_AS_MAX_CLIENTS`), and a **stale-client reaper** that deletes
  never-exchanged registrations older than `MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S`
  (default 24h) before enforcing the cap. A nearing-cap warning logs at 80%.
- **Consent CSRF protection:** the consent decision is bound to a client-scoped signed
  session with `SameSite=Strict`; a silent one-click approval only happens when the client
  is already in the approved set, otherwise the consent page renders. Consent tokens are
  single-use (`jti` burned before the secret is evaluated); a per-IP approve throttle
  (default 10/300s) caps brute force.
- **Consent/session HMAC secret:** when `MENHIR_OAUTH_AS_CONSENT_SECRET` is unset it is
  derived from the on-disk signing-key bytes (domain-separated), stable across workers on
  one host. Multi-host horizontal scaling requires an explicit shared secret (preflight
  warns).
- **Signing key:** RSA-2048 via joserfc, RFC 7638 thumbprint `kid`, persisted with `0o600`
  file mode on Linux (the production target; Windows use is dev-only).
- **Redirect URIs:** `https` only, or `http` to a loopback host.

## 6. Per-client token tier (`api/client_token_store.py`)

An enforced-provenance alternative to shared static keys.

- **Storage:** tokens are `secrets.token_urlsafe(32)`, stored **sha256-hashed only**, and
  shown exactly once at mint. `list_clients` never emits token material. Lookups are hash
  equality (no timing oracle; a preimage is required).
- **Tamper-proof identity:** a resolved token binds its *registered*
  `client_id`/`client_name`/`tier` with `trust_identity_headers=False`, so a caller cannot
  relabel itself via `x-menhir-*` headers.
- **Admin gate:** every admin action (`/api/admin/clients*`) requires the operator key or an
  operator-tier token **except** the one bootstrap capability below, so all admin actions
  carry provenance. REST routes also add `_require_tier("operator")` as defense-in-depth.
- **TOFU bootstrap:** while the store has **no active token**, an unauthenticated **loopback**
  caller may mint the first token (POST `/api/admin/clients` only). It is refused if a
  reverse-proxy forwarding header is present (§8), on a non-loopback bind, or once any active
  token exists. The mint is **atomic** (`INSERT ... WHERE NOT EXISTS active`), so two
  concurrent bootstraps cannot both mint (the loser gets `409`). Revoking the last active
  token deliberately re-opens bootstrap.
- **stdio MCP trust:** the local stdio process binds **operator tier explicitly**
  (`bind_stdio_local_trust`) — its real security boundary is filesystem access to the SQLite
  stores, not a request tier. This is a deliberate, documented local-trust decision, not an
  oversight.

## 7. No-auth bind safety guard (`config/settings.py`, `operator_diagnostics.py`)

`validate_no_auth_bind_safety` refuses to start a **no-key, non-loopback** bind unless OAuth
or client tokens are enabled, or `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1` is explicitly set
(unsafe; lab networks only). The guard is OAuth- and client-token-aware, so a correctly
configured OAuth-only remote deployment starts normally. `menhir diagnostics` mirrors this
as offline preflight checks (bind host, auth mode, no-auth remote guard, admin-key status,
AS consent secret, AS rate-limit proxy awareness) and never prints secret values.

## 8. Reverse-proxy posture

Behind a same-host proxy the peer socket address is always the proxy (e.g. `127.0.0.1`),
which would otherwise (a) satisfy the loopback bootstrap check for internet callers and
(b) collapse per-IP rate limits into one global window. Controls:

- **Bootstrap forwarding-header guard:** a bootstrap mint carrying `X-Forwarded-For`,
  `X-Real-IP`, or `Forwarded` is refused. A genuine local `curl` never sets these; a proxy
  always appends one the external caller cannot strip. Configure the proxy to add a
  forwarding header (nginx `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`).
- **Trusted-proxy rate-limit keys:** with `MENHIR_TRUSTED_PROXY=1`, AS rate limits resolve
  the real client from the **last** `X-Forwarded-For` hop (the IP the local proxy saw);
  without it, XFF is not trusted (default = peer address). Diagnostics warn when the AS runs
  on a loopback bind without trusted-proxy resolution.
- **Operational discipline (runbook):** on a proxied deployment, pre-mint the first operator
  token (or set `MENHIR_OPERATOR_KEY`) **before** wiring the proxy, and never revoke the last
  active token while the proxy is attached. See `docs/runbooks/client-token-tier.md`.

## 9. Rate limiting and resource bounds

- **Fixed-window per-key limiter** (`api/oauth_rate_limit.py`), in-process, threadsafe.
  Used for DCR and consent-approve. The tracked-key set is **hard-capped**
  (`_MAX_TRACKED_KEYS`, FIFO eviction) so a distinct-key flood cannot grow memory or cost
  unbounded within a window.
- **JWKS forced-refresh budget:** ≤1 forced fetch per 30s window regardless of attack rate.
- **DCR client cap + reaper** (§5).
- Rate-limit / consent-jti / consent-secret state is **per-process** — correct at the
  current `workers=1` uvicorn config; a multi-worker move requires shared state (documented
  residual).

## 10. Data and secret handling

- Tokens (client + OAuth) never stored in plaintext; client tokens sha256-only, OAuth tokens
  never persisted at all.
- Static-key comparison is constant-time (`hmac.compare_digest`).
- `WWW-Authenticate` challenge components are server-controlled literals/config; caller
  strings never reach the header (quote/backslash escaped).
- Preflight/diagnostics redact URL userinfo and never emit key values.
- `?api_key=` on MCP paths is stripped before the request reaches downstream handlers.

## 11. Validated vs. pending

**Validated (code + tests):** the auth surface has unit + live coverage. A prior live
local-mock-IdP pass exercised 19/19 token cases (scope/tier enforcement, key rotation
pickup, JWKS-refresh gating, spoofed-header rejection, alg pinning) over real HTTP.

**Live SaaS-IdP (Auth0):** resource-server mode is verified against a real Auth0 tenant.
`scripts/smoke/auth0_live_smoke.py` mints a genuine Auth0 client-credentials RS256 token
and drives a throwaway Menhir pointed at Auth0's real issuer/JWKS/audience: a valid token
passes auth (real JWKS fetch, issuer + audience check, `menhir:*` -> tier mapping), no token
-> 401 + Bearer challenge, and a bogus token against the reachable JWKS -> 401 (token error,
not a 503 outage) — 4/4. The smoke self-skips when `AUTH0_*` env is absent, so it is safe in
`run_all`. Setup + the trailing-space-audience gotcha are documented in
`docs/runbooks/auth0-live-oauth.md`. Still pending: the interactive authorization-code +
PKCE browser flow with a real client (client-credentials M2M is what is exercised).

**Live shape testing (any change).** `scripts/dev/test_server.py` launches a throwaway
server in a selectable auth *shape* (`no-auth`, `static`, `client-token`, `oauth`,
`oauth-as`) on a safe port with a fully isolated env (no repo `.env`, no real backend — it
uses the `MENHIR_STARTUP_SCOPE=auth-only` scope so no Neo4j is needed). `scripts/smoke/
auth_shapes_smoke.py` drives every shape and asserts the guards end-to-end (CT-001
forwarding-header bootstrap guard, client-token bootstrap open/closed, N-002 CORS preflight,
N-003 503-on-outage, OAuth challenge + `?api_key=` rejection, AS discovery) — 16/16. Run it
after any auth change; it is the fast regression net for the shapes.

**Pending (require a real environment):**

- Interactive **authorization-code + PKCE** flow against a real SaaS IdP with a browser
  client — Phase 0 decision-gated. (Resource-server token validation against a real IdP is
  now covered by the Auth0 live smoke, §11 Validated; only the interactive user-login flow
  remains unexercised.)
- Live **proxied-deployment** exercise of the §8 guards (nginx/caddy in front of a loopback
  bind) — code-level guards are in and unit-tested; validate end-to-end before public
  exposure.
- **Dependency CVE scan** — `pip-audit` is not wired into CI. Installed at `6207fa3`:
  joserfc 1.6.4, httpx 0.28.1, fastapi 0.128.8, starlette 0.52.1, uvicorn 0.46.0,
  cryptography 46.0.4 (authlib 1.7.0 transitive only). Recommend adding `pip-audit` to CI.

## 12. Accepted residuals (no action)

- First kid-less junk token per 30s window buys one forced JWKS fetch (bounded ≤2/min).
- Unknown-`kid` spam can delay rotated-key pickup by ≤30s (300s TTL backstop).
- Per-process limiter/jti/consent state (moot at `workers=1`).
- `?api_key=` client token traverses proxy/access logs as a URL (mirrors the static-key
  trade-off; tracked for data-driven removal).
- No WebSocket routes exist; the middleware's WS deny path would need a native close if WS
  is ever added.

## 13. Security-relevant configuration reference

| Env var | Purpose |
|---|---|
| `MENHIR_OAUTH_ENABLED` | Enable OAuth resource-server mode |
| `MENHIR_OAUTH_ISSUER` / `MENHIR_OAUTH_JWKS_URI` / `MENHIR_OAUTH_AUDIENCE` | External IdP binding |
| `MENHIR_OAUTH_ALLOWED_ALGORITHMS` | JWT alg allowlist (default `RS256`) |
| `MENHIR_OAUTH_AS_ENABLED` | Enable the embedded authorization server |
| `MENHIR_PUBLIC_BASE_URL` | Public https base URL (AS issuer/resource) |
| `MENHIR_OAUTH_AS_MAX_CLIENTS` | DCR client cap (default 1000) |
| `MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S` | Reap never-exchanged clients older than this (default 86400) |
| `MENHIR_OAUTH_AS_REGISTER_RATE` / `_WINDOW_S` | DCR throttle (default 20 / 600s) |
| `MENHIR_OAUTH_AS_APPROVE_RATE` / `_WINDOW_S` | Consent-approve throttle (default 10 / 300s) |
| `MENHIR_OAUTH_AS_CONSENT_SECRET` | Explicit consent HMAC secret (required for multi-host) |
| `MENHIR_CLIENT_TOKENS_ENABLED` | Enable the per-client token tier |
| `MENHIR_OPERATOR_KEY` / `MENHIR_AGENT_KEY` / `MENHIR_READONLY_KEY` / `MENHIR_API_KEY` | Static bearer keys by tier |
| `MENHIR_TRUSTED_PROXY` | Trust the last `X-Forwarded-For` hop for AS rate-limit keys |
| `MENHIR_CORS_ORIGINS` | Explicit CORS allow-list (no wildcard default) |
| `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH` | Override the no-auth remote bind guard (unsafe) |
| `MENHIR_OAUTH_AS_DIR` | Directory for the AS/client-token SQLite stores + signing key |

## 14. Change history / provenance

- **2026-07-10 (M4 hardening):** documented the two loopback-guarded local surfaces in §2 —
  Neo4j plaintext bolt transport (Finding A) and the unauthenticated explorer (Finding B) — and
  added the operator checklist `docs/runbooks/local-operator-hardening.md`. Verified `/api/ready`,
  `/api/stats`, MCP stdio backend-client mode, and the active file-event hook against a full-scope
  local backend. No code change; the local posture is safe by loopback, and the encrypted-scheme /
  proxy requirements are the documented gates for any non-local move.
- **2026-07-10 (`4a16417`):** live SaaS-IdP verification — Menhir's OAuth resource-server
  mode is proven against a real Auth0 tenant (`scripts/smoke/auth0_live_smoke.py`, 4/4:
  valid RS256 token accepted, no-token challenge, bogus-token rejection vs live JWKS). The
  `oauth` launcher shape is parametrized for an external IdP; env-driven Auth0 helper scripts
  and `docs/runbooks/auth0-live-oauth.md` added. Closes the resource-server half of the
  "live SaaS-IdP interop" follow-up.
- **2026-07-10 (`0d512eb`):** single source of truth for the auth mode
  (`resolve_auth_mode` / `auth_mode_from`) — middleware, bind guard, CLI `serve`, and
  diagnostics all resolve through one place; fixed a latent S-001 recurrence in the CLI
  bind check. Added the shaped test-server launcher + live auth smoke (`e2defd0`).
- **2026-07-10 (`6207fa3`):** closed the last open findings — reverse-proxy bootstrap guard
  (CT-001), trusted-proxy rate-limit keys (RL-001), key-cap limiter (RL-002), 503-on-outage
  (N-003), CORS preflight exemption (N-002), explicit stdio operator tier (CT-002), atomic
  bootstrap mint (CT-003), DCR stale-client reaper + nearing-cap warn (AS-002 residual).
- Earlier remediations (resource-server S-001..S-009, embedded-AS AS-001..AS-007,
  re-audit N-001..N-004) and full finding history: see
  `.agent/reviews/menhir-oauth-security-consolidated.md` in the archolith workspace.
