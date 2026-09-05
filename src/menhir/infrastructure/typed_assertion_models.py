"""`:TypedAssertion` durable store — the rebuild input for ScalarStateViews (Piece C, commit 1).

This repository ONLY persists and queries typed-scalar assertions; it writes NO Views (commit 2
adds the fold). Correctness properties (review C.1):

* **Atomic supersession.** Each source claim has a `(:TypedAssertionHead {source_key})-[:CURRENT]->
  (:TypedAssertion)`. One statement MERGEs the head (the lock), MERGEs the assertion, and moves the
  CURRENT pointer + supersedes the prior current — so a crash/concurrent write can never leave two
  current assertions for one claim (the failure class already fixed in ViewRepository).
* **Distinct claims stay distinct.** The head keys on the BINDING-STABLE `source_key`
  (source-grounded: episode + span + ordinal, NO subject_uuid and NO extracted semantics), so two
  claims in one episode do not collapse, the head survives merge rebinding, and a newer perceiver
  can correct the interpretation of the SAME claim and supersede it. (`claim_key`, which still
  includes subject_uuid, is retained only as a historical/index property.)
* **Supersession is strict-rank; same-version disagreement does not replace.** A newer
  `perceiver_version` (strictly higher rank) supersedes the claim's current assertion. A DIFFERENT
  value at the SAME (or lower) version does NOT supersede — it is stored non-current for audit, so
  stochastic re-extraction cannot flip current state. An idempotent re-write (same assertion_key)
  monotonically UPGRADES the stored evidence tier (agent -> user) but never downgrades it.
* **Grounded + bound or flagged.** The node write and its `(:Episodic)-[:GROUNDS]->` /
  `(:Entity)-[:HAS_ASSERTION]->` links happen in the SAME statement; if the episode or entity is
  not yet present, `binding_pending=true` records that instead of leaving a silently unlinked node.

No LLM runs here. All writes are deterministic given a `TypedAssertion` value object.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from menhir.domain.typed_assertion import IDENTITY_VERSION, TypedAssertion, normalize_scalar
from menhir.infrastructure.schema import get_scalar_state_activation_queries


class ScalarStateActivationError(RuntimeError):
    """Raised when scalar-state activation is attempted over a store holding :TypedAssertion or
    :TypedAssertionHead nodes whose identity_version is INCOMPATIBLE with this binary's contract —
    unstamped (v1), older, OR newer. Activation is EXACT-MATCH fresh-only: the source_key-anchored
    identity contract (head keyed by source_key, assertion_key built from source_key) has no
    in-place migration to or from any other version, so a rolled-back binary meeting newer-stamped
    nodes must fail closed exactly as it does for legacy nodes. The operator must run an explicit
    migration or purge the incompatible nodes (`purge_scalar_state_nodes`) before activation, so two
    identity spaces never coexist."""

    def __init__(self, incompatible_heads: int, incompatible_assertions: int) -> None:
        self.incompatible_heads = incompatible_heads
        self.incompatible_assertions = incompatible_assertions
        super().__init__(
            f"scalar-state activation refused: found {incompatible_heads} TypedAssertionHead and "
            f"{incompatible_assertions} TypedAssertion node(s) whose identity_version != "
            f"{IDENTITY_VERSION} (unstamped, older, or newer). Fresh-only activation requires an "
            f"exact contract match; run an explicit migration or purge_scalar_state_nodes() first."
        )

_RECORD_CYPHER = """
MERGE (h:TypedAssertionHead {source_key: $source_key})
  ON CREATE SET h.created_at = datetime(), h.namespace = $namespace,
                h.subject_uuid = $subject_uuid, h.claim_key = $claim_key,
                h.identity_version = $identity_version
// already-bound identity-mismatch (ScalarStateView C.4.4) — decided from the head's EXISTING current
// owner, BEFORE any node create / CURRENT move / supersession. The durable invariant belongs to the
// SOURCE CLAIM (head), across every assertion VERSION and interpretation: once a claim is durably
// bound to A, a record presenting ANY different subject identity — a newer perceiver_version, a
// changed value/slot, OR an unresolved `unbound:` sentinel — must NOT rebind it. Re-binding is the
// merge path's job (`rebind_assertions`), never a record's. Note: NOT gated on the presented entity
// existing (a sentinel counts as "different"), and computed from `cur` (the pre-existing current), so
// a brand-new higher-version node cannot escape the guard by lacking a binding_pending property yet.
WITH h
OPTIONAL MATCH (h)-[cr:CURRENT]->(cur:TypedAssertion)
WITH h, cr, cur,
     (cur IS NOT NULL AND NOT coalesce(cur.binding_pending, true)
      AND cur.subject_uuid <> $subject_uuid) AS binding_mismatch
