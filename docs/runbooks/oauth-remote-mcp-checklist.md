# OAuth remote MCP operator checklist

This runbook is for validating Menhir's OAuth **resource-server** posture before exposing protected remote HTTP endpoints to an OAuth-capable client.

Menhir validates access tokens issued by an external identity provider. Menhir does **not** issue OAuth tokens, does **not** run an authorization server, and does **not** make OAuth the default local development path.

## Safety boundary

Use this checklist to prove the local configuration is coherent and the protected-resource behavior is visible. Do not treat this as a compatibility claim for any specific remote client until a real connector smoke test has been run with an actual identity provider token.

Keep these invariants:

- OAuth is disabled by default.
- OAuth is for protected remote HTTP resource-server behavior.
- Static bearer auth remains the non-OAuth mode. When OAuth is enabled, it owns protected HTTP auth.
- No-auth mode must remain loopback-only unless an explicit unsafe override is set.
- Do not expose a remote no-auth Menhir service.
- Do not log, paste, or commit real OAuth tokens, JWKS secrets, or bearer keys.
- OAuth mode owns caller identity for protected HTTP requests; do not rely on caller-controlled `x-menhir-*` identity headers.
- Query-string `?api_key=` fallback must not be used for OAuth-protected HTTP paths.

## Environment variables

Start from `.env.example` and fill in the OAuth block for the target identity provider.

```bash
MENHIR_OAUTH_ENABLED=true
MENHIR_PUBLIC_BASE_URL=https://memory.example.com
MENHIR_OAUTH_RESOURCE=https://memory.example.com/mcp-http
MENHIR_AUTHORIZATION_SERVERS=https://tenant.example.com
MENHIR_OAUTH_ISSUER=https://tenant.example.com/
MENHIR_OAUTH_JWKS_URI=https://tenant.example.com/.well-known/jwks.json
MENHIR_OAUTH_AUDIENCE=https://memory.example.com/mcp-http
MENHIR_OAUTH_SCOPES_SUPPORTED=menhir:read,menhir:write,menhir:admin
MENHIR_OAUTH_READ_SCOPES=menhir:read
MENHIR_OAUTH_WRITE_SCOPES=menhir:write
MENHIR_OAUTH_ADMIN_SCOPES=menhir:admin
```

Notes:

- `MENHIR_PUBLIC_BASE_URL` should be the externally visible origin for the Menhir HTTP service.
- `MENHIR_OAUTH_RESOURCE` should match the resource URI expected by the OAuth client and token audience/resource checks.
- `MENHIR_AUTHORIZATION_SERVERS` should list the trusted authorization server origins that clients can use.
- `MENHIR_OAUTH_ISSUER` and `MENHIR_OAUTH_JWKS_URI` must match the issuer and signing keys for the external identity provider.
- Prefer HTTPS for public URLs. HTTP should only be used for loopback/local smoke work.

## Offline preflight

Run diagnostics before starting any remote smoke. This should not contact the identity provider, fetch JWKS, start the server, require Neo4j, or print secrets.

```bash
python -m menhir.cli diagnostics
python -m menhir.cli diagnostics --json
```

Expected result:

- `oauth_resource_server.status` is `pass`, or only has warnings that are expected for a local smoke.
- URLs with credentials are redacted.
- Non-loopback HTTP URLs warn.
- Metadata URL and bearer challenge resource metadata agree.
- Missing issuer, JWKS URI, audience, resource, or authorization server values fail loudly when OAuth is enabled.

Do not proceed to remote exposure while diagnostics reports OAuth failures.

## Quick local smoke

Use the OAuth local smoke helper to run checks 2–4 against an already-running Menhir HTTP service.

```bash
python scripts/smoke/oauth_local_smoke.py --base-url http://127.0.0.1:8000
python scripts/smoke/oauth_local_smoke.py --base-url http://127.0.0.1:8000 --json  # machine-readable output
```

The helper performs the same checks as the curl commands below:

1. protected-resource metadata (200 + well-formed JSON)
2. Bearer challenge on `/mcp` without Authorization
3. Bearer challenge on `/mcp-http` without Authorization
4. `?api_key=` rejection on `/mcp` and `/mcp-http`

Exit code 0 only when all checks pass. No tokens, no secrets, no IdP, no JWKS fetch, no Neo4j, no Docker, no model calls.

## Local protected-resource metadata check (manual)

Start Menhir in the same environment and fetch protected-resource metadata from the public base URL or local forwarded origin.

```bash
curl -i https://memory.example.com/.well-known/oauth-protected-resource
```

Expected result:

- HTTP 200 when OAuth is enabled.
- JSON metadata advertises the configured resource and authorization server information.
- Metadata should not include tokens or secrets.

When OAuth is disabled, this endpoint is expected to be unavailable rather than advertising stale OAuth metadata.

## Unauthenticated challenge check

Hit an OAuth-protected path without a token.

```bash
curl -i https://memory.example.com/mcp
curl -i https://memory.example.com/mcp-http
```

Expected result:

- HTTP 401.
- `WWW-Authenticate: Bearer ...` is present.
- The challenge points clients at the protected-resource metadata.
- The response does not leak configured keys, raw tokens, or internal secrets.

## Query-string fallback rejection check

OAuth-protected HTTP endpoints should not accept query-string API keys.

```bash
curl -i "https://memory.example.com/mcp?api_key=not-a-real-key"
curl -i "https://memory.example.com/mcp-http?api_key=not-a-real-key"
```

Expected result:

- The request is rejected.
- The error path still advertises OAuth bearer-token authentication.
- A query-string key does not downgrade the request to static bearer behavior.

## Real token smoke, later

Only after the offline and unauthenticated checks pass, run a real identity-provider smoke with a short-lived access token.

For **Auth0**, this is automated end-to-end by `scripts/smoke/auth0_live_smoke.py` (setup in
`docs/runbooks/auth0-live-oauth.md`) — it mints a real token, launches a throwaway Menhir
pointed at Auth0's real issuer/JWKS/audience, and asserts the behaviors below. For any other
IdP, or a manual spot-check, use a short-lived token by hand:

```bash
curl -i \
  -H "Authorization: Bearer $MENHIR_TEST_ACCESS_TOKEN" \
  https://memory.example.com/mcp-http
```

Expected result depends on the MCP transport and request body being exercised, but the auth layer should show the important behavior:

- Valid issuer accepted.
- Valid audience/resource accepted.
- Valid signature accepted from JWKS.
- Expired, wrong-issuer, wrong-audience, wrong-key, malformed, or insufficient-scope tokens rejected.
- Scope maps to the expected Menhir tier: readonly, agent, or operator.
- Caller-controlled `x-menhir-user-id`, `x-menhir-client-id`, and `x-menhir-session-id` must not override the OAuth principal identity.

Never paste a real access token into a PR, issue, log excerpt, or committed fixture.

## Failure-mode table

| Symptom | Likely cause | What to check |
| --- | --- | --- |
| OAuth metadata is 404 | OAuth disabled or server not using the expected env | Confirm `MENHIR_OAUTH_ENABLED=true` and restart with the intended `.env`. |
| Diagnostics fails on missing issuer | Incomplete OAuth config | Set `MENHIR_OAUTH_ISSUER` to the exact IdP issuer. |
| Diagnostics warns on HTTP URL | Public URL is not HTTPS, or local smoke URL is not loopback | Use HTTPS for remote; use `127.0.0.1` or `localhost` for local-only HTTP. |
| Diagnostics warns about credentials in URL | User info appears in a configured URL | Remove embedded credentials from OAuth/public URLs. |
| Unauthenticated protected request does not return bearer challenge | Request is not reaching an OAuth-protected path or OAuth is disabled | Check path, routing, and `MENHIR_OAUTH_ENABLED`. |
| `?api_key=` works against MCP while OAuth is enabled | Auth downgrade regression | Treat as a blocker; OAuth-protected HTTP paths must not accept query-string key fallback. |
| Valid token rejected for audience/resource | Token audience/resource does not match Menhir config | Compare token claims with `MENHIR_OAUTH_AUDIENCE` / `MENHIR_OAUTH_RESOURCE`. |
| Valid token rejected for issuer | Issuer string mismatch, often trailing slash | Compare the exact issuer claim with `MENHIR_OAUTH_ISSUER`. |
| Valid token rejected for signing key | JWKS URI wrong, stale, or missing key ID | Confirm `MENHIR_OAUTH_JWKS_URI` and IdP key rotation state. |
| Token accepted but wrong identity shown | Caller headers are overriding OAuth principal | Treat as a blocker; OAuth principal claims must own identity. |

## PR acceptance checklist

For docs/checklist-only changes:

- No auth behavior changes.
- No live IdP dependency.
- No network calls in tests.
- No server-start requirement in unit tests.
- No Neo4j requirement.
- No Docker requirement.
- No model calls.
- No archolith-bench work.
- No LongMemEval work.
- No token, key, or secret examples beyond obvious placeholders.
- No claim that a specific external client works until a real smoke test proves it.
