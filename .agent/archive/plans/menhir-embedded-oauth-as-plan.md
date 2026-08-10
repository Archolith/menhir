# Menhir Deployment-Tiered Auth — Master Plan

> **Scope reframe (supersedes the original "embedded AS" framing; filename kept for history).**
> Menhir is distributed software: some users run it **locally** (stdio/loopback, one machine),
> others on a **VPS** (network-reachable, possibly cloud clients). Auth need scales with
> exposure, so auth is a **deployment-selected tier**, not one global choice. The embedded AS
> is demoted to one (de-prioritized) option; the spine is **Menhir stays a resource server
> with a pluggable issuer**, and the install/config picks the tier.

**Goal:** One codebase whose auth scales with how it is deployed — zero-config for local
installs, token-per-client for private servers, and optional one-click OAuth for public
servers — while every tier surfaces **per-client provenance** uniformly.

## Deployment-tiered auth — the flexible spine (read first)

| Deployment | Exposure | Auth tier | Build state |
|---|---|---|---|
| Local, stdio | none | **none** (client trusts its subprocess) | exists |
| Local, loopback HTTP | 127.0.0.1 | **loopback guard** + **per-client provenance labels** + optional local token | guard built; provenance = `menhir-loopback-multiclient-provenance.md` (near-term) |
| VPS, few known clients | trusted net | **token per client** (static keys / Rung 1 mint) | mostly built |
| VPS, public one-click | internet | **OAuth via pluggable issuer** | RS half built (hardened) |

Principles:
- **Menhir stays a pure OAuth *resource server*** — it validates tokens against a configured
  `MENHIR_OAUTH_ISSUER`/`JWKS_URI`. It does **not** bake an authorization server into core.
- **The one-click issuer is pluggable and optional:** bring-your-own hosted IdP (2b) *or* a
  bundled Keycloak enabled via a docker-compose profile (2c). Same Menhir config; only the
  issuer URL differs. Local users leave OAuth off entirely.
- **Embedded AS (2a) is de-prioritized** precisely because it burdens every install (incl.
  local stdio users) with an authorization server + attack surface they never use.
- **Footprint is confined to the tier that opted in:** only VPS-public users run Keycloak,
  and they have the box for it; local users carry none of it.
- **Provenance is tier-independent:** static-key, mint-token, and OAuth callers all surface
  `client_id`/`client_name` into session telemetry the same way.

The ladder below is the menu of one-click options for the **public-server tier only** —
chosen per deployment, never globally.

**Provenance track (independent of the AS ladder) — the stated priority:**
- **Now:** [`menhir-loopback-multiclient-provenance.md`](menhir-loopback-multiclient-provenance.md)
  — cooperative per-client labels in loopback no-auth mode. Near-term; what most local users hit.
- **Future (end goal — tamper-proof):**
  [`menhir-per-client-token-tier.md`](menhir-per-client-token-tier.md) — enforced per-client
  identity via a hashed opaque-token registry + admin mint; headers cannot override the
  registered identity. Also fills the ladder's **Rung 1** slot. Same provenance surface as the
  cooperative tier, so the near-term work is a stepping stone, not throwaway. Not scheduled yet.

**Why the OAuth pieces exist at all:** The MCP spec allows the authorization server and
resource server to be the same entity, and Menhir already ships the hardened resource-server
half (JWT validation, scope->tier, `client:<id>` identity, JWKS caching — see
`.agent/reviews/menhir-oauth-e2e-reaudit-results.md`). The remaining question is only how the
*public-server* tier obtains its issuer.

**Project:** `projects/archolith/menhir/`. All child plans live in `.agent/plans/` with the
`menhir-oauth-as-*` prefix.

---

## Implementation ladder (decide the rung in Phase 0 — do not assume Rung 2)

> **D-A constraint (Charles, 2026-07-09):** all issuer options must remain supported
> (pluggable issuer = the spine), but the ladder MUST include a **simple self-hosting
> option** with no external account. This removes "2b-only" from the outcome space: Phase 0
> must recommend which self-host rung (1 / 2a / 2c) is the simple one, with 2b remaining a
> config-only alternative for operators who want it.

The "one-click" win is **asymmetric across connectors** (confirmed by ChatGPT's own read of
the current docs), so this is not embedded-AS-or-nothing. There is a ladder; higher rungs
cost more and buy less than first assumed:

| Rung | What Menhir builds | ChatGPT | Claude (API MCP connector) | Cost | Clone-and-run by others |
|------|--------------------|---------|-----------------------------|------|--------------------------|
| 0 | nothing new — static bearer keys (today) | paste token | paste token (`authorization_token`) | 0 | preserved |
| 1 | **admin-minted client tokens** (self-issue, `client:<id>` provenance) | paste token | paste token | ~1 day | preserved |
| 2a | **embedded AS** (DCR + auth-code + PKCE + consent) | **inline one-click, most seamless** | paste token *(unchanged)* | large + own audit | **preserved** (no external accounts) |
| 2b | **hosted SaaS IdP + DCR** (Auth0/Clerk/WorkOS) | **inline one-click** (redirect) | paste token *(unchanged)* | none (config only) | **broken** (each operator needs own SaaS account) |
| 2c | **bundled self-hosted IdP, auto-provisioned by installer** (Keycloak/Hydra) | **inline one-click** (redirect) | paste token *(unchanged)* | already docker-compose (Neo4j) so bundling is incremental; real cost is **RAM footprint** (2nd JVM if Keycloak) + installer; **no auth code to own/audit** | **preserved** (installer stands up + configs IdP locally, no external accounts) |

Key facts driving the choice:
- **ChatGPT** supports CIMD, Dynamic Client Registration, predefined clients, and PKCE — so
  both Rung 2a (embedded AS) and Rung 2b (hosted IdP) give ChatGPT a genuine inline one-click.
- **Claude's API-side MCP connector** takes an `authorization_token` and expects the API
  consumer to handle the OAuth flow/refresh — so it is **paste-a-token at every rung, under
  every IdP choice**; no rung improves Claude's API connector. (Full MCP clients / MCP
  Inspector may differ — Phase 0 verifies.)
- **"Inline / no extra user steps"** is *best* served by 2a: the embedded AS consent is
  Menhir's own page — no third-party brand, no separate/social account. A hosted IdP (2b) is
  inline-*ish* (a branded redirect) but introduces a third party in the identity path.
- **OpenAI's own guidance** recommends an established IdP over DIY auth — a point *for* 2b.
- **Provenance holds either way**, and 2b is richer: DCR `client_id` = which app + login
  `sub` = which human.

**An installer changes the calculus.** A setup script / first-run web wizard can fully
auto-provision a *self-hosted* IdP (2c) — `docker compose up` + baked realm import + write
issuer/JWKS into config — restoring clone-and-run without any external account. It cannot
conjure a *SaaS* account (2b), so 2b stays "reduced steps, not zero." This makes **2c the
strongest fit for "established IdP + inline + automated setup + no auth code we own/audit"**
(honors OpenAI's don't-DIY-auth guidance while preserving clone-and-run). Its costs: an extra
service to run/patch (Keycloak = heavier but includes login/consent UI; Hydra = lighter but
you build that UI), the installer is real work, and production still needs an HTTPS domain
input for the IdP.

**Decision shape now:**
- **2a (embedded AS):** clone-and-run + most-inline UX, but you write & forever re-audit
  security-critical auth code.
- **2c (bundled self-hosted IdP + installer):** clone-and-run + one-click + **no auth code
  you own**, at the cost of a second service + an installer build.
- **2b (SaaS IdP):** zero build, but breaks clone-and-run (account per operator).
- **Rung 1** remains the cheap floor (Claude + all bearer clients + provenance, ~a day).
- **Claude API connector is paste-a-token under all of the above** — never a differentiator.

Phase 0 recommends the rung, and for 2b/2c assesses installer/provisioning feasibility.

## How this plan is staged (read this first)

This is authored in two waves **on purpose**:

- **Wave 1 (authored now, executable today):** the Phase 0 decision gate and the two
  foundational pieces that do not depend on its outcome (signing keys, client store).
- **Wave 2 (authored after Phase 0):** Phases 3-10. Their exact tasks depend on decisions
  Phase 0 resolves (which JOSE/AS library, connector interop constraints, token shape). It
  would be misleading to hand a Sonnet executor exact tasks for endpoints whose contract is
  not yet fixed. Each is scoped below; its child file is written once Phase 0 lands.

**Do not start Phases 3-10 until Phase 0 is complete and its child plans are authored.**
Phases 1 and 2 MAY run in parallel with Phase 0 (they are interop- and library-independent).

---

## Dependency graph

```
Phase 0 (interop + library decision) ──GATE──> Phases 3,4,6,7 detail
Phase 1 (signing keys) ─┐                (parallel with P0)
Phase 2 (client store) ─┤                (parallel with P0)
                        │
Phase 3 (AS metadata) ──── needs P1
Phase 4 (DCR /register) ── needs P2
Phase 5 (auth-code store) ─ standalone
Phase 6 (/authorize+consent) needs P2,P5
Phase 7 (/token) ───────── needs P1,P5
Phase 8 (consent session) ─ needs P6
Phase 9 (resource wiring+E2E) needs P1,P7 + existing verifier
Phase 10 (security audit) ─ needs P3-P9
```

