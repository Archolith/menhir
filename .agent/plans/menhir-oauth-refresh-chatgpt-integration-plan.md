---
artifact_schema: 1
artifact_uuid: 304142d9-eacc-49ac-938e-86e4ed7b1b30
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir ChatGPT web MCP compatibility: OAuth refresh, identity, and tool auth

## Objective

Make Menhir compatible with the current documented ChatGPT web MCP contract for the intended single-owner/private deployment, then prove that compatibility with a real ChatGPT connection.

This plan began as AS-005 refresh-token completion. A backward review on 2026-08-24 found that refresh alone cannot support a defensible compatibility claim: current ChatGPT integration also depends on client identity, RFC 9207 issuer responses, tool-level OAuth metadata/challenges, complete tool presentation metadata, and a real transport/auth lifecycle test.

This is still not a general identity-platform redesign. It is the bounded work required for the current ChatGPT web MCP contract.

## Compatibility target

Reviewed 2026-08-24 against the current OpenAI MCP authentication/server guidance and the current MCP authorization specification.

"100% compatible" in this plan means all of the following are true at the same time:

1. Every relevant current documented ChatGPT MCP requirement for this private/developer-mode integration is implemented.
2. Menhir remains valid under the MCP authorization requirements those ChatGPT behaviors rely on.
3. A real ChatGPT web connection proves discovery, authorization, tool discovery, tool invocation, token expiry, refresh, restart persistence, and post-refresh invocation end to end.
4. Any protocol/version behavior ChatGPT actually negotiates is captured and covered by regression tests.

It does **not** mean future ChatGPT releases can never change, and it does not claim public app-directory submission, Company Knowledge, UI/Apps SDK features, or every optional OAuth/OIDC feature.

## Backward proof from the desired end state

| Desired end state | What must be true | Current state / gap | Owning phase |
|---|---|---|---|
| ChatGPT reaches Menhir | Public stable HTTPS Streamable HTTP endpoint; exact canonical resource URL | `/mcp-http` exists and the default resource is `<public-base>/mcp-http`; real public/tunnel path still unproven | 0, 10 |
| ChatGPT discovers auth | 401 Bearer challenge -> protected-resource metadata -> AS metadata | Resource challenge/discovery exists; challenge scope behavior needs tightening | 2, 5 |
| ChatGPT has a durable client identity | CIMD preferred/current path plus DCR fallback | Menhir currently resolves only persisted DCR client IDs; CIMD is explicitly absent | 1, 3 |
| ChatGPT uses a stable OAuth callback | AS advertises RFC 9207 and every authorization success/error redirect carries exact `iss` | Metadata has no RFC 9207 flag; authorize redirects omit `iss` | 1, 2 |
| ChatGPT can request durable access | AS advertises refresh grant; `offline_access` is AS-only; refresh issuance policy works with ChatGPT's actual request | Refresh primitive exists upstream but Menhir does not wire it; one shared scope tuple currently feeds PRM and AS metadata | 2, 6, 7 |
| ChatGPT sees the correct OAuth UI per tool | Per-tool `securitySchemes`; auth failures can return `_meta["mcp/www_authenticate"]` with `isError=true` | BaseTool registers only description; there is no tool auth metadata/result challenge | 4, 5 |
| ChatGPT can choose tools safely | Human title, description, explicit schema, accurate safety annotations | Names/descriptions/signatures exist; titles/annotations are not declared or startup-validated | 4 |
| Full owner surface is reachable | Initial connection obtains the full intended permission set; catalog filtering cannot hide tools needed later | `tools/list` is tier-filtered, so a read-only first token would hide write/operator tools | 2, 4, 8 |
| Access expiry is transparent | Refresh token persists, rotates, survives process restart, and ChatGPT actually uses it | Shared refresh store supports rotation/replay defense; Menhir has no configured store/endpoint branch | 6, 7, 10 |
| Transport is accepted by ChatGPT | Streamable HTTP and an MCP protocol version the current ChatGPT client accepts | Menhir resolves FastMCP 3.4.4 / MCP 1.28.1; do not assume either acceptance or required upgrade | 0, 9, 10 |

