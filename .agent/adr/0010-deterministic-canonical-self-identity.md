# ADR 0010 — Deterministic Canonical Self Identity

- **Status:** ACCEPTED as target architecture (2026-09-21). Enforcement remains default-off and
  historical fork consolidation remains unauthorized pending owner decisions.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/architecture.md`, `.agent/default-off-features.md`,
  `.agent/workflows/canonical-self-migration-runbook.md`,
  `.agent/reports/canonical-self-production-observation-2026-09-04.md`

## Context

Graphiti normally resolves extracted entities through candidate search and, when needed, an LLM.
For the human behind a Menhir namespace, that probabilistic boundary created many entities named
`user`: the exact-name candidate window saturated, resolution repeatedly fell through to model
judgment, and one identity fragmented across the graph.

The opposite shortcut is also unsafe. A name such as `user`, or even first-person grammar, cannot
prove an extracted node is the human. A trusted user turn may discuss an application user or quote
another speaker. Incorrectly binding such a node to the canonical identity is harder to repair than
leaving an uncertain node unresolved.

## Decision drivers

- One human identity must not fork because of probabilistic entity resolution.
- Names, grammar, and model output are not identity authority.
- Authorship of an episode and subjecthood of one extracted node are distinct claims.
- Namespace identity must be deterministic without a database lookup.
- Binding must not cross logical namespaces or leave a partially rewritten payload.
- Activation changes durable write semantics and therefore needs explicit evidence and approval.
- Historical cleanup must preserve ambiguous entities and relationships rather than guess or delete.

## Decision

Menhir defines exactly one canonical human-self identity per **logical namespace** and derives it
only through `self_uuid_for_namespace(namespace)`.

The following invariants define the identity boundary:

1. **One formula, no lookup.** The canonical UUID is
   `uuid5(NAMESPACE_URL, "menhir-self:<normalized logical namespace>")`. Writers and readers call
   the shared domain function; they do not copy the formula or discover identity by retrieval.
2. **Logical identity and physical partition are separate.** Identity is keyed by the normalized
   logical namespace. Graphiti `group_id` is derived independently through
   `namespace_to_group_id`; logical `default` maps to physical `""` and is never inferred backward.
3. **A name is never authority.** `user`, first-person aliases, display names, grammar, similarity,
   and model-selected identifiers may identify candidates for observation but cannot prove self.
4. **Authorship is not subjecthood.** Binding requires both trusted Menhir-owned evidence that the
   episode is an admitted human turn and a declaration naming the exact in-memory subject node.
5. **The declaration precedes model output.** The production path constructs a receipt-owned author
   endpoint from an atomically verified, byte-identical evidence projection and declares that node
   before extraction. Model output may attach inferred relationships to it but cannot issue or move
   identity authority.
6. **Binding is atomic and pre-resolution.** In `enforce`, the proven node, both edge directions,
   marker-bearing edge text, and episode index map are rewritten together before candidate
   acquisition. Any partial failure rolls the payload back and refuses the attempt.
7. **Canonical self is isolated.** The proven node bypasses exact, similarity, LLM, and override
   resolution. Undeclared nodes cannot acquire the canonical UUID through those paths, and a
   canonical node is ineligible for correlation merge in either direction.
8. **Ambiguity fails closed and visibly.** Missing, duplicated, stale, foreign-episode,
   cross-partition, colliding, or otherwise inconsistent declarations produce no partial graph
   write. Raw evidence remains available for retry or review.
9. **Identity does not certify facts.** The fixed endpoint proves who the endpoint is, not whether a
   model correctly interpreted negation, reported speech, questions, or relationship semantics.
10. **Runtime prevention does not perform historical consolidation.** Normal writes may create or
    protect the canonical target and report forks, but they never absorb or delete old forks.

Rollout retains three modes: `off` preserves pre-change behavior, `observe` records bounded
non-mutating decisions, and `enforce` applies the identity boundary. Unknown values fail safe to
`off`. This ADR accepts the target and safety fences; it does not activate `enforce`.

## Considered alternatives

### Resolve self by the entity name `user`

Rejected. Software actors, account concepts, and third-person references legitimately use that
name, and a name carries no provenance.

### Treat first-person grammar as node-level proof

Rejected. Reported speech is a direct counterexample, and extraction has already discarded the
source spans needed to disambiguate it.

### Let semantic retrieval or the dedup LLM select the human

Rejected. That is the probabilistic boundary that produced the historical forks.

### Bind every entity from a trusted user-authored episode

Rejected. Authorship proves who supplied the episode, not which entities the episode discusses.

### Merge historical forks during an ordinary write

Rejected. A name-based bulk rewrite can absorb generic users, lose relationships, and delete the
evidence needed to reverse a mistake.

## Consequences

- First-person recall can address the namespace owner deterministically without graph discovery.
- Enforced ingestion removes proven self from probabilistic deduplication.
- Some self-like references remain unresolved or withheld when node-level proof is absent; this is
  the intentional cost of preventing false identity merges.
- Relationship attribution remains model-derived and may still need correction through normal
  evidence and admission paths.
- The implementation carries rollback, namespace, persistence, candidate-isolation, merge-immunity,
  producer-census, and privacy-safe telemetry obligations.
- Off, observe, and enforce behavior must remain distinguishable in tests and operations.

## Open owner decisions

1. **Production activation:** which exact release may run `enforce`, after which production-model
   corpus, persistence E2E, CI, release-classification, and post-deploy canary evidence.
2. **Repository default:** whether successful deployment evidence should eventually change the
   default from `off`, or retain explicit per-deployment activation.
3. **Historical fork disposition:** the owner-approved manifest, treatment of ambiguous nodes and
   relationships, reversible migration design, and delete barrier for existing forks.
4. **Declaration authority expansion:** whether any producer beyond the current receipt-owned
   automatic-memory path may declare an exact self subject, and what equivalent evidence it must
   present.
5. **Richer attribution:** whether future source-span or speaker-attribution evidence should reduce
   withheld/unresolved references without widening identity authority from text alone.

## Evidence in the repository

- `src/menhir/domain/self_identity.py`
- `src/menhir/infrastructure/self_binding.py`
- `src/menhir/infrastructure/graphiti_extraction_patches.py`
- `src/menhir/infrastructure/graphiti_model_patches.py`
- `src/menhir/services/enrichment_steps.py`
- `tests/test_self_identity.py`
- `tests/test_self_binding.py`
- `tests/test_self_identity_producer_census.py`
- `tests/test_canonical_self_endpoint_e2e.py`
- `.agent/workflows/canonical-self-migration-runbook.md`
- `.agent/reports/canonical-self-production-observation-2026-09-04.md`

## Non-goals

This ADR does not activate canonical-self binding, authorize historical deletion or consolidation,
certify model-derived facts as owner-confirmed, or unify the separate typed-scalar and event-history
subject rules.
