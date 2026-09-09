## 2026-09-09 - add the Graphiti release monitor

- Added a daily, manually dry-runnable GitHub workflow that compares Menhir's exact `graphiti-core`
  pin with stable core releases and opens or refreshes a version-specific upgrade-plan issue only
  when the repository-owned materiality policy finds relevant coupling.
- Added deterministic offline assessment and coverage for core-vs-MCP tag filtering, exact-pin
  enforcement, quiet patch behavior, release-line changes, issue-plan generation, and policy paths.
- Documented the monitor's permissions, credentials, guardrails, testing, and complementary role
  alongside the existing ChatGPT Graphiti Pin Watch.

## 2026-09-07 - define the staged promotion deployment model

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