The plan is complete only if every row is proven, not merely implemented.

## Chosen ChatGPT compatibility profile

This deployment is a **single-owner, full-Menhir-surface** integration.

- Canonical protected resource: `https://<host>/mcp-http` unless explicitly overridden.
- Authorization server issuer: public Menhir base URL.
- Permission scopes: `menhir:read`, `menhir:write`, `menhir:admin`.
- Durable-access protocol scope: `offline_access`.
- `offline_access` is authorization-server-only. It MUST NOT appear in protected-resource metadata or resource `WWW-Authenticate` scope challenges, and it never maps to a Menhir tier.
- The ChatGPT owner connection requests/grants all three Menhir permission scopes so `TierFilteredFastMCP` exposes the complete surface. Per-tool `securitySchemes` still declare each tool's minimum scope.
- CIMD is supported as the preferred modern identity path; DCR remains a fallback.
- `token_endpoint_auth_method=none` remains the baseline. ChatGPT supports public clients, so `private_key_jwt` is not required for this profile.
- PKCE remains S256-only.
- Access tokens remain audience/resource-bound to the exact MCP resource.

A future least-privilege/step-up profile may intentionally expose a smaller catalog, but it is a separate design because the current tier-filtered catalog makes hidden higher-tier tools undiscoverable to a low-scope first connection.

## Current state that should be preserved

Already implemented:

- Streamable HTTP MCP at `/mcp-http`.
- OAuth protected-resource metadata and authorization-server discovery.
- Dynamic client registration for public clients.
- Authorization-code flow with mandatory PKCE S256.
- RFC 8707 `resource` binding through authorization and token exchange.
- JWT access tokens, JWKS, issuer/audience/scope validation.
- Single-owner consent gated by `MENHIR_OPERATOR_KEY`.
- OAuth scopes mapped to Menhir authorization tiers.
- Tier-filtered `tools/list` plus invocation-time tier checks.
- Namespace pins and per-client tool allowlists.
- Shared `archolith_oauth` refresh primitives: hashed SQLite storage, single-use rotation, replay-family revocation, `exchange_refresh_token()`.

## Non-goals

These are not blockers for the compatibility profile above:

- Multi-user account management or replacing `MENHIR_OPERATOR_KEY` consent.
- External IdP migration.
- OIDC `openid`/`email`, UserInfo, workspace-domain restrictions, or `id_token_hint` optimization.
- `private_key_jwt` while public-client `none` remains accepted by ChatGPT.
- mTLS or ChatGPT egress-IP allowlisting.
- Public app-directory submission/domain verification.
- Company Knowledge `search`/`fetch` conventions.
- Apps SDK UI resources/components.
- Converting Menhir's JSON-string tools to structured output solely to obtain `outputSchema`; add output schemas only where the tool actually emits MCP `structuredContent`.
- Blind migration to a newer MCP/FastMCP wire version without an observed ChatGPT or conformance requirement.

## Phase 0 — Protocol and endpoint preflight

### Work

1. Stand up the intended compatibility environment through a stable public HTTPS URL or the supported secure development tunnel.
2. Set `MENHIR_PUBLIC_BASE_URL` to that exact public origin and verify the derived resource is exactly `<origin>/mcp-http`.
3. Fetch and record:
   - protected-resource metadata,
   - authorization-server metadata,
   - JWKS,
   - initial unauthenticated `/mcp-http` challenge.
4. Run the current MCP Inspector against `/mcp-http` and capture the protocol version negotiated by the current Menhir dependency set.
5. Record the live dependency baseline: Menhir currently pins `fastmcp>=3.2.4,<4` and resolves FastMCP 3.4.4 with MCP 1.28.1.
6. Do **not** upgrade merely because a newer MCP revision exists. Upgrade FastMCP/MCP only if the Inspector or real ChatGPT client rejects the current negotiation or a required tool/auth field cannot be represented by the pinned SDK.

