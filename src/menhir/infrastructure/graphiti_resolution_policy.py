"""Menhir resolution policy adapters for the Graphiti fork's explicit hooks.

Menhir-owned halves of the former split installers, wired through the fork's
typed extension points instead of runtime symbol rebinding:

- #6  namespace inference for entity records with a NULL stored ``group_id``,
      registered through the fork's ``set_entity_record_group_id_resolver``
      startup configuration seam;
- #12 positive-identity veto policy on the fork's ``IdentityGateHook``;
- #14 structural/View candidate exclusion on the fork's ``CandidateFilterHook``;
- #16 canonical-self pre-resolution, candidate protection, and resolution
      telemetry on the fork's ``NodePreResolutionHook`` plus the independent
      node-pre-resolution edge channel;
- #13 dedupe branch telemetry, re-homed onto the supported hook callbacks and
      flushed per ``add_episode`` call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
from typing import Any

from graphiti_core.candidate_filter import CandidateFilterDecision
from graphiti_core.identity_gate import IdentityGateDecision
from graphiti_core.node_pre_resolution import PreResolutionDecision, PreResolutionResult

from menhir.infrastructure.graphiti_extraction_policy import get_extraction_receipt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Installer #6 half: entity-record namespace inference (fork resolver seam)
# ---------------------------------------------------------------------------


def resolve_entity_record_group_id(record: Any, provider: Any) -> str | None:
    """Infer the canonical group for a stored entity record whose ``group_id`` is null.

    Menhir's namespace convention: logical ``default`` maps to physical ``""``; a named
    namespace maps to that name. Returns ``None`` to fall back to the provider default
    when no namespace can be inferred.
    """
    attributes = record.get("attributes")
    namespace = attributes.get("namespace") if isinstance(attributes, dict) else None
    if namespace in (None, "", "default"):
        return ""
    return str(namespace)


def install_entity_record_group_id_resolver() -> None:
    """Register Menhir's namespace inference on the fork's startup configuration seam."""
    from graphiti_core.nodes import set_entity_record_group_id_resolver

    set_entity_record_group_id_resolver(resolve_entity_record_group_id)


# ---------------------------------------------------------------------------
# Canonical-self resolution policy (Menhir half of installer #16)
# ---------------------------------------------------------------------------


def _current_episode_key() -> str | None:
    try:
        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return None
    return getattr(receipt, "episode_key", None) or None if receipt is not None else None


def _active_self_identity() -> Any:
    """The identity context for the current episode, if binding ran."""
    try:
        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return None
    return getattr(receipt, "self_identity", None) if receipt is not None else None


def _pre_resolved_self_uuid() -> str | None:
    """The canonical self uuid bound for the current episode, if binding ran and succeeded."""
    try:
        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001 - resolution must never fail on instrumentation
        return None
    result = getattr(receipt, "self_bind_result", None) if receipt is not None else None
    if result is None or not getattr(result, "bound", False):
        return None
    return getattr(result, "self_uuid", None)


def _canonical_self_candidate_filter_enabled() -> bool:
    """Candidate isolation mutates resolution, so it belongs to ENFORCE only."""
    try:
        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return False
    return bool(
        receipt is not None
        and str(getattr(receipt, "self_bind_mode", "") or "") == "enforce"
    )


def _is_canonical_self_candidate(node: Any, identity: Any) -> bool:
    """Protect canonical self from every ordinary Graphiti resolution path.

    A declaration-bound node is removed from candidate search entirely. Every node that remains
    searchable is therefore unproven and must not reach canonical self through exact-name,
    similarity, an LLM choice, or ``existing_nodes_override``.
    """
    attributes = getattr(node, "attributes", None)
    if isinstance(attributes, dict) and (
        attributes.get("is_self") is True
        or str(attributes.get("entity_role") or "").strip().casefold() == "self"
    ):
        return True
    expected_uuid = str(getattr(identity, "self_uuid", "") or "")
    return bool(expected_uuid) and str(getattr(node, "uuid", "") or "") == expected_uuid


def _is_structural_graphiti_candidate(node: Any) -> bool:
    """Return whether a Graphiti candidate belongs to Menhir's structure graph."""
    attributes = getattr(node, "attributes", None)
    return isinstance(attributes, dict) and attributes.get("structure_role") is not None


