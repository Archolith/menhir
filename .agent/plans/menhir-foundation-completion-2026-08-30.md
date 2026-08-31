---
artifact_schema: 1
artifact_uuid: 0e3a9f02-b3d8-4698-aa42-cf604433b321
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation abstraction completion

## Decision

Finish the core-promotion work as a staged extension foundation, not as a rewrite or a dynamic
plugin framework. The current stack supplies the semantic contracts and most lifecycle correctness.
The remaining work must prove that unrelated domains can use those contracts through production
composition, durable assertions, scheduled materialization, and a small documented developer
surface without adding their vocabulary to Menhir Core.

This plan is the execution router. Detailed ownership lives in:

1. [Phase 1 — extension substrate](menhir-foundation-phase-1-extension-substrate-2026-08-30.md)
2. [Phase 2 — runtime orchestration](menhir-foundation-phase-2-runtime-orchestration-2026-08-30.md)
3. [Phase 3 — hostile-domain proofs](menhir-foundation-phase-3-hostile-domain-proofs-2026-08-30.md)
4. [Phase 4 — developer surface and cutover](menhir-foundation-phase-4-developer-surface-and-cutover-2026-08-30.md)

## Why

The promotion stack already contains injectable View and evidence kinds, source-bound admission
contracts, projection definitions, generation-fenced lifecycle state, freshness receipts, coverage,
and one scalar materializer. It does not yet provide the complete host path needed by another
domain: production admission is not bound to a generic assertion write, lifecycle work is not
scheduled, the only durable projection adapter is scalar-specific, and the investigation and
personality examples stop at pure tests.

The next unit of progress is therefore not another isolated protocol. It is one end-to-end path
that can be reused without weakening existing scalar/coding behavior. The authority chain must be
singular and durable: the authoritative admission transaction writes both the immutable assertion
and its ordered mutation-journal record; the projection host consumes that journal; the lifecycle
commit installs and certifies the View; and production cutover grants exactly one writer authority
for each promoted definition or cohort.

## Baseline and non-goals

Baseline: the stacked PR sequence `#11 → #12 → #13 → #14 → #15 → #16 → #18 → #19`, ending at
`7145e52d`. The stack is mergeable and its projection-focused and real-Neo4j validation is green;
the repository-wide offline job remains red on the same nine `release.json` contract failures as
`main`. Repairing that independent release baseline is required for an ordinary green merge but is
not abstraction scope.

Out of scope until the four phases complete:

- untrusted or remotely downloaded plugins;
- runtime hot-loading or unloading of extension code;
- a general query language for arbitrary beliefs;
- higher-order belief diffs, counterfactual branches, or semantic-version comparisons;
- physical canonicalization of legacy default-namespace View keys;
- replacing Graphiti or rewriting the existing scalar/event systems wholesale.

## Target architecture

```text
trusted host composition
  ├─ evidence kinds
  ├─ admission policies
  ├─ assertion codecs/repositories
  ├─ projection definitions
  ├─ View kinds/materializers
  └─ installed-state hash adapters
             │
durable evidence → atomic admission + immutable assertion + ordered mutation journal
             → dirty target generation → fenced materialization
             → freshness certificate → bounded retrieval
```

Core owns namespace-bound identity, provenance binding, immutable envelopes and admission receipts,
the ordered mutation journal, definition publication, generations, atomic fencing, lifecycle
receipts, freshness, corruption diagnostics, and bounded execution. Extensions own vocabulary,
payload validation, target derivation, fold meaning, abstention/retirement rules, View payloads, and
installed-state hash semantics. The host owns the trusted mapping from assertion type and purpose to
source resolver, grant authority, policy, projection adapter, and deployed behavior digest.

## Phase sequence and gates

| Phase | Deliverable | Depends on | Exit gate | Estimate |
|---|---|---|---|---|
| 0 | Land and reconcile the current stack | current PR chain | stack merged in order; unrelated CI baseline separately owned | 1–2 days plus CI ownership |
| 1 | Durable extension substrate | phase 0 | a namespace-bound non-scalar assertion and immutable admission decision are atomically persisted with an ordered mutation-journal record, then replayed without a second authority | 2–3 weeks |
| 2 | Generic runtime orchestration | phase 1 | behavior-bound publication, journal consumption, bounded fair workers, temporal wakeups, atomic commit, recovery, and telemetry work for a test adapter while scalar remains read-only shadow | 2–3 weeks |
| 3 | Investigation and personality proofs | phase 2 | two materially different semantic algebras run end to end and coexist through a frozen provisional public seam without domain vocabulary or switches in core | 2–3 weeks |
| 4 | Developer surface and production cutover | phase 3 | an external-in-repo extension is clean-installable, documented, testable, versioned, and activated through an exclusive, attested, reversible writer-authority handoff | 3–4 weeks |

Expected focused effort: 9–13 engineer-weeks for a production foundation. A useful internal alpha
exists after Phase 2 and the first investigation slice, approximately 5–7 weeks after Phase 0. The
estimate excludes any prerequisite physical default-namespace migration discovered by Phase 4's
inventory and excludes the unrelated release-baseline repair.

## Cross-phase invariants

1. Existing scalar, event-history, coding, recall, and admission defaults remain unchanged until a
   separately measured cutover stage says otherwise. Until then, every assertion type has exactly
   one documented writer, reader, currentness authority, and admission model.