### Acceptance

- Public HTTPS endpoint is stable and externally reachable.
- `resource` is byte-for-byte the URL ChatGPT will connect to.
- Inspector completes initialize, `tools/list`, and a representative tool call.
- The negotiated MCP protocol version is recorded.

## Phase 1 — Shared OAuth primitives required by ChatGPT

Prefer generic OAuth protocol behavior in `archolith_oauth`; keep Menhir responsible for deployment policy and Menhir scopes.

### Shared-package work

1. Add RFC 9207 authorization-response issuer support to AS metadata/config:
   - `authorization_response_iss_parameter_supported: true` when enabled.
2. Add CIMD capability advertisement when enabled:
   - `client_id_metadata_document_supported: true`.
3. Add an SSRF-safe CIMD metadata resolver suitable for an authorization server accepting URL client IDs.
4. Extend refresh exchange only where needed for interoperability:
   - accept an optional refresh `scope` when present,
   - allow only equal or narrower scopes than the original grant,
   - never permit scope expansion.
5. Add an explicit refresh-issuance policy seam if the real ChatGPT client does not request `offline_access`; OAuth servers may issue refresh tokens at their discretion, and durable compatibility must not depend on an undocumented ChatGPT scope request.
6. Bump Menhir's exact `archolith_oauth` pin only after the generic package changes are tested.

### CIMD fetch safety requirements

The resolver must:

- require HTTPS and a non-root metadata path;
- reject URL credentials and fragments;
- resolve DNS and reject loopback, private, link-local, multicast, reserved, and otherwise non-public destinations;
- re-check every connection target against DNS-rebinding/redirect tricks;
- disable redirects or allow only explicitly revalidated safe redirects;
- use strict connect/read timeouts and a bounded response size;
- require JSON metadata and exact client-id/metadata-document identity;
- validate redirect URIs with the same strict rules used for DCR;
- log no sensitive response body or credentials;
- cache only bounded, validated metadata.

### Acceptance

- Shared-package tests cover RFC 9207 metadata, CIMD valid path, DNS/IP/redirect SSRF rejections, and refresh scope narrowing.
- Menhir's pin points to the tested revision.

## Phase 2 — Split resource scopes from AS scopes and add RFC 9207

### Files

- `src/menhir/config/oauth.py`
- `src/menhir/config/settings_model.py` as needed
- `src/menhir/api/oauth_metadata.py`
- `src/menhir/api/oauth_as_metadata.py`
- `src/menhir/api/oauth_authorize.py`

### Work

1. Keep protected-resource scopes strictly to Menhir permission scopes.
2. Build AS scopes separately; when refresh is enabled, append `offline_access` only there.
3. Pass `issue_refresh_tokens` and refresh TTL into `AuthorizationServerConfig`.
4. Advertise `authorization_code` + `refresh_token` only when refresh is actually enabled.
5. Advertise RFC 9207 issuer-response support.
6. Append exact AS issuer `iss` to **every** trusted authorization redirect:
   - successful code response,
   - `access_denied`,
   - `invalid_scope`,
   - other redirectable OAuth errors.
7. Preserve exact `state` round-trip.
8. For this full-owner ChatGPT profile, ensure the initial connection is offered/granted `menhir:read menhir:write menhir:admin` so all tier-filtered tools remain visible.
9. Keep `offline_access` out of PRM `scopes_supported`, HTTP resource challenges, and tier mapping.

### Acceptance

- PRM has only Menhir permission scopes.
- AS metadata adds `offline_access` only while refresh is enabled.
- Every authorization redirect carries exact `iss` and original `state`.
- `offline_access` alone cannot authenticate to Menhir or raise a tier.
- A full-owner token contains all three Menhir permission scopes and maps to operator.

