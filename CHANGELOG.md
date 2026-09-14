## 2026-09-14 - complete relation extraction and repair checks after the release merge

- Limited canonical-self relation guidance to first-person text, leaving third-person extraction on
  Graphiti's native prompt and preserving named people as their own typed-scalar subjects.
- Excluded derived Menhir Views from Graphiti's semantic identity candidates so later turns cannot
  attach ordinary relationships to projection nodes and corrupt their evidence provenance.
- Made unchanged-FACT provenance refusals report the exact failed gate, including stale MENTIONS
  parity and contributor scope, lifecycle, and fence-generation details.

- Kept host-only Testinfra assertions out of the ordinary product suite when their external
  operator toolchain is absent, while preserving their documented explicit invocation.
- Updated stale View-provenance, namespace-fence, reasoning-control, and Linux runner contracts to
  exercise the newly published behavior and arguments.
- Registered the feature-flag plan with stable artifact metadata and the active plan index.

## 2026-09-08 - transact first-backup bootstrap before release installation

- Moved first encrypted-backup bootstrap out of the desktop wrapper and into the root release
  installer's already-bound, locked, snapshotted, durably journaled transaction.
- Added cleanup-resume-before-recount behavior, fixed verified helper overlays through the same
  atomic install primitive as the full release, and fail-closed inherited FD 9 lock validation.
- Added bootstrap-phase rollback/recovery and ordering contracts so candidate helpers cannot become
  a retry baseline and general release authority remains unchanged until the backup is complete.

## 2026-09-07 - define the staged promotion deployment model

- Bound every privileged lane to the approved release, bundle, root runner, and staged Cloudflared
  identity before mutation; added durable root-receipt adoption and no-replay completed recovery.
- Made image evidence executable policy: Syft and Grype now inspect the exact sealed archive, critical
  findings fail publication, clean CI builds the frozen wheelhouse, and publication emits a
  digest-only reference through a collision-resistant candidate tag.
- Made scaffold convergence preflighted and transactional, including an independent trusted-copy
  digest check, sudoers validation, prior file/unit snapshots, and automatic rollback.
- Made production a promotion-only target: the exact finalized image must first pass one complete
  production-equivalent staging workflow with isolated data, OAuth/MCP behavior, restart, and
  automatic rollback evidence.
- Limited routine app and non-migrating security-configuration cutovers to one approval, bounded
  replacement, read-only public canary, and automatic prior-image rollback.
- Reserved fresh backups, restore rehearsals, full authority comparisons, and writer replacement for
  mechanically classified maintenance or recovery releases, and blocked further production releases
  until the staging receipt is enforced end to end.
- Separated product publication from personal deployment: publication produces immutable consumable
  artifacts without production access, while personal deployment selects one published digest and
  cannot rebuild or republish it.

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