---

## Phases

| # | Child plan | Scope (one line) | Depends on | Status |
|---|-----------|------------------|-----------|--------|
| 0 | `menhir-oauth-as-phase0-interop.md` | Verify connector interop + choose JOSE/AS library; **decision gate** | — | **DONE 2026-07-09** — findings: `../reviews/menhir-oauth-as-interop-findings.md`; **GO, Rung 2a; library = joserfc; DCR + CIMD accept-path** |
| 1 | `menhir-oauth-as-phase1-signing-keys.md` | Local RSA signing key bootstrap + `/.well-known/jwks.json` | — | **DONE 2026-07-09** — `src/menhir/api/oauth_keys.py` + JWKS route; 7 tests pass |
| 2 | `menhir-oauth-as-phase2-client-store.md` | Persistent registered-client store (SQLite) | — | **DONE 2026-07-09** — `src/menhir/api/oauth_client_store.py` (shared `menhir_oauth_as.db`); 8 tests pass |
| 3 | `menhir-oauth-as-phase3-as-metadata.md` | `.well-known/oauth-authorization-server` (RFC 8414) | P1 | **DONE 2026-07-09** — `api/oauth_as_metadata.py`, `MENHIR_OAUTH_AS_ENABLED` gate; 7 tests |
| 4 | `menhir-oauth-as-phase4-dcr.md` | `/register` Dynamic Client Registration (RFC 7591) [+ CIMD deferred to 4b] | P2 | **DONE 2026-07-09** — `api/oauth_as_register.py`; 10 tests. CIMD accept-path deferred (SSRF surface) |
| 5 | `menhir-oauth-as-phase5-authcode-store.md` | Short-lived auth-code store (code, PKCE, expiry) | — | **DONE 2026-07-09** — `api/auth_code_store.py` (shared `menhir_oauth_as.db`, DB-enforced single-use, verify_pkce); 10 tests |
| 6 | `menhir-oauth-as-phase6-authorize.md` | `/authorize` + admin-gated consent page + PKCE | P2,P5 | **DONE 2026-07-09** — `api/oauth_authorize.py`; open-redirect-safe error dichotomy, PKCE S256, operator-secret consent + HMAC integrity token; 16 tests |
| 7 | `menhir-oauth-as-phase7-token.md` | `/token` code->signed JWT carrying client identity | P1,P5 | **DONE 2026-07-09** — `api/oauth_token.py`; single-use redeem + PKCE verify -> RS256 JWT (findings §5 shape), verified through the RS seam; 9 tests |
| 8 | `menhir-oauth-as-phase8-consent-session.md` | Consent session cookie (true one-click after first) | P6 | **DONE 2026-07-09** — signed HttpOnly SameSite=Lax cookie in `api/oauth_authorize.py`; one-click GET after validation, operator-key-gated; 7 tests |
| 9 | *(no separate child-plan file — see note below)* | Point resource verifier at self; full E2E flow test | P1,P7 | **DONE 2026-07-09** — `build_oauth_config` self-wires issuer/JWKS/authz-server to `MENHIR_PUBLIC_BASE_URL` and enables the verifier when `MENHIR_OAUTH_AS_ENABLED`; explicit `MENHIR_OAUTH_*` overrides win, no-base fails closed. **AS flag ON.** E2E `tests/test_oauth_as_e2e.py` (register->authorize->token->protected `/mcp` accepted at operator/readonly tier) + `tests/test_oauth_as_self_wiring.py`; suite 278 passed / 1 skipped |
| 10 | *(no separate child-plan file — see note below)* | Security audit of the new AS attack surface | P3-P9 | **DONE 2026-07-09** — `../reviews/menhir-oauth-as-security-audit-results.md` (confidence 86/100). Classic OAuth surface holds (exact redirect_uri, PKCE S256, single-use codes, open-redirect dichotomy, escaped consent, RS256/alg-allowlist crypto). **1 High (AS-001):** Phase 8 one-click session is client-agnostic + `SameSite=Lax` → CSRF to operator-tier token; must fix before public exposure. 3 Medium (open-DCR growth, per-process HMAC secret under multi-worker, admin-secret brute-force window), 3 Low. Acceptance #5 (no unremediated High) NOT yet met → remediation plan pending |