def _is_view_graphiti_candidate(node: Any) -> bool:
    """Return whether a Graphiti candidate is a Menhir View (scalar_state, counter, timeline...).

    Views are derived state and must never be an identity-resolution target.
    """
    attributes = getattr(node, "attributes", None)
    if not isinstance(attributes, dict):
        return False
    return (
        bool(attributes.get("is_view"))
        or attributes.get("view_kind") is not None
        or attributes.get("view_class") is not None
    )


def _stamp_canonical_self(node: Any, identity: Any) -> Any:
    """Put the canonical markers on the node that will be persisted."""
    try:
        attributes = getattr(node, "attributes", None)
        if attributes is None:
            return node
        attributes["is_self"] = True
        attributes["entity_role"] = "self"
        if identity is not None and getattr(identity, "namespace", ""):
            attributes["namespace"] = identity.namespace
    except Exception:  # noqa: BLE001 - never fail resolution on a metadata stamp
        logger.exception("Could not stamp canonical-self markers")
    return node


async def _existing_canonical_node(clients: Any, extracted: Any, identity: Any) -> Any:
    """Return the persisted canonical self node, or a stamped *extracted* when none exists yet.

    **Only a genuinely absent node falls back to the extracted object.** Graphiti persists a
    resolved node with `SET n = $entity_data`, which REPLACES the property map, so treating a
    transient driver or database failure as "absent" would let a later successful write erase the
    canonical node's markers, provenance, flags and accumulated summary.
    """
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    driver = getattr(clients, "driver", None)
    if driver is None:
        raise RuntimeError(
            "canonical-self resolution requires a graph driver; refusing to substitute the "
            "extracted node for an unread canonical node"
        )
    try:
        stored = await EntityNode.get_by_uuid(driver, extracted.uuid)
    except NodeNotFoundError:
        return _stamp_canonical_self(extracted, identity)
    if identity is not None:
        from menhir.domain.namespace import namespace_to_group_id

        expected_group = namespace_to_group_id(identity.namespace)
        actual_group = getattr(stored, "group_id", None)
        if actual_group is None or str(actual_group) != expected_group:
            raise RuntimeError(
                f"stored canonical-self node {extracted.uuid!r} belongs to physical group "
                f"{actual_group!r}, expected {expected_group!r} for logical namespace "
                f"{identity.namespace!r}; refusing cross-namespace resolution"
            )
    return stored


# ---------------------------------------------------------------------------
# Identity-gate policy (Menhir half of installer #12)
# ---------------------------------------------------------------------------

_identity_gate_logger = logging.getLogger("menhir.dedup_identity_gate")


def _has_positive_identity_evidence(extracted_name: str, candidate_name: str) -> bool:
    """Return True if there is positive evidence that two names refer to the same entity.

    Conservative: returns True on any plausible match signal so legitimate merges
    (Bob→Robert, NYC→New York City, IBM→International Business Machines) are not blocked.
    """
    a = extracted_name.strip().lower()
    b = candidate_name.strip().lower()

    if not a or not b:
        return False

    if a == b:
        return True

    if len(a) >= 3 and a in b:
        return True
    if len(b) >= 3 and b in a:
        return True

    a_tokens = a.split()
    b_tokens = b.split()
    if len(a_tokens) == 1 and len(b_tokens) > 1:
        acronym = "".join(t[0] for t in b_tokens if t)
        if a.replace(".", "") == acronym:
            return True
    if len(b_tokens) == 1 and len(a_tokens) > 1:
        acronym = "".join(t[0] for t in a_tokens if t)
        if b.replace(".", "") == acronym:
            return True

    a_set = set(a_tokens) - {"the", "a", "an", "of", "in", "at", "on", "for", "to"}
    b_set = set(b_tokens) - {"the", "a", "an", "of", "in", "at", "on", "for", "to"}
    if a_set and b_set:
        intersection = a_set & b_set
        union = a_set | b_set
        if len(intersection) / len(union) >= 0.5:
            return True

    return False