## Phase 3 — CIMD identity with DCR fallback

### Files

- `src/menhir/api/oauth_as_register.py`
- `src/menhir/api/oauth_authorize.py`
- `src/menhir/api/oauth_client_store.py` or a narrowly named CIMD cache wrapper
- shared `archolith_oauth` resolver from Phase 1

### Work

1. Advertise CIMD support in AS metadata.
2. At authorization, distinguish:
   - ordinary persisted DCR `client_id`,
   - HTTPS client metadata document URL.
3. For CIMD, fetch and validate metadata through the shared SSRF-safe resolver.
4. Require an exact redirect URI match and an auth-method intersection Menhir supports (`none`).
5. Persist/cache the validated client snapshot in durable AS storage under its URL client ID so code exchange and later refresh after a process restart can resolve the same client.
6. Never treat a previously cached CIMD document as permanently trusted: define a bounded freshness/revalidation policy while ensuring an already-issued refresh grant does not become unusable merely because the AS process restarted.
7. Keep DCR functional as fallback.
8. Make DCR request validation and response truthful to the refresh feature flag; do not accept/advertise a grant the token endpoint has disabled.

### Acceptance

- Current ChatGPT CIMD metadata can be resolved and authorized as a public client.
- Malicious/private/redirecting client metadata URLs fail closed.
- DCR still succeeds when CIMD is not used.
- Both identity paths survive a Menhir restart and can complete token exchange.

## Phase 4 — Complete ChatGPT tool metadata

### Files

- `src/menhir/mcp/contracts.py`
- tool classes under `src/menhir/mcp/tools/`
- `src/menhir/mcp/tools/__init__.py`
- possibly `src/menhir/api/mcp_remote.py` if the current SDK needs a list-tools transform to expose fields

### Work

Add explicit presentation/security declarations to every ChatGPT-visible tool:

1. Human-readable `title`.
2. Curated `description` (existing descriptions remain the starting point).
3. Accurate safety annotations:
   - `readOnlyHint`,
   - `destructiveHint`,
   - `openWorldHint` where relevant.
4. Per-tool OAuth `securitySchemes` with Menhir protected-resource metadata and the minimum scope for that tool:
   - readonly -> `menhir:read`,
   - agent -> `menhir:write`,
   - operator -> `menhir:admin`.
5. Keep explicit input schemas derived from typed endpoint signatures.
6. Add `outputSchema` only for tools that truly return MCP structured content; do not describe JSON encoded inside text as structured output.
7. Add a startup validator analogous to `assert_tool_scopes_declared()` that refuses to register a tool missing required ChatGPT metadata or a coherent security mapping.
8. Do not infer destructive/read-only semantics from tier alone. An operator tool can be non-destructive and an agent tool can write; declarations must be reviewed per tool.

### Acceptance

- Every visible tool has title, description, input schema, reviewed annotations, and OAuth `securitySchemes`.
- Startup fails on a new tool that omits the required declarations.
- Inspector shows the exact metadata ChatGPT receives.

## Phase 5 — HTTP and tool-result OAuth challenges

### Files

- `src/menhir/api/auth.py`
- `src/menhir/mcp/contracts.py`
- supporting MCP result/error helpers

### Work

1. Preserve HTTP auth semantics:
   - missing/invalid/expired token -> 401,
   - insufficient permission scope -> 403.
2. Include protected-resource metadata in Bearer challenges.
3. For insufficient scope, include:
   - `error="insufficient_scope"`,
   - the minimum scope required for the denied operation where known,
   - `resource_metadata`.
4. For the initial full-owner connection, provide a scope signal that leads ChatGPT to all three Menhir permission scopes; do not include `offline_access` in a resource challenge.
5. Add MCP tool-result authorization signaling expected by ChatGPT:
   - result `isError: true`,
   - `_meta["mcp/www_authenticate"]` containing the Bearer challenge.