2. No request payload may choose its own admitted authority. The host-selected durable source and
   grant mechanisms and core ceiling remain authoritative. An upward request is recorded and
   clamped under the existing contract; extension policy may reject, reinterpret, or lower it.
3. Canonical namespace participates in every source, grant, assertion, current-set, content-hash,
   uniqueness, journal, projection-target, and fence identity. Alias-aware legacy reads must never
   make a cross-namespace or ambiguous match authoritative.
4. A source observation remains distinct from a current belief. Re-folding may replace or retire a
   View but must not erase the evidence, immutable admission decision, assertion, or derivation
   lineage that produced it.
5. The assertion transaction is the sole producer of the ordered mutation journal. Immediate
   dispatch is a latency optimization only. A consumer advances its durable cursor only after
   idempotent dirty/retire application, and historical scans are snapshot-fenced.
6. Every projection mutation must occur inside `ProjectionLifecycleRepository.commit`, using its
   supplied transaction and returning the hash of the adapter's explicitly declared certification
   surface. Required state rolls back together; advisory state cannot be silently certified.
7. Namespace aliases may share logical reads and fences without silently changing physical View
   identity. Any physical migration requires its own expand/backfill/drain/verify/enforce/contract
   sequence and may become a prerequisite if physical-key preservation cannot be proven.
8. Runtime behavior publication and extension registration are deterministic and fail closed on
   collisions, missing definitions or dependencies, incompatible versions, behavior-digest drift,
   ambiguous key-space ownership, or materializer/hash mismatch.
9. The first two hostile domains use a commit-pinned provisional public seam established before
   Phase 3. A required core edit is a missing-seam finding, lands separately with generic regression
   proof, and resets the zero-core-diff baseline.
10. Production writer authority is durable and mutually exclusive. Both legacy/direct and lifecycle
    transactions check the same per-definition or cohort fence; after the first lifecycle mutation,
    rollback is roll-forward or a verified reverse generation, never blind old-image restoration.

## Branch and review strategy

- Keep each phase in a short stacked series whose first PR is the contract and whose final PR is
  the executable proof. Do not place all remaining work in one long-lived branch.
- Phase 1 should separate the storage decision from production wiring.
- Phase 1 owns the atomic mutation-journal producer; Phase 2 owns its consumer and scheduling.
- Phase 2 should separate behavior-manifest publication, dirty routing, temporal work, worker
  execution, and scheduler wiring. It also freezes the provisional extension import seam used by
  Phase 3 without claiming final package stability.
- Phase 3 should keep investigation and personality in separate PRs so the second can expose
  accidental special-casing in the first.
- Phase 4 should separate developer-surface stabilization from activation and cleanup.
- Every phase closes with an implementation report that names commits, test evidence, documents,
  compatibility behavior, and remaining gaps.

## Document ownership matrix

| Phase | Contract documents to create or update |
|---|---|
| 1 | `.agent/architecture.md`, `.agent/data_models.md`, `.agent/memory-governance.md`, assertion/admission/current-set ADR, writer-ownership manifest, mutation-journal contract, `CHANGELOG.md` |
| 2 | `.agent/architecture.md`, `.agent/data_models.md`, runtime behavior-manifest contract, scheduler and temporal-work protocols, operations runbook, provisional import allowlist, default-off registry, `CHANGELOG.md` |
| 3 | investigation and personality extension examples, extension test guide, architecture boundary notes, `CHANGELOG.md` |
| 4 | public extension guide, compatibility/versioning policy, migration runbook, production acceptance report, `CHANGELOG.md` |

### Documentation status — 2026-08-30

- Canonical architecture, data-model, governance, operations, plan-routing, and agent-usage docs
  describe this foundation only as a planned implementation target and preserve the current legacy
  scalar/event authority boundary.
- Proposed ADR 0002 fixes the candidate assertion/currentness/journal design. Phase 1 implementation
  remains blocked until the owner accepts or amends it.
- The stable extension API reference, template, compatibility policy, migration runbook, production
  receipts, and implementation reports remain phase deliverables; planning prose is not substituted
  for those artifacts.
- The shipped default-off activation ledger intentionally has no generic foundation entry. Add one
  only after code is complete and an actual activation flag/gate exists.

## Whole-program acceptance

The abstraction process is complete when all of the following are true:

- scalar/coding, investigation, and personality use one host composition and lifecycle pipeline;
- investigation and personality contain no vocabulary in core modules and require no central
  switch edits;
- replay, backfill, version change, removal, crash, retry, and stale-worker interleavings are tested;
- every installed projection can be assessed as fresh, stale, unavailable, or corrupt from durable
  evidence rather than queue emptiness, without one corrupt target aborting unrelated assessment;
- an extension can live outside `src/menhir`, install from both a wheel and sdist in a clean
  environment, and register using documented public imports only;
- production shadow results demonstrate parity before any existing writer is disabled;
- every rollout stage produces a durable release-bound receipt with exact watermarks, target digest,
  counts, workers, error denominator, and observation interval;
- writer census, mixed-version fencing, atomic authority flip, and bypass-refusal proofs complete
  before lifecycle writes become authoritative;
- rollback procedures are tested and preserve evidence, assertions, before-images, and immutable
  receipts without restoring an unfenced writer;
- compatibility paths are removed only after the Phase 4 Contract window proves zero legacy write
  attempts and zero compatibility-read hits, with backup and owner approval.

Higher-order belief/provenance features remain a subsequent roadmap and are not completion gates.
