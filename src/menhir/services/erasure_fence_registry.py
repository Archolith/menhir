"""Copier registry: every function that writes inventoried memory text, and the gate it runs under.

Plan: ``.agent/plans/menhir-erasure-audit-remediation-plan-2026-11-11.md`` R1.3 and plan v2 section
3.6. The T14 lint (``tests/test_erasure_text_writer_lint.py``) scans ``src/menhir`` and ``scripts/``
for writers of the text properties in ``infrastructure/graph_text_inventory.py`` and fails when a
writer has no entry here, when an entry matches no writer any more, when an UNFENCED entry lacks an
R2/P5/owner-accepted/UNTRIAGED tag, or when a claimed gate shows no evidence in the function.

An entry is a CLAIM about one (module, function). The lint checks the claim has evidence for the
gates it can see (G_Q, G_CREATE, G_COPY, G_FENCED); EXEMPT and G_PUBLISH are reviewed by hand.

Gate meanings (plan v2 section 2.2):

* ``G_Q``: queue-node writer with the ``EPISODE_NOT_MARKED`` predicate.
* ``G_CREATE``: creator of a deterministic-uuid node under the fence lock, ``created_epoch`` stamped.
* ``G_COPY``: ``execute_copy_fenced`` (epoch unchanged, no erasing op, no marked source).
* ``G_FENCED``: ``run_fenced`` / ``check_fence`` post-claim write.
* ``G_PUBLISH``: runs inside the fork's publish transaction (``MenhirPublishFence`` extras).
* ``EXEMPT``: needs no gate; the note names why (erase path, one-off migration, tooling, fresh text
  with no erased source, or a property that is not memory text).
* ``UNFENCED``: a current violator. The note MUST start with ``R2 <finding>``, ``P5 <item>``,
  ``owner-accepted: <reason>`` or ``UNTRIAGED: <why>``. The count only shrinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["COPIERS", "CopierEntry", "Gate"]


class Gate(str, Enum):
    G_Q = "G-q"
    G_CREATE = "G-create"
    G_COPY = "G-copy"
    G_FENCED = "G-fenced"
    G_PUBLISH = "G-publish"
    EXEMPT = "exempt"
    UNFENCED = "UNFENCED"


@dataclass(frozen=True)
class CopierEntry:
    module: str  # dotted module name
    function: str  # qualname, or "<module>" for module-level statements and unused constants
    sources: str  # what text it copies from
    gate: Gate
    note: str


_I = "menhir.infrastructure."
_S = "menhir.services."

#: R1.1's Entity allowlist is open-ended (every property not listed is text). These properties are
#: not memory text but are not in ENTITY_STRUCTURAL_PROPERTIES yet, so their writers are listed.
_AL = "Not memory text; listed only because ENTITY_STRUCTURAL_PROPERTIES lacks "

COPIERS: tuple[CopierEntry, ...] = (
    # ------------------------------------------------------------------ gated
    CopierEntry(
        _I + "episode_lifecycle", "EpisodeLifecycleRepository.create_evidence_projection",
        "TurnEvidence text of an existing turn", Gate.G_CREATE,
        "fence lock + erasing_ops = 0 + markers on turn and projection + created_epoch "
        "(episode_lifecycle.py, lane G class 2 #13)",
    ),
    CopierEntry(
        _I + "episode_lifecycle", "EpisodeLifecycleRepository.ensure_self_entity",
        "fixed self-entity summary", Gate.G_CREATE,
        "fence lock + erasing_ops + created_epoch (lane G class 2 #14)",
    ),
    CopierEntry(
        _I + "graphiti_edge_invalidation", "kept_conflict_statement",
        "kept and cause edge facts of one extraction", Gate.G_PUBLISH,
        "runs as an after_write extra inside the fenced publish tx; the outside-tx path "
        "(claim_fence is None) has 0 production constructors (lane G class 1). Cascade deletion of "
        "these nodes is CF-331 (R2), a separate gap",
    ),
    CopierEntry(
        _I + "anchored_time_persist", "persist_statement",
        "time_expression of an extracted edge", Gate.G_PUBLISH,
        "persist_anchored_time_in_tx runs inside the fenced publish tx; persist_anchored_time "
        "outside the tx is reachable only when claim_fence is None (0 production constructors)",
    ),
    # ------------------------------------------------------------------ R2 / P5 violators
    CopierEntry(
        _I + "episode_maintenance", "EpisodeMaintenanceRepository.create_raw_capture_entity",
        "episode content read before the seal", Gate.G_COPY,
        "R2 CF-327: one MERGE+MENTIONS statement under execute_copy_fenced (source = episode "
        "uuid; epoch from the exhausted fetch, else read at call); episode must exist untombstoned",
    ),
    CopierEntry(
        _I + "consolidation_queries", "ConsolidationRepository.compress_node",
        "entity content read by decay before the scrub", Gate.G_COPY,
        "R2 CF-338: fetch_decay_candidates reads fence key+epoch with the content and skips "
        "rederive_pending; the write runs under execute_copy_fenced and NOT rederive_pending",
    ),
    CopierEntry(
        _I + "consolidation_queries", "ConsolidationRepository.complete_rehydration",
        "merged LLM text over an archived original", Gate.G_COPY,
        "R2 CF-338: a content write runs under execute_copy_fenced with the node and the "
        "context's source as sources and NOT rederive_pending; no epoch is read with the archive, "
        "so only the epoch comparison is skipped (markers and erasing_ops still refuse)",
    ),
    CopierEntry(
        _I + "consolidation_queries", "ConsolidationRepository.resolve_conflict_group",
        "content of the kept and removed entities, read in earlier statements", Gate.G_COPY,
        "R2 CF-332: epoch read before keep/removed content; absorption write under "
        "execute_copy_fenced with both uuids as sources; records absorbed_from for the erase",
    ),
    CopierEntry(
        _I + "consolidation_queries", "ConsolidationRepository.update_edge_facts",
        "in-memory fact of every extracted edge, pre-checked by _fence_check only", Gate.UNFENCED,
        "P5 lane G G-3 (not an R2 row): no rederive_pending guard, no lock; check-then-act",
    ),
    CopierEntry(
        _I + "correlation_queries", "CorrelationRepository.merge_entity",
        "absorbed entity summary/content/merge_audit read in phase 1", Gate.UNFENCED,
        "P5 lane G G-4 / plan 3.6 correlation merge needs G-copy; phase 2 re-checks neither "
        "rederive_pending nor a marker",
    ),
    CopierEntry(
        _I + "correlation_queries", "CorrelationRepository.restore_merge_snapshot",
        "merge snapshot (properties of absorbed node and moved edges)", Gate.UNFENCED,
        "P5 lane G G-10 / plan 3.6 unmerge needs G-copy; refuses only an already-erased snapshot",
    ),
    CopierEntry(
        _I + "turn_evidence_repository", "TurnEvidenceRepository.record_turn_evidence",
        "new turn text for a turn_key", Gate.UNFENCED,
        "P5 lane G G-9: locks f but no erasing_ops refusal, no turn_key tombstone, no created_epoch "
        "(plan 2.2 G-create)",
    ),
    CopierEntry(
        _I + "typed_assertion_write_repository", "TypedAssertionWriteMixin.record_assertion",
        "stated span and value of a turn read before the seal", Gate.UNFENCED,
        "P5 lane G O-2: marker predicate only (G-q half of G-copy); no lock, no epoch check, "
        "so a commit-race leaves an assertion. Plan 3.6 wants G-copy with source turn and episode uuids",
    ),
    CopierEntry(
        _I + "memory_queries", "MemoryQueryRepository.apply_derived_rederive",
        "re-derived summary and fact text", Gate.UNFENCED,
        "P5 lane G C-05: item CAS only (rederive_flagged_at + episode-set equality); plan 3.6 "
        "puts rederive under G-copy",
    ),
    CopierEntry(
        _I + "view_write_repository", "ViewWriteRepositoryMixin._write_version",
        "view text derived from live evidence episodes", Gate.UNFENCED,
        "P5 lane G O-3: gated by the namespace fence lock (generation) plus a live-evidence "
        "recheck, not erasing_ops/epoch/marker; empty $eps passes vacuously. Decide whether that "
        "satisfies G-copy",
    ),
    CopierEntry(
        _I + "view_write_repository", "ViewWriteRepositoryMixin._refresh_fact_provenance",
        "view provenance refresh (n += $refresh)", Gate.UNFENCED,
        "P5 lane G O-3: same fence-lock (generation) + live-evidence gating as _write_version",
    ),
    CopierEntry(
        _I + "view_write_repository", "ViewWriteRepositoryMixin._replace_fact_provenance",
        "view provenance replace (n += $refresh)", Gate.UNFENCED,
        "P5 lane G O-3: same fence-lock (generation) + live-evidence gating as _write_version",
    ),
    CopierEntry(
        _I + "verifier_repository", "VerifierRepository.flag_referencing_beliefs",
        "verifier display string appended to review_reason", Gate.EXEMPT,
        "display is the raw probed config value (verifier_sync.env_key_executor), never memory "
        "text",
    ),
    CopierEntry(
        "scripts.repair_embedding_dimensions", "_reembed_relationships",
        "current fact of each edge, written by elementId", Gate.UNFENCED,
        "P5 plan v2 3.6 operator script: needs G-copy or an owner-accepted reason",
    ),
    CopierEntry(
        "scripts.replay_fold_flags", "main",
        "episode content replayed from a file into new Episodic nodes", Gate.UNFENCED,
        "P5 plan v2 3.6 operator script (replay_fold_flags.py:226): needs G-copy or an "
        "owner-accepted reason",
    ),
    # ------------------------------------------------------------------ exempt: erase path
    CopierEntry(
        _I + "memory_queries", "<module>",
        "scrub statement constants (blank fact/summary)", Gate.EXEMPT,
        "erase path: the entity and edge scrub writes blanks and sets rederive_pending",
    ),
    CopierEntry(
        _I + "memory_queries", "MemoryQueryRepository._delete_memory_with_scalar_cascade_in_namespace",
        "erased memory's holders", Gate.EXEMPT,
        "erase path itself: scrubs and deletes holders under the seal",
    ),
    CopierEntry(
        _I + "memory_queries", "MemoryQueryRepository.delete_memory_with_scalar_cascade",
        "erased memory's holders", Gate.EXEMPT, "erase path itself (cascade entry point)",
    ),
    CopierEntry(
        _I + "memory_queries", "MemoryQueryRepository.scrub_merge_audit_for_episodes",
        "merge_audit entries naming erased episodes", Gate.EXEMPT,
        "erase path itself: rewrites merge_audit with the erased marker",
    ),
    # ------------------------------------------------------------------ exempt: fresh text
    CopierEntry(
        _I + "episode_lifecycle", "EpisodeLifecycleRepository.create_pending_episode",
        "new memory text from the ingest caller", Gate.EXEMPT,
        "writes only text the caller supplies fresh under a new uuid4 (ingest_intake.py:130); no "
        "erased source",
    ),
    CopierEntry(
        _I + "artifact_repository", "ArtifactRepository.create_artifact",
        "artifact summary and body from the caller", Gate.EXEMPT,
        "new caller-supplied artifact text with a fresh uuid; no erased source",
    ),
    CopierEntry(
        _I + "candidate_repository", "CandidateRepository.create_candidate",
        "candidate content from the caller", Gate.EXEMPT,
        "new caller-supplied text (MCP add_candidate -> backend create_candidate); lane G O-8 "
        "asked whether any internal caller builds it from episodes; only the MCP/backend callers "
        "were found by grep",
    ),
    CopierEntry(
        _I + "temporal_repository", "TemporalRepository.create_temporal",
        "temporal item text from the caller", Gate.EXEMPT,
        "new caller-supplied text with a fresh uuid (lane G class 2 #29)",
    ),
    CopierEntry(
        _I + "todo_repository", "TodoRepository.create_todo",
        "todo text from the caller", Gate.EXEMPT,
        "new caller-supplied text with a fresh uuid; no erased source",
    ),
    # ------------------------------------------------------------------ exempt: not memory text
    CopierEntry(
        _I + "structure_queries", "StructureGraphWriter._merge_entity",
        "code-structure entities built from files on disk", Gate.EXEMPT,
        "structure graph from repository files, not memory (lane G class 2 #27)",
    ),
    CopierEntry(
        _I + "structure_queries", "StructureGraphWriter._merge_entities_batch",
        "code-structure entities from disk", Gate.EXEMPT, "structure graph from files, not memory",
    ),
    CopierEntry(
        _I + "structure_queries", "StructureGraphWriter._merge_symbols_batch",
        "code symbols from disk", Gate.EXEMPT, "structure graph from files, not memory",
    ),
    CopierEntry(
        _I + "structure_queries", "StructureGraphWriter._write_calls_edge",
        "code call-graph from disk", Gate.EXEMPT, "structure graph from files, not memory",
    ),
    CopierEntry(
        _I + "structure_queries", "StructureGraphWriter._write_contains_repo_edge",
        "code containment from disk", Gate.EXEMPT, "structure graph from files, not memory",
    ),
    CopierEntry(
        _I + "schema", "_node_defaults_queries",
        "default values for missing properties", Gate.EXEMPT,
        "one-off schema migration of defaults (session_id, user_id, edge_count, emotions); "
        + _AL + "those names",
    ),
    CopierEntry(
        _I + "schema", "get_scalar_state_activation_queries",
        "a node's own name/summary moved to view_name/view_summary", Gate.EXEMPT,
        "one-off activation migration: moves a node's own text between its properties, no new copy",
    ),
    CopierEntry(
        "scripts.smoke.hook_center_stale_lane_smoke_backend", "Neo4jBackend.create_fixture",
        "smoke fixture entity", Gate.EXEMPT, "test/bench tooling: creates a synthetic smoke entity",
    ),
)
