## 2026-09-06 - harden the stacked core projection promotion

- Added instance-local View and evidence registries plus source-bound admission and projection
  definition contracts while preserving the existing default vocabularies.
- Added durable projection lifecycle, coverage, realization, materialization, and reconciliation
  components with transaction-scoped fencing and fail-closed stale or corrupt state handling.
- Kept the new lifecycle opt-in: existing scalar rebuilding remains available, physical default-
  namespace storage is unchanged, and only typed-assertion reads canonicalize logical aliases.

## 2026-09-06 - tighten typed scalar identity before voting

- Canonicalized elapsed durations to seconds, explicit USD money to exact decimals, supported
  measurement units to closed lexical forms, and grounded clock times to 24-hour values before
  proposal identity and k-sample voting are computed.
- Made counts source-authoritative and integer-only, forced intrinsically unitless scalar kinds to
  blank units, normalized weekday/status casing, and rejected boolean values that contradict an
  unambiguous grounded source polarity.
- Preserved exact money values through durable JSON storage, hydration, folding, and View identity,
  with fail-closed behavior for absent, unknown, fractional, or ambiguous source constraints.

## 2026-09-06 - automate reviewed production release staging

- Added committed change fragments and deterministic Markdown/JSON release-note rendering so
  release history is staged alongside each production-impacting fix.
- Added strict four-repository release-spec generation and deterministic install-bundle creation,
  replacing the previous one-off release workspace scripts.
- Added a resumable `prepare -> review -> finalize -> deploy` coordinator that preserves the
  independent security-review gate, previews deployment by default, and requires the exact release
  ID plus an explicit execution flag before invoking the existing production transaction.

## 2026-09-06 - admit ChatGPT's stable CIMD identity

- Added `https://chatgpt.com/oauth/client.json` to the digest-bound ChatGPT
  operator policy while retaining the restored DCR identity during migration.
- Added an authorization regression test that combines ChatGPT's current CIMD
  metadata shape, stable callback, public-client method negotiation, and the
  real production policy.
- Updated the hosted-client access documentation and production policy digest
  to `a6c7cd4f061010415c9f68b66bb79b808eca49b8ed5df51495ff18de312a865c`.

## 2026-09-04 - add verified subject endpoints for canonical self

- Made the lease-acquiring episode claim atomically certify exact evidence-projection lineage,
  cardinality, role/declarant, content, namespace, and no-diff requirements.
- Added an enforce-only, episode-scoped author endpoint carried through Graphiti extraction and
  relationless repair, with deterministic envelope validation plus final current-episode edge/index
  validation before the sole production `declare_self_subject` call.
- Made canonical binding atomically rename the endpoint to `user` while preserving UUID, edge,
  index-map, and display-name rollback. Post-tool projections are queued immediately, and retries
  recover a durable pending projection left behind by an earlier queue exception.
- Added fail-closed unit coverage for malformed authority, retries, mixed RBAC `user` entities,
  prompt isolation, queue failures, legacy blank/default namespace equivalence, and the
  declaration-producer census.

## 2026-09-04 - align production OAuth and agent todo authority

- Restored `menhir:admin` to the production compose authorization-server scope surface so the
  digest-bound Codex, Claude, and ChatGPT operator grants can be issued.
- Granted every agent-tier client the complete todo workflow: list, read, add, close, and stale
  close, while retaining the existing agent OAuth tier and all non-todo denials.
- Made production startup fail closed when runtime scope/tier configuration cannot satisfy the
  canonical access contract, and added compose plus startup regression coverage.

## 2026-09-03 - add Utopia prior-art comparison

- Added a revision-pinned comparison of Utopia's governed bitemporal knowledge application against
  Menhir's code-linked evidence, repository structure, agent authority, and change-impact model.
- Recorded the novelty and category boundary, the ideas worth borrowing, the ideas to keep outside
  Menhir core, and a dependency-aware follow-up order.
- Updated the prior-art index to classify Utopia as the strongest adjacent comparison for enterprise
  world models rather than a direct replacement for Menhir's software-understanding center.

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

- **Binding now requires a DECLARED node-level subject, and nothing else qualifies.** Trusted
  evidence proves who AUTHORED an episode; it never proves which extracted entity that author is.
  Three successive rules tried to answer the second question from the entity's name -- the literal
  string `user`, then an arity guard, then first-person grammar -- and each has a counterexample
  inside a valid human turn, the last being reported speech (`She told me, "I will handle it"`
  extracts an `I` who is someone else). All three made the same mistake: treating a property of
  the extracted STRING as a fact about its PROVENANCE. Only `EXPLICIT_SELF_SUBJECT`, a trusted
  internal caller declaring the episode's subject to be the owner, now binds; two declared aliases
  in one payload raise `AmbiguousSelfBindingError` and write nothing.
  **No production producer emits that declaration, so the prevention path is inert**: `enforce`
  and `off` are currently behaviorally identical. That is deliberate -- correct and doing nothing
  beats plausible and occasionally catastrophic -- but it means preventing forks needs per-node
  subject provenance from extraction (each node's source span, and whether it is quoted speech),
  which does not exist. The `self_like_unresolved` outcome,
  `self_like_without_subject_authority`, and `first_person_unresolved` counters describe the
  unclassified population in observe mode; they deliberately do not predict which nodes are safe
  to bind. A structural census now fails on any new context constructor, factory call site, or
  executable `EXPLICIT_SELF_SUBJECT` reference.
- **A missing driver or a failed canonical-node read is no longer treated as "absent".** Graphiti saves with
  `SET n = $entity_data`, which replaces the property map, so falling back to the sparse extracted
  node on a transient driver error would let a later write erase the stored node's markers,
  provenance, flags and summary. Only `NodeNotFoundError` falls back now.
- **The first canonical node in a namespace is stamped** with `is_self`, `entity_role` and the
  logical namespace. It was previously created without them, and the generic ingest metadata stamp
  supplies neither, so no structural reader would have recognized the node just created.
- **Resolution telemetry now covers the LLM outcomes**, not only deterministic similarity:
  `llm_selected_candidate`, `llm_selected_new`, `no_candidates_new`, unresolved count,
  candidate-count bounds, embedding model and dimension. Per-candidate cosine scores are NOT
  recorded: graphiti's search ranks by score and then drops it, omitting `name_embedding` from the
  returned record and popping it from `attributes`, so a measurement taken here would silently
  measure nothing in production while looking like a metric. The saturation signature the RCA
  depends on stays visible in `candidate_count_max` and `multiple_exact_llm`. Dedupe-prompt sizes
  are recorded per batch and per section -- entities, candidates including their attributes, the
  episode, and previous episodes including their serialized timestamps.
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
