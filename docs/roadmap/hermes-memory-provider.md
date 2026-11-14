# Hermes Agent memory-provider adapter

Status: Phase 1 implementation on `feat/hermes-memory-provider` (2026-11-13).

## Why

- Make existing Menhir knowledge available inside Hermes without maintaining a second, divergent memory store.
- Give Hermes a narrow, recognizable read/write interface with a clear data-sharing boundary.

## Scope

- Ship a standalone, user-installable Hermes `MemoryProvider` under `integrations/hermes/menhir/`.
- Automatically inject bounded Menhir context for substantive prompts via `/api/context`.
- Offer explicit `menhir_recall` and opt-in `menhir_remember` tools via `/api/recall` and `/api/memory`.
- Configure a fixed Menhir REST endpoint, namespace, and credentials per Hermes profile.
- Keep transcript mirroring, automatic extraction, turn evidence, graph operations, and deletion **out** of Phase 1.

## Proposed design

The Hermes `MemoryManager` discovers a directory plugin and invokes the `MemoryProvider` interface.
The provider sends a single prompt query to Menhir's existing `/api/context` endpoint before an
agent turn; Hermes receives a small, clearly marked historical-context block. Explicit recall
returns compact ranked results with provenance and temporal markers. The explicit writer, disabled
by default, sends only the caller's chosen episode to Menhir as `source=hermes-agent`, `role=agent`,
with a deterministic idempotency key. Neither Hermes transcripts nor tool results are ingested
automatically. Server-side namespace pins remain authoritative.

Configuration is local to the Hermes profile; all data egress uses an operator-configured endpoint.
Loopback HTTP is allowed, remote transport requires HTTPS, and credentials stay in Hermes' scoped
secret store. A failing backend does not stop the Hermes turn, but explicit tool calls report errors.

## Alternatives considered

- **Hermes MCP tool collection:** easy to wire but misses its native pre-turn recall lifecycle.
- **Automatic transcript mirror:** writes too much unreviewed content, loses speaker provenance,
  and would bypass Menhir's intentional evidence-admission controls.
- **Native provider (chosen):** supports pre-turn context and explicit, auditable write intent.

## Risks

- Context retrieval adds an HTTP request to a turn (bounded by a short timeout).
- A user-selected namespace may not be the one their existing history occupies; the setup guide
  requires a recall check.
- Remote endpoint and token configuration are sensitive. No token is persisted in provider JSON.
- Phase 1 does not preserve complete transcripts or backfill Hermes' pre-existing memory files.

## Invariants

- No automatic write or transcript sync, including on session end or compression.
- No automatic elevation to user-attested evidence; explicit episodes are agent-authored.
- No cross-profile mutable globals, no cross-namespace tool argument override, and no stale recall
  results reused across sessions.
- Menhir REST response errors remain distinguishable from an empty result.

## Validation

- Offline provider tests with a fake Hermes SDK and mock HTTP transport: setup/profile isolation,
  automatic recall, empty/error cases, explicit recall/write, safety limits, idempotency and no sync.
- Static compilation; where possible test plugin registration against the Hermes SDK.
- Live verification is a separate opt-in step, requiring running Menhir and Hermes instances.

## Docs to update

- Provider-local README (install, configuration, privacy, validation).
- `docs/post-install.md` (link to supported optional integration).
- `.agent/architecture.md` (new external adapter surface).
- `CHANGELOG.md` (new feature).
