## 2026-09-03 - surface refile lineage where agents actually meet todos

- `list_todos` rows now carry `supersedes_count`, and both renderers that consume
  them show it: the MCP tool prints "refile of N earlier todo(s)" with a footer
  pointing at `get_todo`, and the session-start hook appends "(refile of N)".
- Closes the half of the supersession feature that the earlier fix missed. `get_todo`
  renders the full lineage, but nothing reaches `get_todo` unless something first says
  there is a lineage to look up -- and `list_todos` is the pinned discovery tool, the
  session-start hook, and the bootstrap recall path. A refiled todo appeared in all
  three as brand-new work, so the prior attempt's context sat behind a uuid no agent
  had a reason to ask for.
- A count rather than the uuids: listings are token-sensitive and the marker only has
  to prompt the drill-down. Predecessors are counted in Python and scoped to the
  caller's silo, for the same reason the lineage read is.

## 2026-09-03 - fix ten review findings in the todo supersession surface

- **`get_todo` now actually prints the lineage.** The repository attached a
  `supersession` block and `GetTodoTool` had no branch for it, so the SUPERSEDED_BY
  edge had no agent-facing reader -- the CF-143 dead-edge shape the feature was
  built to avoid. The same-day claim that it had a reader was wrong.
- **Cycles were reachable and are now blocked twice.** `supersede(A,B)` ->
  `reopen(A)` -> `supersede(B,A)` built A->B->A, because only the OLD todo was
  guarded against having a successor and `reopen_todo` returned a superseded todo
  to open without clearing its edge. `supersede_todo` now guards the NEW todo too,
  and `reopen_todo` refuses a superseded todo outright.
- **The lineage reader no longer drops data.** Two successors (which concurrent
  supersessions can still produce) previously became two rows behind an unordered
  `LIMIT 1`, silently discarding one; `superseded_by` is now a list, and `get_todo`
  warns when it holds more than one.
- **Backend-boundary ownership guard.** The four todo ops are reachable through the
  generic `/api/internal/backend/{operation}` dispatch, which injects a namespace
  only into methods declaring one -- none of these do. They now call
  `_require_own_todo` / `_require_own_memory` at the backend, not only in the tools.
- Lineage reads are scoped to the caller's silo; the relation whitelist has one
  definition instead of two; refusals name which precondition failed; and both
  namespace comparisons are coalesced. The inverted namespace rule versus
  `supersede_artifact` is documented rather than changed.

Verified: 8483 passed, 347 skipped. The Cypher remains unexecuted by tests (stubbed
driver); the cycle sequence was replayed through the real methods against a
predicate-evaluating fake to confirm both guards compose.

## 2026-09-03 - todo lifecycle and refile lineage reach the MCP surface

- Added `supersede_todo`: closes a todo and writes a `SUPERSEDED_BY` edge to its
  replacement in one statement. Menhir has no update path, so editing a todo means
  closing it and adding a new one; until now that lineage was lost. The edge is the
  first todo-to-todo relationship and the exception to the inward-only rule is
  recorded at its definition -- supersession is an identity fact, not a knowledge
  claim, and a todo still never becomes a semantic object.
- Exposed `resolve_todo`, `reopen_todo`, and `link_memory_to_todo`, which were
  written, tested, and unreachable since slice 1: no MCP tool and no caller outside
  `TodoRepository`. All four run the ownership guard on every uuid they name.
- `get_todo` now returns a `supersession` block (`superseded_by`, `supersedes`), so
  the new edge has a reader rather than becoming the next CF-143 dead edge.
- Updated the production client policy for the four new tools and recomputed its
  canonical digest to
  `047fd945ea56033036a68a20f03eb9208f0127cff70a0cba59423b7e834420aa`.
  **`MENHIR_CLIENT_POLICY_DIGEST` must be updated on the deployed host before this
  ships, or startup fails closed. Independent security review and reauthorization of
  existing grants are still outstanding, per deploy/ACCESS_CONTRACT.md.**

## 2026-08-30 - position Menhir around provenance and governance

- Reframed the public README, runtime descriptions, CLI help, and agent template around
  inspectable evidence, code impact, lifecycle authority, artifact governance, and
  release provenance, with MCP described as an access surface rather than the product
  category.
- Added an evaluation posture for the LongMemEval-derived temporal subset that records
  its diagnostic limits, publication requirements, and effect on default-off decisions.
- Updated governance and model records to match current source defaults and the checked-in
  SBOM without presenting a historical `.env`, coverage snapshot, or benchmark as live
  deployment evidence.
- Updated package metadata to use the same provenance and governed-context description.

## 2026-08-30 - require an independent security review for every production release

- Added a two-phase release-authoring flow that emits the exact candidate
  authority digest for review and refuses final authoring without a matching
  independent `APPROVED` attestation.