> **Filename note (2026-08-08, curator audit).** Phases 0-8 were each authored as their own
> `menhir-oauth-as-phase<N>-*.md` child-plan file; the `menhir-oauth-as-phase9-resource-wiring.md`
> and `menhir-oauth-as-phase10-security-audit.md` filenames named in earlier drafts of this row
> were never created as separate files (confirmed via `git log --all`) — Phase 9 landed directly
> as `build_oauth_config` self-wiring (evidence cited above) and Phase 10 landed as the review at
> `../reviews/menhir-oauth-as-security-audit-results.md` plus
> `.agent/plans/menhir-oauth-as-security-remediation-plan.md`. Both phases are genuinely DONE;
> only the two child-plan filenames were dangling references.

---

## Acceptance criteria (whole feature)

1. A fresh clone with no external IdP: start Menhir, obtain the printed bootstrap admin
   secret, and a client completing the auth-code+PKCE flow receives a valid JWT that the
   **existing** resource-server verifier accepts.
2. The token carries the connecting client's `client_id`/`client_name` from Dynamic Client
   Registration; that identity appears in session telemetry (`_derive_subject` path).
3. **ChatGPT** connector completes discovery -> DCR -> authorize -> token with no manual
   client pre-registration (Phase 0 confirmed interop, incl. same-origin AS). **Phase 0
   correction:** **claude.ai web custom connectors and Claude Code also run the full OAuth
   flow (DCR/CIMD out of the box)** — treat claude.ai-web one-click as an acceptance
   target too. Only **Claude's API-side MCP connector** is paste-a-token by design and is
   NOT expected to gain one-click from this work.
4. Scopes map to tiers exactly as today (`menhir:read/write/admin` -> readonly/agent/operator).
5. No regression in the existing resource-server suite; new AS code has its own tests; the
   Phase 10 audit finds no unremediated High/Critical.
   **MET (2026-07-09):** the Phase 10 audit's sole High, **AS-001** (client-agnostic
   one-click session + `SameSite=Lax` → CSRF to operator tier), is remediated in commit
   `032b46d` (client-scoped one-click + `SameSite=Strict`). All Medium/Low findings
   (AS-002..AS-007) are also remediated — see
   `.agent/plans/menhir-oauth-as-security-remediation-plan.md` and the OAuth-AS CHANGELOG
   entries. Full OAuth suite 299 passed / 1 skipped.

## Cross-cutting guardrails (every child plan)

- **Do not modify existing tests** to make them pass (file-split import updates excepted).
  If a test fails because behavior legitimately changed, stop and report.
- **Meat-first:** output the hard logic (crypto, flow endpoints) complete and compilable
  before scaffolding.
- New AS code is **security-sensitive** — Phase 10 audits it; do not skip validation
  (redirect_uri exact-match, PKCE required, single-use codes, consent CSRF, admin-secret
  handling).
- One child plan per session. Commit per phase with a `feat(oauth-as):` prefix, explicit
  file paths, and a CHANGELOG entry.

## Out of scope

- Refresh-token rotation (Phase 2+ follow-up; start with short-lived access tokens).
- Multi-tenant / multi-human-user login (this stays single-admin-approves; the "user" is
  whoever holds the admin secret).
- External IdP federation (the door stays open — issuer/JWKS are configurable — but not built).

## Open decisions carried by Phase 0

**ALL RESOLVED 2026-07-09** — see `../reviews/menhir-oauth-as-interop-findings.md`:

- **JOSE/AS library → hand-rolled narrow profile on `joserfc`.** Authlib's AS framework is
  not ASGI/async-ready (provider support deferred to Authlib 2.0) and S-009 exits
  `authlib.jose` anyway; FastMCP's `OAuthProvider` rejected (second auth owner beside
  `BearerAuthMiddleware`, no CIMD, proxy advisory GHSA-5h2m-4q8j-pqpj).
- **Which rung → 2a (embedded AS), GO** under the D-A constraint (simple self-host, no
  external account). Rung 1 is effectively shipped (client-token tier) for private/API
  surfaces; 2b/2c stay pluggable-issuer alternatives.
- **Connector interop → confirmed for BOTH connectors.** ChatGPT: reads RFC 9728
  `authorization_servers`, does DCR, prefers CIMD, tolerates same-origin AS. **Claude
  correction:** claude.ai web custom connectors + Claude Code run the full OAuth flow
  (DCR + CIMD out of the box, same-origin explicitly supported, PKCE S256 always); only
  the API-side connector is paste-a-token. 10s timeout on discovery/registration/token
  endpoints; 30s on refresh.
