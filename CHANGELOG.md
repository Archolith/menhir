## 2026-09-04 - align production OAuth and agent todo authority

- Restored `menhir:admin` to the production compose authorization-server scope surface so the
  digest-bound Codex, Claude, and ChatGPT operator grants can be issued.
- Granted every agent-tier client the complete todo workflow: list, read, add, close, and stale
  close, while retaining the existing agent OAuth tier and all non-todo denials.
- Made production startup fail closed when runtime scope/tier configuration cannot satisfy the
  canonical access contract, and added compose plus startup regression coverage.
## 2026-09-03 - fix three faults found in the live production logs

- **`get_artifact_relationships` had never worked.** The adapter delegated to
  `_work_artifacts.get_artifact_relationships`; the repository defines the method as
  `artifact_relationships`. Every other delegation in the adapter matches its
  repository name, so this was a lone typo raising AttributeError on every call.
  Checked the remaining eleven delegations mechanically -- this was the only one.
- **Malformed dedupe output no longer fails the whole episode.** The identity gate
  reads the raw LLM response before Graphiti validates it, and gpt-4.1-nano returned
  an `entity_resolutions` entry that was a bare string. The resulting AttributeError
  propagated out of `add_episode`, leaving the content in the graph with no entities:
  `add_memory` reported success, retry classification marked it `manual_review`, and
  recall could never see it. The new guards mirror the fail-safe
  `PatchedNodeResolutions._drop_degenerate` already applies on the validation path, so
  both consumers of that output now agree on what malformed means.
- **`SCHEDULER_TRACE_DISABLED=1` turns off scheduler task tracing.** The scheduler is
  a developer-workstation service; production has none, so every lifecycle transition
  paid a 2s timeout to localhost:8082 and logged a WARNING. Tracing is observability
  only, and both network paths are now gated.

## 2026-09-04 - close six review findings on the canonical-self prevention path

- **Binding now requires node-level subject authority, not just a proven author.** Trusted
  evidence proves who AUTHORED an episode; it never proves which extracted entity that author is.
  A first-person name (`I`, `me`, `my`) in a trusted human turn does prove it, by grammar. A
  third-person `user` does not -- it is as likely to be an RBAC role, a `users` table, or the
  person a support turn is about -- so it now stays an ordinary entity and is counted under the
  new `ordinary_user_entity` outcome. Two proving nodes in one payload raise
  `AmbiguousSelfBindingError` and write nothing, leaving the episode retryable.
  This narrows the mechanism: third-person `user` extractions, which are most of the existing
  fork population, are no longer prevented at write time. Folding a foreign subject into the
  canonical identity is the one outcome no later migration can separate, so declining is the
  correct trade -- and `EXPLICIT_SELF_SUBJECT` is the declared extension point for a producer that
  can vouch for a third-person subject.
- **A missing driver or a failed canonical-node read is no longer treated as "absent".** Graphiti saves with
  `SET n = $entity_data`, which replaces the property map, so falling back to the sparse extracted
  node on a transient driver error would let a later write erase the stored node's markers,
  provenance, flags and summary. Only `NodeNotFoundError` falls back now.
- **The first canonical node in a namespace is stamped** with `is_self`, `entity_role` and the
  logical namespace. It was previously created without them, and the generic ingest metadata stamp
  supplies neither, so no structural reader would have recognized the node just created.
- **Resolution telemetry now covers the LLM outcomes**, not only deterministic similarity:
  `llm_selected_candidate`, `llm_selected_new`, `no_candidates_new`, unresolved count,
  candidate-count bounds, embedding model and dimension. Candidate cosine bounds are now
  MEASURED from the two name embeddings, since graphiti's search discards the score it ranked by,
  and the event reports how many pairs were measurable. Dedupe-prompt section sizes are recorded
  per batch.
- **An ambiguous refusal is now recorded before it raises**, and observe mode no longer fails
  the episode: the outcome an operator most needs during an observation window was the only one
  producing no telemetry.
- **`detect_self_forks` no longer writes.** It obtained its uuid by calling `ensure_self_entity`,
  which MERGEs, so a census mutated the graph it was inspecting.

## 2026-09-03 - bind the canonical self deterministically, before graphiti dedup

- Menhir now has one authoritative human-self entity per logical namespace, and no longer asks
  cosine search or an LLM to decide which node that is. New `domain/self_identity.py` owns the
  single UUID formula and the evidence contract; `infrastructure/self_binding.py` applies it.
- The name is never authority. Binding requires trusted metadata the ingestion boundary owns --
  a `user`/`manual` source that the admission gate GRANTED, meaning it verified Menhir-owned
  turn evidence with `role == "user"` and text grounded in that turn. An entity called `user`
  from an agent turn, a project scan or an imported document stays an ordinary semantic entity.
- Evidence needed no new field: it already survives the async queue in the episode's persisted
  `source`, because the gate rewrites ungrounded claims to `agent_inference` before persistence.
- A proven self is withheld from `_collect_candidate_nodes` entirely rather than skipped
  afterwards. Candidate search IS the mechanism that fragmented the identity: with 66 exact-name
  `user` nodes against a 15-candidate window, graphiti's deterministic single-match branch was
  arithmetically unreachable, so every extraction escalated to the LLM and a
  `duplicate_candidate_id = -1` verdict could mint another fork. Tests assert the calls do not
  happen, not merely that the resulting uuid is right.
- Three self-UUID derivations (one writer, two recall readers) now route through one helper, and
  a static guard fails if a second copy appears. Output is byte-identical to the formula already
  written into production data.
- `ensure_self_entity` is non-destructive. `_absorb_self_entity_forks` -- which bulk-rewired and
  `DETACH DELETE`d every same-named node as a side effect of an ordinary write, dropping
  fork-to-canonical edges as "split artifacts" -- is removed, not merely unreferenced. Forks are
  now reported as `SELF_FORKS_REQUIRE_MIGRATION`; consolidating them is an operator-only,
  journaled migration driven by an approved UUID manifest.
- Fixes the activation hazard that made the above unsafe: the canonical write stamped
  `group_id = <logical namespace>`, so a `default` namespace would have created the node in group
  `"default"` -- a partition holding none of the production data. Detection reads both spellings,
  since existing forks live under the wrong one.
- Adds privacy-safe observability: per-decision binding records and per-branch dedup counters
  (`unique_exact_bind`, `multiple_exact_llm`, `entropy_guard_skip`, ...), carrying enums, counts
  and UUIDs but never memory text or arbitrary entity names. The RCA could only infer which
  branch production took; it is now recorded.
- Ships dormant. `MENHIR_CANONICAL_SELF_BINDING_MODE` is `off | observe | enforce`, default
  `off`, and an unrecognized value falls back to `off`. Consolidating the existing forks remains
  blocked on an approved census and a restored-copy rehearsal.

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
  `09ede2c69a145ec551bcd51e037d8f825e6cc7fb211335450c1d736bb616d3b7`.
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
