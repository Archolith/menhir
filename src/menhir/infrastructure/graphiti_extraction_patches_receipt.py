"""Per-episode extraction receipt threaded across the combined-extraction task boundary.

Owns the receipt dataclass, its ContextVar, the parent-task lifecycle helpers, and the
policy-empty predicate. Extracted verbatim from ``graphiti_extraction_patches``; that module
re-exports everything here.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from menhir.domain.self_identity import (
    SelfIdentityContext,
    SelfSubjectEndpointEnvelope,
)
from menhir.infrastructure.self_binding import SelfBindMode, SelfBindResult

# ---------------------------------------------------------------------------
# Combined-extraction receipt (raw -> final counts, threaded across the child task)
# ---------------------------------------------------------------------------
# graphiti_core's add_episode is spawned inside asyncio.create_task() by
# GraphitiClient._await_add_episode_request, so any ContextVar *rebind* performed
# inside graphiti (child task) does NOT propagate back to the parent task where
# stamp_and_finalize runs. To carry pre-resolution counts across that boundary we
# set a *mutable* receipt object into a ContextVar in the PARENT (add_episode)
# BEFORE the child task is created; the child inherits the same object and mutates
# its fields in place, which the parent then reads. Rebinding the ContextVar inside
# the child would be invisible; mutating the shared object is not.


@dataclass
class CombinedExtractionReceipt:
    """Per-episode receipt distinguishing legitimate-empty from collapsed extraction."""

    episode_key: str = ""
    episode_text: str = ""
    source_description: str = ""
    #: What the ingestion boundary actually PROVED about this episode's author, carried from the
    #: parent task so the binding seam never has to infer identity from extracted text. ``None``
    #: means no trusted signal was supplied, which fails closed: no self binding. The logical
    #: namespace lives here rather than being inferred from ``group_id``, because logical
    #: ``default`` maps to physical ``""`` and the two must not be conflated.
    self_identity: "SelfIdentityContext | None" = None
    #: Menhir-created author endpoint for one graph-proven evidence projection.  It is separate
    #: from identity evidence because authorship alone must never select an extracted node.
    self_subject_endpoint: "SelfSubjectEndpointEnvelope | None" = None
    #: The actual Menhir-allocated author node, created before model dispatch. Extraction may
    #: reference its handle but never chooses its identity. Binding operates on a copy.
    self_subject_node: Any | None = None
    #: Refusal-only output accounting. Raw evidence survives; an unresolved author reference
    #: is not recovered by persisting another ordinary self node.
    unresolved_author_nodes_suppressed: int = 0
    unresolved_author_edges_suppressed: int = 0
    #: Rollout control for this episode. ``OFF`` reproduces pre-change behavior exactly.
    self_bind_mode: "SelfBindMode" = SelfBindMode.OFF
    #: Outcome of the binding attempt, or ``None`` if binding never ran. Read by the resolver
    #: partition to know which UUID is already authoritative and must skip candidate search.
    self_bind_result: "SelfBindResult | None" = None
    #: Graphiti's internally allocated primary episode UUID for this extraction.  It differs from
    #: the external pending UUID and is required to prove a marker edge belongs to CURRENT MESSAGES.
    graphiti_episode_uuid: str = ""
    #: Text Graphiti supplied to the extractor as previous conversational context. Missing edge
    #: endpoints may be closed when grounded here even if the current turn uses a pronoun (for
    #: example, previous "Rachel ..." followed by current "She moved to Chicago.").
    previous_episode_texts: tuple[str, ...] = ()
    #: A lazy, bounded loader for adjacent raw transcript turns. Graphiti's ordinary
    #: ``previous_episodes`` contains only enriched episodes, while context-only assistant turns
    #: live exclusively as ``:TurnEvidence``. The loader is called only after an entity-bearing,
    #: edge-empty first pass, so the successful path pays no graph read and sees no extra context.
    relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None
    #: The exact adjacent turns supplied to the corrective pass. Kept on the receipt so endpoint
    #: closure may ground a repair-emitted endpoint in the same context the model saw.
    relationless_repair_context_texts: tuple[str, ...] = ()
    #: Repair edges rejected because none of their fact/relation/endpoint tokens were grounded in
    #: CURRENT MESSAGES. Native previous-episode context improves recall but can prime the model to
    #: copy a preceding claim; this count makes that deterministic precision guard auditable.
    context_unsupported_edges_suppressed: int = 0
    #: Malformed marker transport removed during sanitation. Identity and literal transport are
    #: structural; semantic attribution remains model inference, not owner confirmation.
    subject_marker_edges_suppressed: int = 0
    raw_entity_count: int = 0
    raw_edge_count: int = 0
    malformed_entities_dropped: int = 0
    malformed_edges_dropped: int = 0
    endpoints_synthesized: int = 0
    resolved_node_count: int = 0
    resolved_edge_count: int = 0
    orphan_nodes_dropped: int = 0
    #: Edges suppressed because they were `user -> X` ECHO on an assistant turn (the human already
    #: stated the fact first-hand in their own turn). When this accounts for every raw edge, the
    #: resulting empty extraction is a POLICY decision, not a collapse -- see
    #: `is_policy_empty_extraction`.
    self_echo_edges_suppressed: int = 0
    #: Membership edges emitted for a TITLED LIST whose items the extractor returned with no relation
    #: between them (see `parse_titled_list`). Counted separately from `endpoints_synthesized` because
    #: the provenance differs: an endpoint is closed to save an edge the model DID state, whereas these
    #: encode membership the list SYNTAX states. Auditable either way.
    list_membership_edges_added: int = 0
    #: A model response containing entities but no usable relationship gets one immediate,
    #: instruction-hardened repair attempt before it can become a visible failure. These fields
    #: make that extra paid call and its outcome explicit in the receipt/error path.
    relationless_repair_attempted: bool = False
    relationless_repair_succeeded: bool = False
    relationless_initial_entity_count: int = 0
    relationless_initial_edge_count: int = 0
    #: An assistant turn that extracts only the canonical human label and no relationship is the
    #: entity-only form of the existing self-echo policy. It contains no first-hand fact to store
    #: and must complete as an intentional empty result rather than paying for a repair that policy
    #: would suppress even if it produced ``user -> X``.
    assistant_self_only_relationless: bool = False
    #: Self-only shape of the FIRST extraction pass: ALL extracted entities were canonical
    #: self-labels and there were zero raw edges, regardless of source role. Recorded separately
    #: from the repair pass because the two passes see different payloads and ONE field would be
    #: overwritten by the second: a first pass that extracted `Seattle` followed by a repair that
    #: returned only `user` must stay a visible collapse, not a policy-empty success.
    initial_self_only_entities: bool = False
    #: Self-only shape of the REPAIR pass, under the same rule. Only meaningful once
    #: ``relationless_repair_attempted`` is set. Both flags together (plus a failed repair) mean
    #: two independent passes agreed the content has nothing extractable
    #: (e.g. "Thanks again for your help!").
    repair_self_only_entities: bool = False


_extraction_receipt: ContextVar[CombinedExtractionReceipt | None] = ContextVar(
    "menhir_graphiti_extraction_receipt",
    default=None,
)


def is_policy_empty_extraction(receipt: "CombinedExtractionReceipt | None") -> bool:
    """True when an empty extraction is an intentional no-op, not a collapse to be retried.

    An assistant turn that only restates the human's own facts (`user -> X`) has every edge
    suppressed by design, which leaves nothing to persist. That is the CORRECT outcome, and it is
    deterministic: retrying re-extracts the same echo and suppresses it again, so treating it as a
    retryable failure burns the episode's whole retry budget and inflates the measured failure rate.
    The same policy applies when extraction returns only a self label and no edge: a repair could
    only produce the assistant-authored self edge this policy would suppress. Real collapses remain
    visible because either every surviving raw edge must be accounted for as echo, or every
    relationless entity must be a self label on an explicitly prefixed assistant turn.
    """
    if receipt is None:
        return False
    if receipt.assistant_self_only_relationless:
        return True
    # BOTH passes must independently have produced only self-labels with zero edges. This covers
    # user turns like "Thanks again for your help!" whose evidence projections bypass the adaptive
    # segmenter and correctly extract only {"name":"user"} twice over. Requiring the INITIAL pass
    # to be self-only too is what keeps the guard honest: a first pass that extracted a real entity
    # and a repair that came back with only `user` is content the pipeline lost, and it must stay a
    # visible collapse rather than borrow this success path from the repair's shape alone.
    if (receipt.relationless_repair_attempted
            and not receipt.relationless_repair_succeeded
            and receipt.initial_self_only_entities
            and receipt.repair_self_only_entities):
        return True
    # Native context can prime a repair to copy a preceding claim into a truly empty current turn.
    # If the first pass saw only self and EVERY usable repair edge lacked any current-message anchor,
    # the grounding guard correctly removed copied context and the resulting empty is intentional.
    usable_repair_edges = receipt.raw_edge_count - receipt.malformed_edges_dropped
    if (
        receipt.relationless_repair_attempted
        and not receipt.relationless_repair_succeeded
        and receipt.initial_self_only_entities
        and receipt.relationless_repair_context_texts
        and usable_repair_edges > 0
        and receipt.context_unsupported_edges_suppressed >= usable_repair_edges
    ):
        return True
    if (receipt.unresolved_author_nodes_suppressed > 0
            and receipt.resolved_node_count == 0 and receipt.resolved_edge_count == 0
            and receipt.unresolved_author_edges_suppressed
            >= max(0, receipt.raw_edge_count - receipt.malformed_edges_dropped)):
        return True
    if receipt.raw_edge_count <= 0:
        return False
    usable_edges = receipt.raw_edge_count - receipt.malformed_edges_dropped
    return usable_edges > 0 and receipt.self_echo_edges_suppressed >= usable_edges


def get_extraction_receipt() -> CombinedExtractionReceipt | None:
    """Return the active extraction receipt for this task, if any."""
    return _extraction_receipt.get()


def clear_extraction_receipt() -> None:
    """Deactivate the extraction receipt (consume-once semantics)."""
    _extraction_receipt.set(None)
