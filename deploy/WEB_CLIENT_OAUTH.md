# Hosted web-client OAuth

`deploy/client-policy.production.json` is the source of truth for hosted web
connector identity, callback metadata, scopes, tier, namespace, and tool
authority. Production startup atomically seeds missing static public clients
from this digest-bound policy and refuses to start if an existing row drifts.

| Client | Policy label | OAuth client ID | Exact callback |
| --- | --- | --- | --- |
| ChatGPT web | `chatgpt-chat` | `69c2cd871b488ff4` | `https://chatgpt.com/connector_platform_oauth_redirect` |
| Claude.ai web | `claude-web` | `6cf6322fa828bb72` | `https://claude.ai/api/mcp/auth_callback` |

Both are public clients using authorization code plus PKCE S256. They have no
client secret and must never share a client ID, token cache, or audit label.
Their agent-tier tool surface includes the read-only `query_structure` tool so
hosted connectors can inspect an already-ingested project graph. The graph-write
`ingest_project` tool remains denied and requires an operator-controlled path.
`list_todos` remains denied to hosted web clients. Each client requires its own
operator consent; approving ChatGPT never approves Claude, or vice versa.

## Connection behavior

- ChatGPT may use its restored DCR registration. Menhir returns only the exact
  policy-owned identity whose callback and scopes match the request.
- In Claude.ai, add `https://memory.ctharvey.me/mcp-http` as a custom connector,
  open Advanced settings, enter client ID `6cf6322fa828bb72`, and leave the
  client-secret field empty.
- Claude requests `offline_access`. Menhir treats it strictly as a refresh-token
  protocol capability: it is added to the static OAuth registration when refresh
  is enabled, but it never appears in or expands the client's permission policy.
- Complete authorization flows one at a time. Enter the Menhir operator key on
  the consent page when prompted; the hosted connector receives OAuth tokens,
  not the operator key.
- Claude custom OAuth credentials are immutable after connector creation. To
  change the client ID, remove and re-add that connector.

Changing any registration or authority field requires recomputing the policy's
canonical SHA-256 digest and deploying the policy and configured digest as one
reviewed release.