def _edge_facts_mention(entity_name: str, edges: list[Any] | None) -> set[str]:
    """Return the set of fact texts from the request-local edges that mention ``entity_name``."""
    if not edges or not entity_name:
        return set()
    name_lower = entity_name.strip().lower()
    if len(name_lower) < 3:
        return set()
    facts: set[str] = set()
    for edge in edges:
        fact = ""
        if hasattr(edge, "fact"):
            fact = edge.fact or ""
        elif isinstance(edge, dict):
            fact = edge.get("fact", "")
        if name_lower in fact.lower():
            facts.add(fact)
    return facts


class MenhirIdentityGateHook:
    """Veto LLM-proposed merges lacking positive identity evidence.

    Wired on the fork's ``IdentityGateHook`` seam, which is invoked exactly once per
    valid LLM-proposed merge with the request-local edge evidence (the
    combined-extraction edges for this episode). For each merge the gate applies:

    1. **Name-level identity evidence** — exact match, substring, acronym, or ≥50%
       token Jaccard. If none exists, the merge is vetoed.
    2. **Edge-consistency invariant** — if the episode's edges' fact text mentions
       the extracted entity name but *not* the candidate name, the fact contradicts
       the merge and the merge is vetoed even when name-level evidence exists.

    Every veto is logged for analysis. This is defense-in-depth behind temperature=0
    and the fork's anti-conflation dedupe prompt.
    """

    async def evaluate_identity_gate(self, context: Any) -> IdentityGateDecision:
        ext_name = str(getattr(context.extracted_node, "name", "") or "")
        cand_name = str(getattr(context.candidate_node, "name", "") or "")

        veto_reason = ""

        if not _has_positive_identity_evidence(ext_name, cand_name):
            veto_reason = "no positive identity evidence"

        if not veto_reason:
            episode_edges = list(getattr(context, "edges", None) or [])
            ext_facts = _edge_facts_mention(ext_name, episode_edges)
            if ext_facts:
                cand_facts = _edge_facts_mention(cand_name, episode_edges)
                if not cand_facts:
                    veto_reason = (
                        f"edge-consistency: facts mention {ext_name!r} "
                        f"but not {cand_name!r}"
                    )

        collector = _resolution_telemetry.get()
        if veto_reason:
            episode = getattr(context, "episode", None)
            ep_content = episode.content[:120] if episode is not None else ""
            _identity_gate_logger.warning(
                "Identity gate VETO: %r merged into %r by LLM — %s. "
                "Overriding to new entity. Episode: %s",
                ext_name,
                cand_name,
                veto_reason,
                ep_content,
            )
            if collector is not None:
                collector.identity_gate_vetoes += 1
            return IdentityGateDecision.VETO

        if collector is not None:
            collector.identity_gate_merges += 1
        return IdentityGateDecision.ALLOW


# ---------------------------------------------------------------------------
# Candidate-filter policy (Menhir half of installer #14 + #16 protection)
# ---------------------------------------------------------------------------


