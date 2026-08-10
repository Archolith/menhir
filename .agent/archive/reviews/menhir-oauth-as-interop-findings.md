# Menhir OAuth AS — Phase 0 Interop Findings & Decisions

- **Plan:** `.agent/plans/menhir-oauth-as-phase0-interop.md` (gate for Phases 3-10 of
  `menhir-embedded-oauth-as-plan.md`)
- **Date:** 2026-07-09
- **Author:** Claude Code (Fable), research-only pass — no product source changed
- **D-A constraint (Charles, 2026-07-09):** all issuer options must remain supported
  (pluggable issuer = the spine), but there MUST be a **simple self-hosting option** with
  no external account.

---

## 1. MCP authorization spec — what the AS must be (verified against spec rev 2025-06-18)

Source: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization

| Requirement | Level | Menhir status |
|---|---|---|
| RS implements RFC 9728 protected-resource metadata | **MUST** | shipped (`api/oauth_metadata.py`), live-verified 2026-07-09 |
| RS sends `WWW-Authenticate` w/ `resource_metadata` on 401 | **MUST** | shipped, live-verified |
| RS validates audience binding (RFC 8707 §2) | **MUST** | shipped (`_resource_matches`), live-verified |
| AS implements OAuth 2.1 (auth-code + PKCE) | **MUST** | not built (this is the Wave-2 work) |
| AS provides RFC 8414 metadata (`/.well-known/oauth-authorization-server`) | **MUST** | not built (Phase 3) |
| AS + clients support RFC 7591 DCR | **SHOULD** | not built (Phase 4) |
| Clients send `resource` param in authorize + token requests | client MUST | AS must accept/echo it (Phase 6/7) |
| AS endpoints over HTTPS; redirect URIs localhost-or-HTTPS | MUST | deployment config (installer contract) |
| Access tokens short-lived; refresh rotation for public clients | SHOULD / MUST | Phase 7 token shape |

**AS location — the load-bearing answer:** the spec says the authorization server
*"may be hosted with the resource server or a separate entity"* and discovery goes
entirely through `authorization_servers` in the RS metadata. **Same-origin embedded AS is
explicitly permitted by the spec.** No same-origin restriction exists on either connector
(below).

**Direction of travel:** the current *draft* spec revision deprecates DCR in favor of
**CIMD (Client ID Metadata Documents)** — client_id is an HTTPS URL to a client-metadata
JSON the AS fetches. CIMD is *simpler* for an embedded AS than DCR (no registration
storage, no `/register` endpoint state); plan for both: DCR now (2025-06-18 interop),
CIMD accept-path alongside (cheap: fetch + cache the URL client_id).

## 2. Per-connector interop verdicts

### ChatGPT (Apps SDK connector) — **one-click against an embedded same-origin AS: YES**

Source: https://developers.openai.com/apps-sdk/build/auth

