# Menhir production access contract

This is a deployment invariant, not optional setup guidance. The digest-bound
authority is `deploy/client-policy.production.json`; production startup and
release authoring fail closed when this document's contract is not represented
by that policy.

## One client data-plane endpoint

All ChatGPT, Codex, Claude, and OpenCode memory traffic uses exactly:

```text
https://memory.ctharvey.me/mcp-http
```

Do not configure `/mcp`, `/api/*`, a tunnel URL, a host-local URL, or
`/ops/mcp` as a memory client endpoint. `/ops/mcp` is a separate release-control
surface used only by the fixed deployment machinery; it is not an alternate
Menhir memory API.

## Identity, cryptographic proof, and authorization

Every client is an OAuth public client with its own immutable `client_id`.
Agent Smith clients use a client-specific HTTPS Client ID Metadata Document
(CIMD); hosted web clients use their policy-owned static client ID and exact
callback. Client IDs, callback registrations, token caches, and audit labels
must never be shared across products.

The required flow is OAuth 2.1 authorization code with PKCE S256:

1. The client presents its stable `client_id`, exact callback, PKCE challenge,
   requested scopes, and the canonical `/mcp-http` resource.
2. Menhir resolves the identity against the digest-bound production policy and
   requires explicit operator consent.
3. Menhir issues a short-lived, signed JWT access token bound to the client ID,
   resource audience, exact scopes, and tier. The resource server verifies that
   signature and every binding on every request.
4. Refresh tokens rotate per client. They are a protocol capability and do not
   expand scopes, tier, or tool access.
5. The policy's exact allow/deny tool partition is enforced both when tools are
   listed and when a tool is invoked.

Possessing a client ID is not authorization. The cryptographic proof is the
PKCE-bound code exchange plus Menhir's signed bearer token; the permissions are
the exact digest-bound policy entry for that client ID.

## Required product roles

| Product | Required role | Production identities |
| --- | --- | --- |
| ChatGPT | `operator` | `69c2cd871b488ff4` (`chatgpt-chat`) |
| Codex | `operator` | `agent-smith-codex` CIMD |
| Claude | `operator` | `6cf6322fa828bb72` (`claude-web`), `agent-smith-claude`, `agent-smith-wsl-claude` |
| OpenCode | `agent` | `agent-smith-opencode`, `agent-smith-wsl-opencode` |

An `operator` has `menhir:read`, `menhir:write`, and `menhir:admin`, plus the
full normal operator tool surface including `get_provenance`. The three
separate authority-boundary tools remain denied: `delete_namespace`,
`mint_client`, and `revoke_client`.

An `agent` has `menhir:read` and `menhir:write` and the bounded daily memory
surface: `add_memory`, `build_context`, `list_todos`, `query_structure`,
`read_flagged_memories`, `recall_context_memories`, and `recall_memories`.

`menhir-deploy-probe` is a non-product, read-only service identity used only for
post-replacement acceptance. It has exactly `menhir:read`, can call only
`recall_memories`, and receives a 60-second signed JWT minted in memory by the
root-owned deployment runner. It has no refresh token and no stored credential.

The contract covers host variants. A WSL Claude identity cannot remain an agent
while Claude is declared an operator, and a WSL OpenCode identity cannot be
promoted independently of OpenCode.

## Change and verification rule

Any endpoint, authentication, identity, role, scope, or tool-boundary change
must update the versioned access contract in `client-policy.production.json`,
recompute its canonical digest, update `production.env.example`, pass the
focused access/release tests, receive an independent security review, and be
classified `security-config`. Use the focused security-config deployment path when it
exists; until then, use the full maintenance `release-run.sh` transaction. An access
contract change can never be classified `app-only`. Do not repair drift by editing the
live OAuth database, issuing a broader token, or adding another endpoint.

After deployment, existing Codex and local Claude grants must reauthorize: old
tokens carry the former agent scopes/tier and are rejected by the exact policy
check. Live acceptance must prove the canonical endpoint, signed-token client
identity, expected role, and both an allowed and denied tool for each product.