class MenhirCandidateFilterHook:
    """Exclude structural/View nodes (and, in enforce mode, canonical self) from dedupe pools.

    Graphiti's semantic candidate search has no knowledge of Menhir's structural/semantic
    boundary or of Views. An extracted project or file name could resolve onto a structure
    node or a View and send that derived node through Graphiti's hydration + replacement-save
    path. Excluded candidates can never be resolved to — deterministically or via the LLM —
    for the extracted node under evaluation.
    """

    async def filter_candidate(self, context: Any) -> CandidateFilterDecision:
        candidate = context.candidate_node
        excluded_reason = ""
        if (
            _is_structural_graphiti_candidate(candidate)
            or _is_view_graphiti_candidate(candidate)
        ):
            excluded_reason = (
                "view"
                if _is_view_graphiti_candidate(candidate)
                else "structural"
            )
        elif (
            _canonical_self_candidate_filter_enabled()
            and _is_canonical_self_candidate(candidate, _active_self_identity())
        ):
            excluded_reason = "canonical_self"

        collector = _resolution_telemetry.get()
        if collector is not None:
            collector.record_candidate(context.extracted_node, candidate, excluded_reason)
        if excluded_reason:
            return CandidateFilterDecision.EXCLUDE
        return CandidateFilterDecision.INCLUDE


# ---------------------------------------------------------------------------
# Node pre-resolution policy (Menhir half of installer #16)
# ---------------------------------------------------------------------------


class MenhirNodePreResolutionHook:
    """Pre-resolve the canonical-self node and police undeclared canonical identity.

    A node already bound to the deterministic canonical-self uuid is authoritative by
    construction: trusted episode metadata proved the author, so there is nothing for
    similarity or an LLM to decide. It is pre-resolved here — excluded from candidate
    search, the candidate filter, deterministic similarity, and the dedupe LLM — and the
    EXISTING canonical node is committed when there is one, because Graphiti's
    replacement-save would otherwise wipe the stored node's accumulated state.

    In enforce mode an extracted node that merely *carries* canonical-self markers
    without being the declaration-bound node is refused outright: endpoint closure can
    retain an ordinary node named `user`, and without this guard a unique exact match
    would silently turn that name back into identity authority.
    """

    async def pre_resolve_node(self, context: Any) -> PreResolutionResult:
        _ensure_resolution_telemetry()
        node = context.extracted_node
        bound_uuid = _pre_resolved_self_uuid()
        if bound_uuid and str(getattr(node, "uuid", "") or "") == bound_uuid:
            resolved = await _existing_canonical_node(
                context.clients, node, _active_self_identity()
            )
            collector = _resolution_telemetry.get()
            if collector is not None:
                collector.extracted_node_count += 1
                collector.pre_resolved_self += 1
            logger.info(
                "Canonical-self resolver pre-resolved uuid=%s", bound_uuid
            )
            return PreResolutionResult(
                decision=PreResolutionDecision.RESOLVE, resolved_node=resolved
            )
        if (
            _canonical_self_candidate_filter_enabled()
            and _is_canonical_self_candidate(node, _active_self_identity())
        ):
            raise RuntimeError(
                "undeclared extracted node carries canonical-self identity in enforce "
                "mode; refusing ordinary Graphiti resolution"
            )
        collector = _resolution_telemetry.get()
        if collector is not None:
            collector.extracted_node_count += 1
            # An empty search never calls the candidate filter. Seed its zero
            # here so entirely absent candidates are still counted at flush.
            key = str(getattr(node, "uuid", "") or id(node))
            collector._pool_counts.setdefault(key, 0)
        return PreResolutionResult(decision=PreResolutionDecision.DEFER)


# ---------------------------------------------------------------------------
# Dedupe resolution telemetry (re-homed installer #13)
# ---------------------------------------------------------------------------


