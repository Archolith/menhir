# Client Token Tier — Enforced Per-Client Identity

## 1. What it is / when to use

The client-token tier gives each client its own opaque bearer token whose identity and
access tier are bound **server-side** in a SQLite registry. A client **cannot** lie about
who it is — self-declared `x-menhir-*` headers are ignored. Identity comes from the
token-to-registry lookup, not from the caller.

Use this tier for:

- Network / VPS deployments where multiple clients connect to the same Menhir server.
- Any multi-client setup that needs trustworthy provenance without an external identity
  provider.
- Scenarios where you want per-client identity separation but do not want to host a
  full OAuth/JWKS/JWT authorization server.

Contrast with other authentication modes:

| Mode | Provenance model | When to use |
| --- | --- | --- |
| **Loopback no-auth** | Cooperative labels (`x-menhir-*` headers), **not** enforced | Local dev, single-user, loopback-only |
| **OAuth** | External IdP tokens (JWT/JWKS) | Federated identity, external SSO, IdP-managed scopes |
| **Client token (this tier)** | Self-issued enforced tokens (hashed registry) | Multi-client private server, per-client identity without an IdP |

## 2. Enabling

Set the environment variable:

```bash
export MENHIR_CLIENT_TOKENS_ENABLED=1
```

The client-token tier counts as an **authenticated mode** for the bind-safety guard.
When enabled, the server may bind to a non-loopback host (e.g. `0.0.0.0` or a LAN
interface) **without** needing `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH`. Without this
variable, a non-loopback bind is rejected unless the unsafe override is set.

## 3. Bootstrap the first token (trust on first use)

The very first token must be minted on the **local machine** with the server bound to a
loopback address (`127.0.0.1`, `localhost`, or `::1`). While the client-token store is
**empty**, the loopbound `/api/admin/clients` endpoint accepts a `POST` with **no
credential** — this is trust on first use (TOFU).

```bash
curl -X POST http://127.0.0.1:8100/api/admin/clients \
  -H "Content-Type: application/json" \
  -d '{"client_name":"my-operator","tier":"operator"}'
```

Expected response (HTTP 200):

```json
{
  "client_id": "id-here",
  "client_name": "my-operator",
  "tier": "operator",
  "token": "the-raw-opaque-token-shown-once"
}
```

**Save the `token` value immediately** — it is returned once and never retrievable
again. Tokens are stored only as SHA-256 hashes.

Valid tiers:

| Tier | Description |
| --- | --- |
| `operator` | Full access, can mint and revoke clients, all MCP tools |
| `agent` | Standard agent access (write MCP tools) |
| `readonly` | Read-only MCP access |

## 4. Adding more clients

After the first token exists, minting additional clients **requires** a credential — either:

- The `MENHIR_OPERATOR_KEY` environment variable value, OR
- An operator-tier client token in the `Authorization` header.

```bash
curl -X POST http://HOST:8100/api/admin/clients \
  -H "Authorization: Bearer <operator-token-or-key>" \
  -H "Content-Type: application/json" \
  -d '{"client_name":"claude-desktop","tier":"agent"}'
```

Replace `HOST` with the server's address. The response returns the new raw token once:

```json
{
  "client_id": "new-uuid",
  "client_name": "claude-desktop",
  "tier": "agent",
  "token": "new-raw-token-save-this"
}
```

## 5. Using a client token

Clients authenticate by sending the token in one of two ways:

**Authorization header (preferred):**

```bash
curl -H "Authorization: Bearer <token>" http://HOST:8100/mcp
```

**Query-string fallback (`/mcp` paths only):**

```bash
curl "http://HOST:8100/mcp?api_key=<token>"
```

Identity and tier always come from the server-side registry — any
`x-menhir-client-name`, `x-menhir-client-id`, or `x-menhir-session-id` headers sent by the
client are **ignored**. The token alone determines who the client is and what it can do.

Tier-to-access mapping:

| Token tier | Effective access |
| --- | --- |
| `operator` | Full administrative and MCP access |
| `agent` | Standard write MCP tool access |
| `readonly` | Read-only MCP tool access |

