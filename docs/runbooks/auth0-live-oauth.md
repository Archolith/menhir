# Runbook: Live Auth0 OAuth verification

Verifies Menhir's OAuth **resource-server** mode against a real Auth0 tenant
(the SaaS-IdP counterpart to the local mock-IdP coverage). Menhir does not act as
the authorization server here: Auth0 issues RS256 access tokens; Menhir validates
them against Auth0's JWKS, checks issuer + audience, and maps `menhir:*` scopes to
its tiers.

## Auth0 setup (one time per tenant)

1. **Create an API** (Applications -> APIs). The **Identifier** is the OAuth
   *audience* -- e.g. `https://menhir-test/api`. It never has to resolve; it is
   just a string that must match exactly.
   - **Watch for a trailing space** in the identifier. Auth0 does not show it, and
     audience matching is an exact compare -- a stray space yields
     `access_denied: "Service not enabled within domain"` on every token request,
     the same error Auth0 gives when the API does not exist. Identifiers cannot be
     edited after creation; if one is wrong, create a fresh API.
2. **Permissions tab** -> add `menhir:read`, `menhir:write`, `menhir:admin`.
3. **Settings tab** -> enable **RBAC** and **Add Permissions in the Access Token**.
   Without both, a client-credentials token carries no `menhir:*` scopes and
   Menhir returns 403.
4. **Application Access tab** (older UI: "Machine To Machine Applications") ->
   authorize the M2M app and grant it the three `menhir:*` permissions.

Menhir maps scopes to tiers (`src/menhir/api/oauth.py::tier_from_scopes`):
`menhir:admin` -> operator, `menhir:write` -> agent, `menhir:read` -> readonly. It
reads `scope`, `scp`, and `permissions` claim shapes, so either scope- or
RBAC-permission-based tokens work.

### Scripted provisioning (optional)

A Management-API M2M app can create the whole thing without the dashboard:

```bash
AUTH0_DOMAIN=dev-xxxx.us.auth0.com \
AUTH0_MGMT_CLIENT_ID=<mgmt-app-id> AUTH0_MGMT_CLIENT_SECRET=<secret> \
MENHIR_API_ID='https://menhir-test/api' GRANT_CLIENT_ID=<m2m-app-id> \
python scripts/dev/auth0_provision.py
```

It creates the resource server (RS256, three scopes, RBAC + permissions-in-token)
and the client grant, refusing any identifier with surrounding whitespace.

Debug an existing tenant with `scripts/dev/auth0_diagnose.py` (lists
resource-servers, client-grants, clients). NB: Auth0 Management paths are
**hyphenated** (`/resource-servers`, `/client-grants`), not underscored.

## Inspect a token

```bash
AUTH0_DOMAIN=dev-xxxx.us.auth0.com \
AUTH0_CLIENT_ID=<m2m-app-id> AUTH0_CLIENT_SECRET=<secret> \
python scripts/dev/auth0_token_probe.py --audience 'https://menhir-test/api'
```

A healthy result is HTTP 200, an RS256 JWT with `aud` = the API identifier and
`scope`/`permissions` containing the `menhir:*` values. An opaque (non-JWT) token
means the `audience` param was omitted from the request.

## Run the live smoke

```bash
AUTH0_DOMAIN=dev-xxxx.us.auth0.com \
AUTH0_CLIENT_ID=<m2m-app-id> AUTH0_CLIENT_SECRET=<secret> \
AUTH0_AUDIENCE='https://menhir-test/api' \
python scripts/smoke/auth0_live_smoke.py
```

It mints a real token, launches a throwaway Menhir in the `oauth` shape pointed at
Auth0's real issuer/JWKS/audience, and asserts: no-token -> 401 + challenge; valid
token -> passes auth; bogus token vs the live JWKS -> 401 (token error, not a 503
outage). With the `AUTH0_*` env unset the smoke **skips** (exit 0), so it is safe
inside `scripts/smoke/run_all.py`.

## Credential hygiene

All four scripts read credentials from the environment -- no secret is written to
disk or committed. Client secrets pasted into a terminal still land in shell
history and any session transcript; rotate a test app's secret in Auth0
(Applications -> the app -> Settings -> Rotate) after ad-hoc debugging.