@dataclass
class ResolutionTelemetry:
    """Per-episode aggregation of the resolution-branch observations.

    The former installer #13 wrapped ``node_operations._resolve_with_similarity`` to
    classify each deterministic branch from the resolver's private state. The fork
    exposes no such private seam by design; the same semantics are reconstructed at the
    nearest supported boundaries — the pre-resolution hook (per extracted node, before
    search), the candidate filter (per candidate, after search), and the identity gate
    (per LLM-proposed merge) — and flushed once per ``add_episode`` call.
    """

    extracted_node_count: int = 0
    pre_resolved_self: int = 0
    candidate_pool_max: int = 0
    nodes_with_no_candidates: int = 0
    structural_excluded: int = 0
    view_excluded: int = 0
    canonical_self_excluded: int = 0
    identity_gate_merges: int = 0
    identity_gate_vetoes: int = 0
    _pool_counts: dict[str, int] = field(default_factory=dict, repr=False)

    def record_candidate(
        self, extracted_node: Any, candidate: Any, excluded_reason: str
    ) -> None:
        key = str(getattr(extracted_node, "uuid", "") or id(extracted_node))
        if excluded_reason:
            # Excluded candidates do not join the pool the resolver sees.
            self._pool_counts.setdefault(key, 0)
        else:
            self._pool_counts[key] = self._pool_counts.get(key, 0) + 1
        if excluded_reason == "structural":
            self.structural_excluded += 1
        elif excluded_reason == "view":
            self.view_excluded += 1
        elif excluded_reason == "canonical_self":
            self.canonical_self_excluded += 1

    def as_details(self) -> dict[str, Any]:
        pools = [count for count in self._pool_counts.values()]
        self.nodes_with_no_candidates = sum(1 for count in pools if count == 0)
        self.candidate_pool_max = max(pools) if pools else 0
        return {
            "extracted_node_count": self.extracted_node_count,
            "pre_resolved_self": self.pre_resolved_self,
            "candidate_count_max": self.candidate_pool_max,
            "no_candidates_new": self.nodes_with_no_candidates,
            "structural_excluded": self.structural_excluded,
            "view_excluded": self.view_excluded,
            "canonical_self_excluded": self.canonical_self_excluded,
            "llm_selected_candidate": self.identity_gate_merges,
            "identity_gate_vetoes": self.identity_gate_vetoes,
        }


_resolution_telemetry: ContextVar[ResolutionTelemetry | None] = ContextVar(
    "menhir_resolution_telemetry",
    default=None,
)


def _ensure_resolution_telemetry() -> ResolutionTelemetry:
    collector = _resolution_telemetry.get()
    if collector is None:
        collector = ResolutionTelemetry()
        _resolution_telemetry.set(collector)
    return collector


def start_resolution_telemetry() -> None:
    """Start a fresh per-episode telemetry aggregation (called by the extraction hook)."""
    _resolution_telemetry.set(ResolutionTelemetry())


def flush_resolution_telemetry() -> None:
    """Record the aggregated resolution telemetry and reset the collector.

    Called inside the Graphiti request task after the call completes (or fails),
    where the task-local resolution collector is visible.
    Telemetry must never fail an ingest, so every failure inside is swallowed.
    """
    collector = _resolution_telemetry.get()
    _resolution_telemetry.set(None)
    if collector is None:
        return
    try:
        from menhir.infrastructure.telemetry.recorders import record_lifecycle_event

        record_lifecycle_event(
            component="graphiti_dedup",
            event="resolution_outcomes",
            state="observed",
            episode_uuid=_current_episode_key(),
            details=collector.as_details(),
        )
    except Exception:  # noqa: BLE001 - instrumentation must never fail ingest
        logger.debug("Resolution telemetry flush failed", exc_info=True)


# ---------------------------------------------------------------------------
# Construction-time wiring validation
# ---------------------------------------------------------------------------


def menhir_resolution_hooks_installed(client: Any) -> bool:
    """True when the Graphiti instance carries the Menhir policy adapters.

    Canonical-self enforce mode requires the extraction hook and the full resolution
    policy set to be installed before any persistence can run; a partial construction
    would reopen probabilistic self resolution.
    """
    from menhir.infrastructure.graphiti_extraction_policy import MenhirExtractionHook

    return (
        isinstance(getattr(client, "single_episode_extraction_hook", None), MenhirExtractionHook)
        and isinstance(getattr(client, "identity_gate_hook", None), MenhirIdentityGateHook)
        and isinstance(getattr(client, "candidate_filter_hook", None), MenhirCandidateFilterHook)
        and isinstance(
            getattr(client, "node_pre_resolution_hook", None), MenhirNodePreResolutionHook
        )
    )