- Bound the review to every release claim, including all four commits, evidence,
  rendered artifacts, image digests, policies, rollback anchors, secret versions,
  and installed artifacts; any drift invalidates approval.
- Made zero unresolved critical/high findings and complete security scope strict
  release-schema requirements inherited by bootstrap, backup, candidate,
  promotion, rollback, and runtime validation paths.
- Documented the permanent release gate and recorded the follow-up to replace
  opaque MCP internal errors with actionable subsystem-specific diagnostics.

## 2026-08-29 - close the live operations gateway source blockers

- Updated the live VPS playbook to the implemented fixed topology: local
  no-shell root-wrapper dispatch, a Docker-bridge-only gateway listener, exact
  Caddy peer admission, `/ops` TLS routing, and OAuth protected-resource
  discovery bound to the operations audience.
- Pinned the external `menhir-proxy` bootstrap command to subnet
  `172.30.0.0/24` and gateway `172.30.0.1`, matching the fixed host listener.
- Kept live activation explicitly unproven until the immutable four-repository
  release is installed and passes backup, candidate, route, authorization, and
  public negative-path acceptance on the VPS.

## 2026-08-29 - add a guarded live VPS deployment playbook

- Added the canonical ordered workflow for immutable four-repository Menhir
  releases, one-time host bootstrap, backup/rehearsal/candidate/route/promotion,
  acceptance, and phase-aware rollback.
- Documented two real first-bootstrap stop gates in the current operations
  gateway: its Linux service still dispatches through a Windows-only runner,
  and no reviewed TLS transport currently reaches its loopback listener.
- Updated the production environment example to the current digest-bound hosted
  operator policy and added contract tests to prevent workflow or digest drift.
- Removed the stale blanket rejection of admin-scoped production authorization.
  Exact client policy still controls the full scope set, so ChatGPT and Claude
  can complete their reviewed operator grant while narrower clients remain
  unable to request admin authority.

## 2026-08-29 - allow hosted operators to ingest documents and projects

- Added `ingest_document` and `ingest_project` to the exact ChatGPT and Claude operator policies.
  Hosted operators now receive 51 of 54 MCP tools; only `delete_namespace`, `mint_client`, and
  `revoke_client` remain denied.

## 2026-08-29 - promote hosted web clients to operator authority

- Promoted the separate ChatGPT and Claude OAuth clients to exact operator-tier grants with
  read, write, and admin scopes. Each receives 49 of the 54 MCP tools, including artifact,
  todo, conflict, scheduler, and scoped memory-administration operations.
- Kept namespace-wide deletion, host-filesystem ingestion, and client credential administration
  outside hosted connector authority. Bumped the consent-session schema so the scope elevation
  requires fresh operator authorization; old access and refresh tokens fail the exact scope check.

## 2026-08-29 - bind production rights and consent to each OAuth client

- Removed shared Agent Smith consent groups so every hosted and managed client requires an explicit
  approval and can carry an independent digest-bound tool policy.
- Kept hosted web clients on the reviewed memory, diagnostics, and structure surface. Narrowed
  Agent Smith clients to their documented workspace tools, including read-only `list_todos`, while
  retaining `add_memory_and_track` only for Codex because its generated config explicitly pins it.
- Rejected legacy or unknown client-policy fields, documented the authority boundary, and added
  regression coverage for different client rights and non-transitive consent. Versioned consent
  cookies invalidate the old group-capable format at deployment.

## 2026-08-29 - expose read-only project structure to production agents

- Added `query_structure` to the digest-bound production tool surface for hosted web and
  Agent Smith OAuth clients so connector sessions can inspect already-ingested repositories.
- Kept `ingest_project` denied; expanding the production connector does not grant a new graph-write
  path. Added policy assertions and documented the resulting authority boundary.

## 2026-08-29 - retire Reasonix OAuth authority

- Removed the archived Reasonix client from Agent Smith's published OAuth metadata and
  digest-bound production policy.
- Added regression coverage proving the retired client is neither published nor admitted by
  production policy while the remaining managed-client suite stays intact.

## 2026-08-28 - fix: complete Claude web refresh authorization

- OAuth protocol scopes are now separate from Menhir permission scopes, so
  `offline_access` can request a refresh token without becoming a new access tier.
- The dedicated Claude web registration alone declares `offline_access`; ChatGPT and every
  Agent Smith registration keep their existing exact scope contracts.
- Startup atomically upgrades the exact legacy Claude registration and refuses unknown or
  disabled protocol scopes before the service accepts traffic.
- Consent pages allow only the validated callback origin through CSP `form-action`, so
  Chromium can follow the authorization POST redirect instead of stranding issued codes.