6. Introduce a typed authorization failure/result path instead of translating every domain `PermissionError` into an OAuth challenge. Tenancy failures, invalid arguments, and domain refusals are not token-refresh requests.
7. Keep HTTP `server_error` behavior transient (503, no fake re-auth loop).

### Acceptance

- Invalid/expired HTTP token produces a 401 resource challenge.
- Insufficient tool scope can produce both correct HTTP/transport behavior and the tool-result `_meta` challenge ChatGPT uses for linking/step-up UI.
- Domain/tenancy refusals do not masquerade as OAuth failures.

## Phase 6 — Persistent refresh store and configuration

### Files

- Add `src/menhir/api/oauth_refresh_store.py`.
- Update `src/menhir/config/settings_model.py`.
- Update `src/menhir/api/server_support.py`.
- Update `src/menhir/api/server.py` if app-state access is needed.
- Update `.env.example`.

### Work

1. Add `oauth_as_refresh_tokens_enabled`, default `False`.
2. Add `oauth_as_refresh_ttl_s`, default 30 days.
3. Parse both from environment.
4. Validate refresh TTL > 0 at settings construction.
5. Mirror the auth-code/client-store wrapper and construct `archolith_oauth.RefreshTokenStore` under `oauth_as_db_path(...)`.
6. Configure it during server prerequisite construction when embedded AS + refresh support are enabled.
7. Make the configured store available to the token endpoint without request-time environment drift.
8. Keep refresh default-off for existing deployments; the ChatGPT compatibility deployment explicitly enables it.

### Acceptance

- Refresh state survives process restart.
- Raw refresh tokens are never persisted.
- Invalid TTL fails startup.
- Feature-disabled behavior is unchanged.

## Phase 7 — Token endpoint refresh wiring

### File

- `src/menhir/api/oauth_token.py`

### Work

Branch on `grant_type`:

### `authorization_code`

- Preserve PKCE/resource/client/redirect binding.
- Pass the configured refresh store into `exchange_authorization_code()`.
- Issue a refresh token when the AS's durable-access policy says to do so.
- Prefer the standard `offline_access` request path when ChatGPT actually sends it.
- If the live ChatGPT client omits `offline_access`, do not declare success with an access-token-only session: use the explicit shared-package issuance-policy seam from Phase 1 so owner-approved ChatGPT durable access still receives a refresh token without adding `offline_access` to the protected-resource permission model.

### `refresh_token`

- Require refresh token, client ID, and exact resource.
- Accept optional `scope`; equal/subset only, never expansion.
- Call shared `exchange_refresh_token()`.
- Return the rotated refresh token and new access token.
- Preserve `Cache-Control: no-store` and `Pragma: no-cache`.

### Acceptance

- Initial code exchange returns access + refresh for the proven ChatGPT durable-access flow.
- Rotation succeeds once.
- Consumed token replay revokes the family.
- Wrong client/resource fails.
- Scope expansion fails.
- Refreshed access token preserves subject, client identity, resource/audience, and granted Menhir scopes.

## Phase 8 — Authorization and catalog invariants

Prove the compatibility work does not weaken Menhir:

- `menhir:read`, `menhir:write`, `menhir:admin` remain the only permission scopes that determine tier.
- `offline_access` affects token longevity only.
- Full-owner ChatGPT token maps to operator and therefore sees the complete tier-filtered catalog.
- A deliberately read-only test token sees only readonly tools and cannot invoke higher-tier tools by guessing names.
- Namespace pins remain enforced.
- Per-client tool allowlists remain enforced.
- CIMD/DCR client identity is bound from verified/registered metadata, never caller-controlled headers.
- Access-token audience remains the exact `/mcp-http` resource.

## Phase 9 — Automated conformance and Inspector matrix

### Automated tests

Add focused Menhir integration tests for:

- refresh-disabled and refresh-enabled discovery;
- PRM/AS scope separation;
- RFC 9207 metadata and `iss` on every success/error authorization redirect;
- CIMD valid flow and SSRF rejection corpus;
- DCR fallback;
- DCR/metadata grant truthfulness;
- authorization code with/without durable-access request;
- successful refresh rotation and family replay revocation;
- client + refresh persistence across complete store/server reconstruction;
- optional refresh scope equal/subset/expansion behavior;
- wrong client/resource rejection;
- all three Menhir permission scopes -> operator;
- `offline_access` alone -> no Menhir tier;
- HTTP 401 and 403 Bearer challenges;
- tool-result `_meta["mcp/www_authenticate"]` and `isError`;
- full tool metadata census and startup failure for omissions;
- feature-disabled regressions.

Do not duplicate shared-package unit tests; Menhir tests prove wiring and externally observable behavior.

### Inspector matrix

With current MCP Inspector:

1. Initialize.
2. Record negotiated protocol version.
3. Inspect every tool's title, description, input schema, annotations, and security schemes.
4. Call every tool at least once with a safe representative case or a deliberate dry-run/test fixture.
5. Exercise invalid input for representative schema families.
6. Exercise unauthenticated, expired-token, insufficient-scope, and authorized cases.
7. Verify resources/list/read if ChatGPT-visible resources remain exposed.

If required metadata cannot be represented by the current MCP SDK, this phase triggers the bounded SDK migration. The migration is complete only when the same matrix passes again.

## Phase 10 — Real ChatGPT web acceptance test

Use the actual intended ChatGPT web connection, not a simulated client.

### Environment

- stable public HTTPS URL or supported secure developer tunnel;
- `MENHIR_PUBLIC_BASE_URL` = exact public origin;
- `MENHIR_OAUTH_AS_ENABLED=true`;
- refresh enabled;
- `MENHIR_OPERATOR_KEY` configured;
- disposable/test namespace for mutating calls;
- shortened access TTL for the expiry test;
- normal durable refresh TTL.

### Required sequence

1. Add/refresh the Menhir connector/app in ChatGPT after metadata changes.
2. ChatGPT reaches `/mcp-http` and receives the expected protected-resource discovery signal.
3. Capture actual PRM and AS discovery traffic.
4. Capture the actual client identity path:
   - CIMD preferred; record metadata URL and selected token auth method,
   - DCR fallback if ChatGPT chooses it.
5. Capture actual authorization request:
   - redirect URI,
   - `resource`,
   - scopes,
   - PKCE S256,
   - state.
6. Owner approves on Menhir consent page.
7. Verify redirect returns exact `iss` and state to ChatGPT.
8. Capture token exchange and confirm access + refresh token issuance.
9. `tools/list` exposes the complete intended Menhir catalog with tool metadata/security schemes.
10. From ChatGPT, execute representative safe operations across all permission classes:
    - readonly: `recall_memories` or equivalent;
    - agent/write: `add_memory` into the disposable namespace;
    - operator: a non-destructive operator action or explicit dry-run wherever available.
11. Shorten/expire the access token.
12. **Restart Menhir before refresh**. This proves both refresh state and client identity state are durable rather than process-local.
13. Without new consent/login, let ChatGPT refresh the access token.
14. Confirm rotation produced a new refresh token and the new access token remains resource/scope/client bound.
15. Execute another MCP call successfully with the refreshed access token.
16. In the automated/Inspector lane, replay the consumed refresh token and verify family revocation; do not intentionally corrupt the user's live ChatGPT session merely to prove replay defense.
17. Exercise direct, indirect, edge, and out-of-scope ChatGPT prompts to verify the model sees and chooses the tool surface sensibly.

### Evidence to record

Record protocol facts, not credentials:

- ChatGPT client metadata/DCR shape;
- chosen client ID form and redirect URI;
- requested scopes;
- whether ChatGPT requested `offline_access`;
- token auth method;
- OAuth errors if any;
- negotiated MCP protocol version;
- whether ChatGPT consumed the rotated refresh token after restart;
- tool-list metadata observed by ChatGPT/Inspector.

