# Phase 0 — Interop Verification + Library Decision (GATE)

Parent: `menhir-embedded-oauth-as-plan.md`. **This is a decision gate — no product code
ships in this phase.** It produces a findings document and two decisions that shape
Phases 3-10. Do not author or start Phases 3-10 until this is complete.

**Project:** `projects/archolith/menhir/`.

## Objective

Auth is **deployment-tiered** (see master spine): local installs need none, VPS-public
installs need one-click OAuth. This phase does not pick one global rung — it (a) confirms the
tiered spine holds, and (b) for the **public-server tier only**, recommends how that tier
gets its issuer (pluggable BYO IdP vs bundled Keycloak vs embedded AS). Answer with evidence:

1. **Interop (per connector — they differ):**
   - **ChatGPT:** does its current Apps SDK / connector flow complete OAuth against an
     **embedded, same-origin** AS (AS metadata + RFC 7591 DCR + auth-code + PKCE served by
     Menhir)? Evidence suggests yes (CIMD, DCR, predefined clients, PKCE) — confirm and cite.
   - **Claude:** the API-side MCP connector takes an `authorization_token` and expects the
     consumer to run the OAuth flow/refresh — i.e. **paste-a-token, and the embedded AS does
     NOT give it one-click**. Confirm this, and separately record whether any full MCP client
     / MCP Inspector path gives Claude a non-paste flow. Cite.
2. **Build-vs-buy + installer feasibility:** OpenAI's docs recommend an established IdP over
   DIY auth. Weigh it against the ladder, and specifically assess **Rung 2c** (bundled
   self-hosted IdP auto-provisioned by an installer):
   - Can a script/first-run wizard fully provision **Keycloak** (docker compose + realm-import
     JSON enabling a menhir client, `menhir:read/write/admin` scopes, **DCR** via anonymous
     registration policy / initial-access-token) and write issuer/JWKS into Menhir config,
     with zero external account? Confirm the DCR-enablement mechanism concretely.
   - Compare **Keycloak** (heavier JVM, includes login/consent UI) vs **Ory Hydra** (lighter
     Go binary, but requires a login/consent app you build) for the bundle. Bundling is cheap
     (already docker-compose for Neo4j; IdP = one more service + `--import-realm`). Footprint
     (Keycloak ~0.4-0.6 GB JVM vs Hydra ~30-80 MB) matters **only for VPS-public users** — it
     is confined to the tier that opted in; local users run no IdP at all, so this is not a
     universal cost.
   - **Pluggable-issuer confirmation:** verify Menhir's resource-server config already accepts
     an arbitrary external `MENHIR_OAUTH_ISSUER`/`JWKS_URI` (it does — `api/oauth.py`), so
     "BYO hosted IdP" and "bundled Keycloak" are the *same* Menhir config with a different
     issuer URL. This is the spine; the bundle is an optional compose profile, not core code.
   - **Installer tier-selection:** the first-run installer/wizard asks *"local, or a server
     others connect to?"* and sets the tier — loopback (local) vs static-key/mint (private
     server) vs OAuth+issuer (public server). Only the public-server path pulls in an IdP.
     Record this as the installer's contract.
   - Note the SaaS limit: an installer cannot create an Auth0/Clerk account, so **2b** stays
     reduced-steps, not zero — records this explicitly.
   - Production input: both need an HTTPS domain for the IdP issuer/redirect URIs; note what
     the installer templates vs what the operator must supply.
3. **Library:** Which library implements the AS endpoints — Authlib's AS framework, or
   hand-rolled grants on `joserfc`, or another — given (a) FastAPI/Starlette + pure-ASGI
   middleware, and (b) the in-progress S-009 migration off `authlib.jose`? (Only needed if the
   recommended rung is 2.)

## Tasks

1. **Read the MCP authorization spec** (current revision) and record the exact required
   endpoints/flows: RFC 8414 AS metadata, RFC 9728 protected-resource metadata (already
   shipped — `api/oauth_metadata.py`), RFC 7591 DCR, authorization-code + PKCE (RFC 7636).
   Note any "MUST" the client imposes on the AS location (same-origin vs separate).
2. **Verify ChatGPT connector behavior:** from its current connector/authorization docs,
   confirm whether it (a) reads `authorization_servers` from the resource metadata, (b)
   performs DCR at `/register`, (c) tolerates the AS being the same origin as the resource.
   Capture citations.
3. **Verify Claude connector behavior:** same three checks against Claude's current remote
   MCP connector docs. Capture citations.
4. **Library evaluation:** determine whether `authlib.oauth2` provides a framework-agnostic
   `AuthorizationServer` + RFC 7591/7636 grants usable from pure ASGI/Starlette without the
   Flask/Django integrations, and how that interacts with the S-009 `joserfc` migration.
   Record a recommendation with rationale (reuse vs. new dep vs. hand-roll).
5. **Confirm the data-dir convention** for AS state: extend `src/menhir/infrastructure/paths.py`
   pattern — a new `oauth_as_db_path()` returning `workspace_root() / ".agent" /
   "menhir_oauth_as.db"` with a `MENHIR_OAUTH_AS_DB` env override (mirror `telemetry_db_path`).
   This is a spec decision recorded here; the helper is added in Phase 1/2.
6. **Write the findings + decisions** to
   `.agent/reviews/menhir-oauth-as-interop-findings.md`: per-connector interop verdict with
   citations (ChatGPT vs Claude separately), the build-vs-buy weighing, the library decision +
   rationale (if Rung 2), the token-shape decision (claims: `iss`=self, `aud`=resource, `sub`,
   `client_id`, `client_name`, `scope`), and a bold **RECOMMENDED RUNG (0/1/2/3)** with
   justification.
7. **Recommend a rung, then route accordingly:**
   - **Rung 1 (self-issue mint):** author a `menhir-oauth-as-rung1-mint.md` plan (admin-gated
     token mint endpoint reusing Phases 1-2 signing key + client store) and stop there — no
     embedded AS. Satisfies Claude + all bearer clients + provenance.
   - **Rung 2 (embedded AS):** only if ChatGPT one-click is judged worth it — author the
     Wave-2 child plans (Phases 3-10) against the confirmed library/token decisions.
   - **Rung 3 (external IdP) / NO-GO on embedded:** record why, name Auth0-free/Keycloak as
     the fallback, and stop the embedded-AS track.
   In all cases Phases 1 and 2 (signing key, client store) are still useful foundations and
   may proceed.

## Acceptance criteria

- `.agent/reviews/menhir-oauth-as-interop-findings.md` exists with: two per-connector
  verdicts (with doc citations), the build-vs-buy weighing, a library decision + rationale
  (if Rung 2), a token-shape spec, the `oauth_as_db_path()` convention, and a bold
  **RECOMMENDED RUNG** line with justification.
- The recommended rung's next plan is authored and linked from the master (Rung 1 mint plan,
  or Phase 3-10 for Rung 2, or the external-IdP fallback note).
- No product source changed in this phase.

## Notes for the executor

- This is research + decision work; use web/doc sources and cite them. Do not guess connector
  behavior — if a source is unavailable, record the gap and mark that item unverified rather
  than asserting it.
- Phase 1 and Phase 2 do not depend on this and may proceed in parallel.