## 6. Revoking a client token

Use `POST /api/admin/clients/{client_id}/revoke` with an operator credential.

```bash
curl -X POST http://HOST:8100/api/admin/clients/<CLIENT_ID>/revoke \
  -H "Authorization: Bearer <operator-token-or-key>"
```

Expected response (HTTP 200):

```json
{
  "client_id": "<CLIENT_ID>",
  "revoked": true
}
```

The revoked token is immediately rejected on all subsequent requests (HTTP 401).
Revocation is permanent — there is no un-revoke.

## 7. Recovery / reset

If you lose all tokens **and** no `MENHIR_OPERATOR_KEY` is set, you can reset the
store:

### Option A: Delete the database file

1. Stop the server.
2. Delete `client_tokens.db` (located in the `.agent` directory by default, or under
   `MENHIR_OAUTH_AS_DIR` if that variable is set).
3. Restart the server.
4. Re-bootstrap the first token via loopback (see section 3).

### Option B: Revoke the last active token (if you still have one)

If you have at least one operator token that is still active, use it to revoke itself
or any other token. Once all tokens are revoked, the store has no active tokens, and
the loopback bootstrap endpoint re-opens (TOFU resets). This avoids deleting the
database.

## 8. Security notes

- **Token storage:** Raw tokens are stored only as SHA-256 hashes. The plaintext token
  is shown exactly once, at mint time. If you lose it, you must revoke and re-mint.
- **Loopback-admin trust:** The TOFU bootstrap endpoint only works when the server is
  **loopback-bound** (the `api_host` is `127.0.0.1`, `localhost`, or `::1`). This
  ensures only local processes can mint the first token.
- **Reverse proxy caveat (network bind):** Loopback-admin is gated on the server's *bind*
  address, not only the request's origin. When the server binds a non-loopback host (e.g.
  `0.0.0.0`), the loopback-origin bootstrap path is disabled entirely — so a request
  arriving via a **same-host** reverse proxy (which would appear to come from `127.0.0.1`)
  is **not** granted loopback admin. On such a deployment, mint/revoke require the operator
  key or an operator-tier token.
- **Reverse proxy caveat (loopback bind behind a proxy) — read this before a VPS deploy:**
  The common public topology for the embedded OAuth AS is uvicorn bound to `127.0.0.1`
  behind a **same-host** TLS-terminating reverse proxy (nginx/caddy), because the AS needs
  an `https` public base URL. In that topology `loopback_bound` is **true**, and every
  proxied external request presents peer `127.0.0.1`, which would otherwise satisfy the
  loopback bootstrap check. Two protections apply:
  1. **Forwarding-header guard (automatic).** Bootstrap minting is refused when the request
     carries any reverse-proxy forwarding header (`X-Forwarded-For`, `X-Real-IP`,
     `Forwarded`). A genuine local `curl` never sets these; a proxy always appends one the
     external caller cannot strip. Ensure your proxy is configured to add a forwarding
     header (nginx `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`).
  2. **Operational discipline (yours).** Do **not** rely on the guard alone:
     - **Pre-mint the first operator token (or set `MENHIR_OPERATOR_KEY`) *before* wiring
       the proxy**, while the server is still reachable only from true loopback.
     - **Never revoke the last active token on a proxied deployment.** Revocation re-opens
       the bootstrap window (TOFU reset); on a proxied host that window is only protected by
       the forwarding-header guard. If you must reset, do it while the proxy is detached and
       the server is reachable only from loopback.
  Behind a same-host proxy, also set `MENHIR_TRUSTED_PROXY=1` so the AS rate limits key on
  the real client IP (from the proxy's forwarded header) instead of collapsing every caller
  onto the shared `127.0.0.1` peer address.
- **Operator key vs. operator token:** The `MENHIR_OPERATOR_KEY` environment variable
  is a static key that works as a credential for all admin operations. An operator-tier
  client token is a minted token with the same authority. Either can be used for
  minting and revoking.
- **No token expiry:** This tier does not implement token expiry or rotation. Revocation
  is the only way to invalidate a token once minted.