Never log access tokens, refresh tokens, authorization codes, PKCE verifier, operator key, or consent secret.

## Conditional compatibility branches

These are part of this plan, not permission to declare success early:

- **ChatGPT omits `offline_access`:** use the explicit AS refresh-issuance policy seam and repeat Phase 10 until ChatGPT receives and uses a refresh token.
- **ChatGPT chooses DCR instead of CIMD:** DCR must pass; CIMD still remains implemented and tested because it is part of the current modern client-identity contract.
- **ChatGPT/Inspector rejects the current MCP wire or current SDK cannot expose required fields:** perform the smallest FastMCP/MCP upgrade that satisfies the requirement, re-run the full Inspector and regression matrix, and pin the working version.
- **ChatGPT sends a refresh `scope`:** accept only equal/subset semantics and prove it.
- **ChatGPT exposes a different documented required field during the real run:** add the smallest compatibility patch and repeat the acceptance sequence. A real-run failure leaves this artifact open.

## Rollout

1. Land shared OAuth primitives and Menhir compatibility wiring default-off where appropriate.
2. Enable the full profile on the private/test deployment.
3. Pass automated tests + Inspector matrix.
4. Run the real ChatGPT acceptance flow.
5. Fix every demonstrated required interoperability defect and repeat from the failing boundary.
6. Enable on the intended Menhir deployment only after the complete acceptance sequence passes.
7. Any future default-on refresh policy or broader identity platform remains a separate owner decision.

## Done criteria

This plan is complete only when all of these are true:

- Stable public HTTPS `/mcp-http` is reachable by ChatGPT.
- Protected-resource and AS discovery are correct and truthful.
- Canonical resource/audience is exact `/mcp-http`.
- RFC 9207 is advertised and every authorization success/error redirect carries exact `iss`.
- CIMD works with SSRF-safe resolution and DCR remains a working fallback.
- ChatGPT's chosen client identity survives a Menhir restart.
- Every ChatGPT-visible tool carries reviewed title, description, input schema, safety annotations, and OAuth `securitySchemes`.
- Auth failures expose both correct HTTP Bearer challenges and ChatGPT tool-result `_meta["mcp/www_authenticate"]` behavior where applicable.
- The full-owner ChatGPT token contains all three Menhir permission scopes, maps to operator, and exposes the full intended catalog.
- `offline_access` never becomes a resource permission or Menhir tier.
- ChatGPT receives a refresh token in the actual connection flow.
- Access expiry followed by a **Menhir restart** does not require re-consent/re-login.
- ChatGPT successfully rotates the refresh token and performs another MCP call.
- Replay defense is proven in automated tests.
- Existing tier, namespace, client-allowlist, and audience boundaries do not regress.
- Current Inspector matrix passes.
- The actual ChatGPT protocol/client/scopes/redirect/refresh observations are recorded without secrets.
- No known current documented requirement for this compatibility profile remains unimplemented or untested.

Only after those gates pass is "100% compatible with the current documented ChatGPT web MCP contract as reviewed on 2026-08-24" a defensible statement.

## Suggested implementation commits

1. `feat(oauth): add RFC9207 and safe CIMD primitives`
2. `feat(oauth): split resource and authorization-server scopes`
3. `feat(oauth): accept CIMD clients with DCR fallback`
4. `feat(mcp): declare ChatGPT tool metadata and security schemes`
5. `feat(mcp): emit OAuth challenges in tool results`
6. `feat(oauth): configure persistent refresh-token storage`
7. `feat(oauth): wire refresh-token grant and durable issuance policy`
8. `test(oauth): prove ChatGPT auth and refresh invariants`
9. `test(mcp): prove tool metadata and auth challenge contract`
10. `docs(oauth): record Inspector and ChatGPT compatibility run`