MERGE (a:TypedAssertion {assertion_key: $assertion_key})
  ON CREATE SET
    a.assertion_id = randomUuid(), a.recorded_at = datetime(),
    a.identity_version = $identity_version,
    a.claim_key = $claim_key, a.source_key = $source_key,
    a.subject_uuid = $subject_uuid, a.subject_display = $subject_display,
    a.attribute = $attribute, a.scope = $scope,
    a.value_kind = $value_kind, a.unit = $unit,
    a.operation = $operation,
    a.value = $value_norm, a.value_json = $value_json,
    a.stated_span = $stated_span, a.span_start = $span_start, a.span_end = $span_end,
    a.claim_ordinal = $claim_ordinal,
    a.episode_uuid = $episode_uuid,
    a.valid_at = datetime($valid_at), a.learned_at = datetime($learned_at),
    a.time_basis = $time_basis,
    a.evidence_tier = $evidence_tier, a.evidence_rank = $evidence_rank,
    a.perceiver_version = $perceiver_version, a.perceiver_rank = $perceiver_rank,
    a.namespace = $namespace, a.metadata = $metadata_json,
    a.absolute_semantics = $absolute_semantics,
    // G19: a future-dated assertion needs a durable wake-up bit because the ingestion cursor will
    // advance before valid_at arrives. The scheduler atomically converts this into a projection
    // repair receipt when due. Historical/current writes stay false and add no work.
    a.activation_pending = datetime($valid_at) > datetime(),
    // 4a.1 write-time observation embedding (null when no embedder / embed failed -> backfill fills it).
    // ON CREATE only: a re-perception (ON MATCH) never overwrites an existing/backfilled embedding.
    a.name_embedding = $name_embedding, a.embed_version = $embed_version,
    a.superseded = true, a._created = true
  ON MATCH SET a._created = false
MERGE (h)-[:HAS_VERSION]->(a)
// monotone absolute-semantics upgrade on an idempotent re-write: ordinary -> correction is allowed,
// correction -> ordinary is FORBIDDEN (a later realization that dropped the correction label must not
// downgrade the persisted correction). Mismatch-gated like the tier upgrade.
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND $absolute_semantics = 'correction'
                   THEN [1] ELSE [] END |
    SET a.absolute_semantics = 'correction')
// monotonic tier upgrade on an idempotent re-write (user confirms an agent value); never down, and
// NEVER on a mismatch (a mismatched higher-tier rewrite must not upgrade A's claim).
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND $evidence_rank > coalesce(a.evidence_rank, -1)
                   THEN [1] ELSE [] END |
    SET a.evidence_tier = $evidence_tier, a.evidence_rank = $evidence_rank)
WITH h, a, cr, cur, binding_mismatch,
     ((NOT binding_mismatch) AND cur IS NOT NULL AND cur.assertion_key <> $assertion_key
      AND $perceiver_rank > coalesce(cur.perceiver_rank, -1)) AS will_supersede
// a becomes current: no prior current, OR it strictly out-ranks it, OR it IS the current (idempotent)
// — but NEVER on a binding_mismatch (a mismatched version stays a non-current, superseded audit node).
FOREACH (_ IN CASE WHEN (NOT binding_mismatch)
                        AND (cur IS NULL OR will_supersede
                             OR (cur IS NOT NULL AND cur.assertion_key = $assertion_key))
                   THEN [1] ELSE [] END |
    SET a.superseded = false
    MERGE (h)-[:CURRENT]->(a))
// replace + supersede the prior current when out-ranked (will_supersede is already mismatch-gated)
FOREACH (c IN CASE WHEN will_supersede THEN [cur] ELSE [] END |
    SET c.superseded = true, c.superseded_by = a.assertion_id, c.expired_at = datetime()
    MERGE (a)-[:SUPERSEDES]->(c)
    DELETE cr)
