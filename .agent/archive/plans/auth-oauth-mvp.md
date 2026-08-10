# Plan: MVP auth — OAuth 2.1 + surface hardening

**Status: PARTLY IMPLEMENTED 2026-07-09 (Phase 0 DONE; code-side resource-server plumbing is landed.
The remaining blockers are D-A, the authorization-server choice, and the live connector proof.)**
Context: the 2026-07-04 auth review found a well-built three-tier static-bearer middleware whose
tier model is enforced on MCP tools only (tracker Q4), non-constant-time token compares (Q5), and
client-asserted identity headers. Good bones for a single-user LAN service; not an MVP posture.
This plan takes menhir from static shared keys to standards-based auth without breaking the
existing local workflow.

## Where auth stands today (verified, not assumed)
- `api/auth.py`: pure-ASGI bearer middleware; operator/agent/readonly tiers; fail-closed when any
  key configured, wide-open when none (local dev mode); query-param key path for header-less
  connectors, sanitized after use, tool-allowlisted + rate-budgeted (`mcp/contracts.py`).
- Tier enforcement: MCP tools only (`contracts.py:208` `_tier_allows`); REST `/api/*` accepts any
  valid tier for everything, including `DELETE /namespace` (guarded against default-ns, but any
  token reaches the guard).
- Identity: `x-yawn-user-id` / `x-yawn-session-id` headers are **client-asserted** — any bearer
  holder can claim any identity. Fine single-user; disqualifying for multi-user.
- Transport: plaintext bearer in `.mcp.json`; no TLS at the app layer (LAN + whatever fronts it).

## Target architecture (MVP)
menhir becomes an **OAuth 2.1 resource server** on its protected HTTP surface per the MCP
authorization spec, with the REST surface enforcing the same scope model. It does **not** become
an authorization server by hand — the AS is delegated (D-A). Static bearer keys remain the
non-OAuth mode; when OAuth is enabled, it owns protected HTTP auth rather than acting as a
compatibility layer beside shared keys.

- **Scopes ↔ existing tiers** (keep the mental model): `memory:read` (readonly), `memory:write`
  (agent), `memory:admin` (operator). Destructive ops (`delete_memory`, `delete_namespace`,
  `recover_orphans`, scheduler controls, conflict resolution) require `memory:admin`.
- **Identity from the token**, not headers: `sub` claim → `user_id`; `x-yawn-user-id` becomes a
  display hint only when OAuth is active. Session ids stay derived.
- **MCP spec compliance** (verify exact requirements against the current spec revision at build
  time — this moves): OAuth 2.1 with PKCE; Protected Resource Metadata (RFC 9728,
  `/.well-known/oauth-protected-resource`) pointing at the AS; audience/resource-indicator
  validation (RFC 8707) so a token minted for another service cannot be replayed here;
  `WWW-Authenticate` on 401. Claude.ai custom connectors additionally expect the AS to support
  dynamic client registration (RFC 7591).

## D-A — the one decision that needs Charles (does not block Phase 0)

**D-A constraint set 2026-07-09 (Charles):** the architecture must support ALL issuer options
(pluggable `MENHIR_OAUTH_ISSUER`/`JWKS_URI` stays the spine — BYO SaaS IdP always possible),
but there MUST be a **simple self-hosting option** that needs no external account. Phase 0's
job is now to pick which self-host rung is the simple one (Rung 1 self-issue mint / 2a
embedded AS / 2c bundled Keycloak) and confirm connector interop for it.
Authorization server options, in rough order of my recommendation:
1. **FastMCP-native auth integration** — menhir already runs on FastMCP; recent FastMCP versions
   ship auth support (JWT verification / OAuth provider integrations). Least code, most aligned
   with the MCP stack. First step of Phase 1 is a version/capability check.
2. **Self-hosted Keycloak on the VPS** — full OAuth 2.1 AS with DCR, users, and a future
   multi-user story; heavier to operate, fully owned.
3. **Commercial IdP (Auth0/WorkOS/Stytch free tier)** — fastest DCR-compatible path for claude.ai
   connectors; external dependency on the memory system's front door.
Hand-rolling an AS is explicitly rejected (token minting, DCR, and revocation are exactly the
wheels not to reinvent for an MVP).

