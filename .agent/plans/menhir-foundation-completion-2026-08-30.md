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
that can be reused without weakening existing scalar/coding behavior.

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
durable evidence → admitted immutable assertion → dirty target generation
             → fenced materialization → freshness certificate → bounded retrieval
```

Core owns identity, provenance binding, immutable envelopes, namespace isolation, definition
publication, generations, atomic fencing, receipts, freshness, and bounded execution. Extensions
own vocabulary, payload validation, target derivation, fold meaning, abstention/retirement rules,
View payloads, and installed-state hash semantics.

## Phase sequence and gates

| Phase | Deliverable | Depends on | Exit gate | Estimate |
|---|---|---|---|---|
| 0 | Land and reconcile the current stack | current PR chain | stack merged in order; unrelated CI baseline separately owned | 1–2 days plus CI ownership |
| 1 | Durable extension substrate | phase 0 | a non-scalar assertion is admitted, persisted, replayed, and routed using core contracts | 1–2 weeks |
| 2 | Generic runtime orchestration | phase 1 | startup publication, dirty discovery, bounded workers, atomic commit, recovery, and telemetry work for scalar plus a test adapter | 2–3 weeks |
| 3 | Investigation and personality proofs | phase 2 | both domains run end to end and coexist without domain vocabulary or switches in core | 2–3 weeks |
| 4 | Developer surface and production cutover | phase 3 | an external-in-repo extension is documented, testable, versioned, and activated through a measured reversible rollout | 2–3 weeks |

Expected focused effort: 7–10 engineer-weeks for a production foundation. A useful internal alpha
exists after Phase 2 and the first investigation slice, approximately 3–5 weeks after Phase 0.

## Cross-phase invariants

1. Existing scalar, event-history, coding, recall, and admission defaults remain unchanged until a
   separately measured cutover stage says otherwise.
2. No request payload may choose its own admitted authority. The durable source mechanism and
   core ceiling remain authoritative; extension policy may only reject, reinterpret, or lower it.
3. A source observation remains distinct from a current belief. Re-folding may replace or retire a
   View but must not erase the evidence or immutable assertion that produced it.
4. Every projection mutation must occur inside `ProjectionLifecycleRepository.commit`, using its
   supplied transaction and returning the hash of the exact persisted state.
5. Namespace aliases may share logical reads and fences without silently changing physical View
   identity. Any physical migration requires its own expand/backfill/drain/verify/enforce/contract
   sequence.
6. Extension registration is deterministic and fail-closed on collisions, missing dependencies,
   incompatible definition versions, or ambiguous materializer ownership.
7. The first two hostile domains must use public promoted seams. A required core edit is a missing-
   seam finding and must be reviewed before it is implemented.

## Branch and review strategy

- Keep each phase in a short stacked series whose first PR is the contract and whose final PR is
  the executable proof. Do not place all remaining work in one long-lived branch.
- Phase 1 should separate the storage decision from production wiring.
- Phase 2 should separate host composition, dirty routing, worker execution, and scheduler wiring.
- Phase 3 should keep investigation and personality in separate PRs so the second can expose
  accidental special-casing in the first.
- Phase 4 should separate developer-surface stabilization from activation and cleanup.
- Every phase closes with an implementation report that names commits, test evidence, documents,
  compatibility behavior, and remaining gaps.

## Document ownership matrix

| Phase | Contract documents to create or update |
|---|---|
| 1 | `.agent/architecture.md`, `.agent/data_models.md`, `.agent/memory-governance.md`, assertion/admission ADR, `CHANGELOG.md` |
| 2 | `.agent/architecture.md`, `.agent/data_models.md`, scheduler protocols, operations runbook, default-off registry, `CHANGELOG.md` |
| 3 | investigation and personality extension examples, extension test guide, architecture boundary notes, `CHANGELOG.md` |
| 4 | public extension guide, compatibility/versioning policy, migration runbook, production acceptance report, `CHANGELOG.md` |

## Whole-program acceptance

The abstraction process is complete when all of the following are true:

- scalar/coding, investigation, and personality use one host composition and lifecycle pipeline;
- investigation and personality contain no vocabulary in core modules and require no central
  switch edits;
- replay, backfill, version change, removal, crash, retry, and stale-worker interleavings are tested;
- every installed projection can be assessed as fresh, stale, unavailable, or corrupt from durable
  evidence rather than queue emptiness;
- an extension can live outside `src/menhir` and be registered with documented public imports;
- production shadow results demonstrate parity before any existing writer is disabled;
- rollback procedures are tested and preserve evidence, assertions, and before-images;
- compatibility paths are removed only after a measured drain proves they are unused.

Higher-order belief/provenance features remain a subsequent roadmap and are not completion gates.