WITH h, a, will_supersede, binding_mismatch
// atomic provenance: link episode + entity in the SAME statement, or flag binding_pending.
// G14 bridge: the grounding anchor is an :Episodic (legacy/fixture path) OR a :TurnEvidence
// (production ADR-0001 path) carrying the DECLARANT foundation. $episode_uuid holds the anchor's
// uuid/turn_id; at most one of e/te resolves for a committed source (id spaces are disjoint
// randomUuids), so this is INERT for every current Episodic-sourced write (te IS NULL -> identical
// binding, no FOUNDS edge). A TurnEvidence anchor grounds the assertion AND draws (te)-[:FOUNDS]->(a)
// so the 10.G basis gate can trace the head to an admitted user statement.
OPTIONAL MATCH (e:Episodic {uuid: $episode_uuid})
OPTIONAL MATCH (te:TurnEvidence {turn_id: $episode_uuid})
OPTIONAL MATCH (n:Entity {uuid: $subject_uuid})
  WHERE $allow_canonical_self OR (
    NOT coalesce(n.is_self, false)
    AND toLower(trim(coalesce(n.entity_role, ''))) <> 'self'
  )
WITH h, a, will_supersede, binding_mismatch, e, te, n,
     ((e IS NOT NULL OR te IS NOT NULL) AND n IS NOT NULL) AS fully_bound,
     coalesce(a.binding_pending, true) AS was_pending
// pending -> bound adoption (ScalarStateView C.4.3): a claim first persisted as an unbindable
// advisory (sentinel subject_uuid, binding_pending=true) can later become uniquely bindable and be
// re-perceived. assertion_key OMITS subject_uuid, so the re-write lands on the SAME node; without
// adopting the now-resolved identity the node would keep the sentinel subject_uuid while
// binding_pending cleared below, and the subject_uuid-keyed fold would never retrieve it. When this
// write resolves a real entity for a still-pending row, adopt the identity onto BOTH the assertion
// and its head. NEVER on a mismatch (the claim is already durably bound to a different owner).
// `projection_pending` is a DURABLE crash-recovery marker (C.4.4.2): set true here — atomically with
// clearing binding_pending — exactly when this write NEWLY BINDS a row (a fresh bound write or a
// pending->bound adoption; NOT an idempotent re-write of an already-bound row, where was_pending is
// false). It means "bound, but the ScalarStateView has not yet been rebuilt". The caller
// (bind_and_persist / repair) clears it via `mark_projection_complete` ONLY after the rebuild
// succeeds, so a crash between the adoption write and the rebuild leaves the row discoverable by the
// repair pass (which selects binding_pending OR projection_pending) instead of a bound assertion with
// no View.
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND fully_bound AND was_pending THEN [1] ELSE [] END |
    SET a.subject_uuid = $subject_uuid, a.subject_display = $subject_display,
        a.claim_key = $claim_key, a.projection_pending = true,
        h.subject_uuid = $subject_uuid, h.claim_key = $claim_key)
// binding_pending is recomputed only when NOT a mismatch — a mismatch must never de-authorize A's
// bound claim by recomputing pending against the presented (wrong or unresolved) subject.
FOREACH (_ IN CASE WHEN binding_mismatch THEN [] ELSE [1] END |
    SET a.binding_pending = ((e IS NULL AND te IS NULL) OR n IS NULL))
// provenance links only when NOT a mismatch, so an already-bound claim can never acquire a second
// GROUNDS/HAS_ASSERTION owner via a record.
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND e IS NOT NULL THEN [1] ELSE [] END |
    MERGE (e)-[:GROUNDS]->(a))
// G14: a :TurnEvidence anchor FOUNDS the assertion -- the declarant-foundation edge the 10.G basis
// gate traces. Drawn ONLY when the anchor is a :TurnEvidence (never on the Episodic path), so it is
// inert for every current write.
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND te IS NOT NULL THEN [1] ELSE [] END |
    MERGE (te)-[:FOUNDS]->(a))
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND n IS NOT NULL THEN [1] ELSE [] END |
    MERGE (n)-[:HAS_ASSERTION]->(a))
RETURN a.assertion_id AS assertion_id, coalesce(a._created, false) AS created,
       will_supersede AS superseded_prior, coalesce(a.binding_pending, false) AS binding_pending,
       binding_mismatch AS binding_mismatch
"""