## Phases

**Phase 0 — hardening the current scheme (build NOW; survives every D-A outcome)** — DONE
1. ✓ Q5: `hmac.compare_digest` for all token comparisons (`auth.py:_resolve_tier`).
2. ✓ Q4: per-route tier enforcement on REST — a `required_tier` map for `/api/*` mirroring the MCP
   tools' contracts (destructive → operator; writes → agent; reads → readonly). One middleware
   check beside the existing bearer validation; tests per tier per route class.
3. ✓ Deprecate query-param auth to read-only tools only (today it already has an allowlist — tighten
   it to `memory:read`-equivalents and log usage so removal is data-driven).
   - Implementation: computed allowlist from tool required_tier == "readonly" + add_memory
   - Query-auth usage logged via `record_mcp_event(kind="background", operation="query_auth_usage", ...)`
   - Verified via test_query_auth_allowlist_equals_readonly_tools_plus_add_memory
4. ✓ Audit log for destructive ops (who/which-tier/what) — rides the existing telemetry store.
   - New helper: `record_destructive_op()` in infrastructure/telemetry/recorders.py
   - MCP: called in contracts.py execute() when required_tier == "operator"
   - REST: called in routes.py delete_memory, delete_namespace, and backend_invoke for operator ops
   - Verified via test_destructive_audit.py (operator tools emit, readonly/agent do not)

**Phase 1 — resource-server plumbing (LANDED on `main`, 2026-07-09)**
1. JWT validation: issuer allowlist, audience/resource binding, exp/nbf, JWKS fetch with caching,
   and scope claims → tiers via one table.
2. Protected Resource Metadata and `WWW-Authenticate` bearer challenges are served.
3. OAuth now owns protected HTTP auth when enabled; static bearer/query auth are the non-OAuth path,
   not a parallel compatibility mode on OAuth-protected routes.

**Phase 2 — the AS (per D-A) + connector proof**
1. Stand up the chosen AS; register scopes; enable DCR if claude.ai connectors are in scope.
2. End-to-end proof: a claude.ai custom connector (or MCP inspector) completes the OAuth flow and
   calls one read tool and one write tool with correct scope behavior; a `memory:read` token is
   refused by a destructive REST route (Phase 0's enforcement, now exercised by real tokens).
3. Token lifecycle: short-lived access tokens; refresh handled by the AS; revocation = AS-side.

**Phase 3 — identity binding + migration**
1. HTTP request identity binding is landed: `sub`/`client_id` own the request session and
   client-asserted identity headers are ignored when a JWT is present. Remaining follow-up is to
   verify every downstream stamp path we care about during the live rollout.
2. Per-user namespace policy decision (single-user MVP: all namespaces; multi-user: default
   namespace binding per subject — design note only unless MVP is multi-user, see D-B below).
3. Rotate the `.mcp.json` bearer out if any remote clients still depend on it; local dev keeps the
   no-key open mode (localhost only — the bind-address assertion is already landed).

**D-B (flag, don't decide yet):** is the MVP single-user or multi-user? Multi-user pulls forward
per-subject namespace isolation, per-user budgets, and recall-scope enforcement — a separate plan
if so.

## Explicitly NOT in scope (decided, not forgotten)
- Hand-rolled authorization server, token minting, or session cookies.
- Explorer app auth (localhost-only today; inherits the resource-server layer later or stays
  loopback-bound).
- mTLS / network topology (VPS reverse-proxy TLS is deployment config, not app code — but Phase 3's
  loopback assertion is app code).
- Fine-grained per-namespace ACLs (D-B territory).

## Verification
1. Phase 0: tier-matrix tests (3 tiers × read/write/destructive routes, REST + MCP); timing-safe
   compare pinned by test; query-auth allowlist regression.
2. Phase 1: JWT validation unit tests (bad issuer/audience/exp/scope); metadata endpoint conforms
   to RFC 9728 shape; protected HTTP routes reject static/query auth while OAuth is enabled.
3. Phase 2: live connector flow recorded in the tracker; scope-refusal proofs both surfaces.
4. Phase 3: identity provenance test — a JWT-authenticated ingest stamps `sub`-derived user_id
   regardless of spoofed headers; no-key mode refuses non-loopback bind.