- Reads `authorization_servers` from RFC 9728 RS metadata: **confirmed** ("one or more
  issuer base URLs... ChatGPT will try each to find OAuth metadata").
- Registration: supports **CIMD (prioritized), DCR, and predefined clients** — "ChatGPT
  prioritizes CIMD when it is available, but the app creator can choose DCR when both are
  available."
- Same-origin AS: **no prohibition**; `authorization_servers` URLs are independently
  configurable and nothing requires a separate host.
- AS requirements: RFC 8414 metadata, `authorization_endpoint` + `token_endpoint`,
  **PKCE S256** (`code_challenge_methods_supported: ["S256"]`), token endpoint auth
  `none` (public/CIMD) or `private_key_jwt`, `resource` parameter support.
- Caveat to record honestly: OpenAI *"strongly recommend[s] that you use an existing
  established identity provider rather than implementing authentication from scratch."*
  Guidance, not a gate — the flow works against any conformant AS.

### Claude — claude.ai web / hosted surfaces — **one-click: YES (this updates the prior assumption)**

Source: https://claude.com/docs/connectors/building/authentication

- The prior working assumption ("Claude is paste-a-token by design and no rung changes
  that") is true **only for the API-side MCP connector** (`authorization_token` param,
  consumer runs the flow). **claude.ai custom connectors and Claude Code run the full
  OAuth 2.1 flow**: discovery via RFC 9728 → RFC 8414/OIDC metadata → **DCR (supported
  out of the box), CIMD (supported out of the box), or pre-registered credentials** →
  auth-code + **PKCE S256 on every authorization request**.
- Same-origin AS: **explicitly supported** — "an alternative is to serve the MCP endpoint
  and the authorization server behind a single custom domain that can route both
  `/.well-known/*` and your MCP path."
- Operational constraints: OAuth discovery/registration/token endpoints must respond
  within **10s** (30s for refresh); `/token` must accept
  `application/x-www-form-urlencoded`; RS metadata `resource` field must match the MCP
  server URL exactly; Claude egress from `160.79.104.0/21`.
- Directory-scale note: for high-traffic servers Anthropic recommends CIMD or
  pre-registered creds over DCR (DCR client-row explosion) — another point for building
  the CIMD accept-path.

### Claude — API-side MCP connector — **paste-a-token at every rung (unchanged)**

`authorization_token` is supplied by the API consumer; no rung improves this. Menhir's
**per-client token tier (shipped)** already serves this surface. No contradiction with
the embedded AS: an operator can also mint an AS token out-of-band and paste it.

## 3. Build-vs-buy (the ladder under the D-A constraint)

| Rung | Self-host? | Simple? | One-click (ChatGPT + claude.ai web) | Status |
|---|---|---|---|---|
| 1 — self-issue mint (client-token tier) | yes | **yes** | no (paste) | **already shipped** for the private tier |
| **2a — embedded AS in Menhir** | **yes** | **yes — no extra service, no external account** | **yes** | **RECOMMENDED — build** |
| 2b — BYO SaaS IdP | no (SaaS account) | config-only but external dependency | yes | stays supported, pluggable issuer (config already generic — verified `api/oauth.py` `build_oauth_config`) |
| 2c — bundled Keycloak/Hydra | yes | no — extra always-on service (Keycloak JVM ~0.4-0.6 GB; Hydra needs a login/consent app built anyway) | yes | fallback if 2a is judged too risky at Phase 10 audit; **realm-import + anonymous-DCR provisioning mechanism NOT verified in this pass** (gap, deliberately deprioritized — 2c is not the recommended path) |

Why 2a wins under the constraint:
- It is the only rung that is simultaneously **self-hosted, no-external-account, and
  no-second-service**. 2c is self-hosted but not simple; 2b is simple but not self-hosted.
- Both target connectors are verified to complete the flow against a same-origin AS.
- The blast surface is bounded: **public clients only, S256 only, auth-code only** — no
  client secrets, no password grant, no federation, no social login. Menhir's login is
  its own single-operator credential (design decision for Phase 6/8).
- The RS half is already built, hardened (S-001..S-009), and now live-verified including
  JWKS rotation — the embedded AS plugs into an audited verifier rather than a greenfield.
- OpenAI/industry "don't DIY auth" guidance is acknowledged; it argues for 2b, which the
  D-A constraint explicitly declines as the *only* path. Mitigation: narrow profile +
  mandatory Phase 10 security audit (same audit machinery that produced the S-series).

## 4. Library decision (Phase 0 question 3)

**Decision: hand-roll the narrow AS profile on `joserfc` + existing SQLite patterns. Do
not adopt Authlib's AS framework. Do not adopt FastMCP's `OAuthProvider`.**

Evidence:
- **Authlib AS framework: not viable on ASGI.** Authlib's `AuthorizationServer`
  integrations are Flask/Django (sync); the maintainer's position is that async provider
  support waits for Authlib v2.0 ("impossible to implement FastAPI OAuth providers
  because FastAPI is async and Authlib is not ready for async providers"). Menhir's auth
  is a pure-ASGI middleware on FastAPI — a sync AS framework is a structural mismatch.
  Plus S-009 already commits to migrating *off* `authlib.jose`; adopting a second Authlib
  surface while exiting the first is the wrong direction.
- **FastMCP `OAuthProvider`: exists in the shipped dep (fastmcp 3.2.4) but wrong fit.**
  It provides authorization/token/discovery endpoints, but (a) docs warn it "should be
  used only when you have specific requirements that external providers cannot meet and
  the expertise to implement OAuth securely" and requires implementer-supplied user/client
  stores and policy anyway; (b) no CIMD support; (c) there is an unresolved advisory
  against FastMCP's OAuth *proxy* (GHSA-5h2m-4q8j-pqpj — proxy ignores `resource` and
  mis-scopes tokens), which lowers confidence in adopting its auth stack wholesale;
  (d) Menhir deliberately owns protected-HTTP auth in `BearerAuthMiddleware` *outside*
  the FastAPI/FastMCP apps — wiring FastMCP's auth in would create a second auth owner.
- **`joserfc` hand-roll: aligned on every axis.** joserfc is Authlib's own successor for
  JOSE (S-009 lands on it anyway → one JOSE lib for sign *and* verify); the AS surface
  Menhir needs is 4 endpoints + 2 stores, and Phases 1/2 (signing key, client store) are
  already authored against menhir's own SQLite conventions. The "multi-month
  spec-compliant AS" warning applies to the general case; the narrow profile (auth-code +
  PKCE S256 + DCR/CIMD + refresh rotation, public clients only) is exactly what Phases
  3-10 scope, with Phase 10 as the audit gate.

### 4a. JOSE library trust hedge — provider seam (added 2026-07-09, post-migration)

**Concern (Charles):** `joserfc`, though maintained by Authlib's author (`lepture`) and its
official JOSE successor, is newer and less battle-tested than the most ubiquitous option —
uncomfortable for a library sitting directly in the token-verification path.

**Resolution: keep `joserfc`, but confine it behind a one-file provider seam** so the choice
is cheaply reversible and the security-critical JOSE surface is a single auditable module.
- Seam: `src/menhir/api/jose_provider.py` (`parse_jwks`, `jwks_has_kid`, `verify_jwt`,
  `generate_signing_key`, `serialize_key`, `load_key`, `sign_jwt`; provider-neutral
  `JoseError`). Verifier (`api/oauth.py`) and signing-key code (`api/oauth_keys.py`) import
  no JOSE library directly. Commits `9ee2b30` (S-009 migration) + `d1a7f8c` (seam).
- **Sanctioned drop-in alternative: PyJWT** (`pyjwt` + `cryptography` + `PyJWKClient`) — the
  most widely deployed Python JWT library; covers verify (now) and sign (Phase 7). Swapping
  is a single new provider implementation with no changes to the verifier, signing-key code,
  or their tests (only the test *token-minting* helpers, which still use joserfc, would move).
- Deferred (not blocking): pull hard supply-chain data (downloads, CVE/advisory history,
  release cadence) for joserfc vs PyJWT to confirm the choice. The seam means this can be
  decided later at near-zero switching cost. Runtime trust in the underlying lib is unchanged
  by the seam — it reduces switching cost and audit surface, it is not itself a security
  control (Phase 10 audits the actual crypto path).

## 5. Token shape (decision, binds Phase 7)

- `iss` = Menhir's own public origin (`MENHIR_PUBLIC_BASE_URL`), `aud` = the configured
  resource (`{base}/mcp-http` — matches RS `_resource_matches`), `sub` = operator/user
  identity, `client_id` + `client_name` = from the registered-client record (provenance),
  `scope` = space-joined `menhir:read|write|admin`, `exp` ≈ 1h access tokens, refresh
  tokens rotated per OAuth 2.1; `kid` from the Phase 1 signing key; **RS256** (matches
  the pinned verifier allowlist default).
- Self-wiring (Phase 9): when the embedded AS is enabled, `MENHIR_OAUTH_ISSUER`/
  `MENHIR_OAUTH_JWKS_URI` default to self — the verified RS path then consumes the AS's
  own JWKS with zero config. The live mock-IdP pass (2026-07-09 reaudit addendum) already
  proved the verifier against exactly this token shape.

## 6. AS state directory (Phase 0 task 5)

`oauth_as_db_path()` **already exists** (`src/menhir/infrastructure/paths.py:60`) — it
returns a **directory** (`workspace_root()/.agent`, override `MENHIR_OAUTH_AS_DIR`) and
already hosts `client_tokens.db`. Variation from the plan spec (which imagined a db-file
path + `MENHIR_OAUTH_AS_DB`): keep the shipped dir-shaped convention; AS stores land as
files inside it (`oauth_clients.db`, `oauth_codes.db` or one `menhir_oauth_as.db`).
Phase 2/5 child plans bind to the dir convention.

## 7. Installer contract (recorded)

First-run wizard asks *"local, or a server others connect to?"* → tier:
- **local** → loopback, no OAuth, no IdP;
- **private server** → client-token tier (shipped);
- **public server** → embedded AS on (2a) *or* operator supplies an external issuer URL
  (2b/2c — same Menhir config, different issuer). Operator must supply the HTTPS domain;
  installer templates everything else. SaaS limit recorded: an installer cannot create an
  Auth0/Clerk account — 2b stays reduced-steps, never zero-step.

---

## RECOMMENDED RUNG: **2a — embedded AS in Menhir** (GO)

Justification: it is the only rung satisfying the D-A constraint (simple + self-hosted +
no external account) while delivering verified one-click for **both** ChatGPT and
claude.ai web custom connectors; the spec and both connectors explicitly tolerate a
same-origin AS; the resource-server half is already hardened and live-verified; and the
build is bounded to a narrow, auditable profile on the library (joserfc) the codebase is
migrating to anyway. Rung 1 remains shipped for private/API-connector surfaces; 2b/2c
remain available as pluggable-issuer alternatives at zero code cost.

**Routing per the Phase 0 plan:** Rung 2 ⇒ author the Wave-2 child plans (Phases 3-10)
against these locked decisions: **library = joserfc; registration = DCR + CIMD
accept-path; profile = auth-code + PKCE S256, public clients, refresh rotation; token
shape = §5; state dir = §6.**

## Gaps / unverified (recorded, not asserted)

- 2c Keycloak realm-import + anonymous-DCR provisioning mechanism: not verified
  (deprioritized — not the recommended rung).
- Claude API connector non-paste paths via full MCP clients / MCP Inspector: not
  separately exercised; docs reviewed cover hosted surfaces + Claude Code.
- No live connector flow has been run yet against any Menhir AS (that is Phase 9's E2E,
  by design).
- CIMD deprecation-of-DCR status is from the *draft* spec revision; 2025-06-18 (current
  stable) still has DCR as SHOULD. Build both, prefer CIMD.

## Sources

- MCP authorization spec (2025-06-18): https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- OpenAI Apps SDK auth: https://developers.openai.com/apps-sdk/build/auth
- Claude connector authentication: https://claude.com/docs/connectors/building/authentication
- FastMCP authentication docs: https://gofastmcp.com/servers/auth/authentication
- Authlib async-provider status: https://github.com/authlib/authlib/pull/278 (+ docs)
- FastMCP OAuth proxy advisory: GHSA-5h2m-4q8j-pqpj <!-- gitleaks:allow — public advisory ID -->
