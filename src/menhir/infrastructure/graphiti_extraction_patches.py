"""Combined Graphiti extraction and extraction-receipt compatibility patches.

Facade: the receipt, endpoint, sanitation, prompt, runner, binding-support, and model-patch
families live in the ``graphiti_extraction_patches_*.py`` siblings and are re-exported here
unchanged. The runtime patch seam stays in THIS module: the globals ``_patch_graphiti_combined_
extraction`` mutates, the extraction bridges that read them, the titled-list parser pinned here
by source, and the receipt/binding entry points tests pin by source location.
"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import logging
import re
from typing import Any, Callable

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    SelfEvidenceKind, SelfIdentityContext, SelfSubjectEndpointEnvelope,
    declare_self_subject, is_self_alias, proves_self_subject,
)
from menhir.infrastructure.self_binding import (
    AmbiguousSelfBindingError, InvalidSelfSubjectDeclarationError, SelfBindMode,
    SelfBindOutcome, SelfBindResult, bind_canonical_self,
)
from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX, check_graphiti_version
from menhir.infrastructure.graphiti_extraction_patches_binding import (
    _edge_endpoint_uuids, _record_self_binding_decision,
)
from menhir.infrastructure.graphiti_extraction_patches_lists import (
    _LIST_BULLET_RE, _LIST_CLAUSE_PRONOUNS, _LIST_TITLE_RE, _LIST_VERBS, _LIST_VERB_RE,
    _MEMBERSHIP_RELATION,
)
from menhir.infrastructure.graphiti_extraction_patches_endpoints import (
    _CURRENT_MESSAGE_ANCHOR_STOPWORDS, _CURRENT_MESSAGE_TOKEN_RE, _EDGE_ANCHOR_EVIDENCE_FIELDS,
    _NON_SYNTHESIZABLE_ENDPOINTS, _SELF_ENTITY_NAME, _SELF_FIRST_PERSON, _SELF_THIRD_PERSON,
    _ASSISTANT_POLICY_SELF_LABELS, _active_subject_marker, _anchor_token_forms,
    _contains_token_sequence, _current_message_anchor_tokens, _edge_has_current_message_anchor,
    _episode_role, _is_reserved_subject_marker, _is_synthesizable_endpoint,
    _is_unresolved_self_like_endpoint, _normalize_endpoint_name, _subject_marker_guard_active,
)
from menhir.infrastructure.graphiti_extraction_patches_models import (
    _patch_graphiti_combined_extraction_models,
)
from menhir.infrastructure.graphiti_extraction_patches_prompts import (
    _AUTHOR_REFERENCE_RE, _DO_NOT_INVENT, _FIRST_PERSON_SELF_BINDING, _PROMPT_SECTION_TAGS,
    _RELATIONLESS_REPAIR_CONTEXT_INSTRUCTIONS, _RELATIONLESS_REPAIR_CONTEXT_MAX_CHARS,
    _RELATIONLESS_REPAIR_CORE, _RELATION_COMPLETENESS_CORE, _REPAIR_FIRST_PERSON_USER,
    _combine_extraction_instructions, _episode_cache_key, _first_person_self_binding,
    _is_first_person, _load_relationless_repair_context, _needs_relationless_repair,
    _relation_completeness_instructions, _relationless_repair_instructions,
    _relationless_repair_previous_episodes, _subject_endpoint_correction_instructions,
    _subject_endpoint_instructions, _neutralize_prompt_delimiters,
    _unresolved_author_aliases,
)
from menhir.infrastructure.graphiti_extraction_patches_receipt import (
    CombinedExtractionReceipt, _extraction_receipt, clear_extraction_receipt,
    get_extraction_receipt, is_policy_empty_extraction,
)
from menhir.infrastructure.graphiti_extraction_patches_runner import (
    _run_graphiti_combined_extraction,
)
from menhir.infrastructure.graphiti_extraction_patches_sanitize import (
    _sanitize_combined_edge, _sanitize_combined_entity, _sanitize_combined_payload,
)

logger = logging.getLogger(__name__)

# Version guard - run once at import like the pre-CF-87 local check did. The
# shared helper (graphiti_helpers.check_graphiti_version) owns the expected
# prefix declaration and the warn-only logic.
check_graphiti_version()

_combined_extraction_cache: ContextVar[tuple[str, list[Any]] | None] = ContextVar(
    "menhir_graphiti_combined_extraction_cache",
    default=None,
)
_original_graphiti_extract_edges: Any | None = None
_original_graphiti_extract_nodes: Any | None = None
#: The MODULE holding the replacement extractor's real dependency, imported at patch time so the
#: patch's own ImportError guard covers it. Deliberately the module and not the function: the
#: attribute is read per call so a later rebind -- another patch, or a test seam -- is still seen.
#: Freezing the function here would trade one silent-failure mode for another.
_graphiti_combined_extraction_module: Any | None = None


def _resolve_combined_extractor() -> Any:
    """Return the combined extractor, resolved from the module bound at patch time.

    Falls back to a direct import for callers that reach this function without having applied the
    patch (the extraction tests do exactly that). Availability is still PROVEN at patch time, which
    is the point of CF-12: the patch no longer reports success while its real dependency is absent.
    """
    module = _graphiti_combined_extraction_module
    if module is None:
        from graphiti_core.utils.maintenance import combined_extraction as module
    return module.extract_nodes_and_edges


# Titled-list recognition. The parser below is pinned to THIS file by source (the CF-193 drift
# guard reads it with inspect.getsource); its regex/allowlist data lives in
# graphiti_extraction_patches_lists and is re-exported from here.

#: An item must look like a NAME, not a sentence. Verbs and sentence punctuation disqualify the whole
#: block -- one prose line is enough to refuse, because a half-parsed list is worse than none.
#: Verbs come from the closed allowlist `_LIST_VERBS` below -- the same style as
#: `_ACQUISITION_VERBS` in services/event_history_recall.py. No stemming, no synonyms, no
#: part-of-speech call.
#:
#: The rule is POSITIONAL: an item that BEGINS with a verb or a pronoun and continues is a clause,
#: not a name -- "buy milk", "fixed the bug", "ate lunch", "we are working today".
#:
#: Matching an allowlisted verb ANYWHERE was tried first and over-refused badly, because most of
#: these words are also common nouns: it rejected "Tools:/saw/hammer/drill",
#: "Races:/fun run/night run" and an album named "Work" -- exactly the NAME lists this parser
#: exists to accept. Anchoring at the start costs the mid-item case (a clause whose first word is
#: neither verb nor pronoun still passes THIS guard) and buys back that whole class of lists.
#: Sentence punctuation and the 6-word cap remain as the other two guards.
_LIST_ITEM_MAX_WORDS = 6


def parse_titled_list(episode_text: str) -> tuple[str, list[str]] | None:
    """Parse ``title:\n item\n item...`` into (title, items), or None when it is not clearly a list.

    DELIBERATELY STRICT -- refusing a real list costs one enrichment that behaves exactly as it does
    today, while accepting prose invents membership edges that are silently wrong. Every rule below
    exists to make the second failure impossible, so read them as a whitelist, not a heuristic:

      * the first line must contain ':' -- an explicit author-written "a list follows" marker
      * at least 3 items, so a colon in ordinary prose cannot produce a two-node "list"
      * every item <= 6 words, free of sentence punctuation, and free of allowlisted verbs
        (`_LIST_VERBS`) -- items are NAMES, not clauses
      * ONE non-conforming item refuses the WHOLE block (no partial parse)

    The FIRST ITEM MAY SIT ON THE TITLE LINE (`agents names below:Admon`), which is how the turn that
    motivated this is actually written. Requiring the colon to end the line refused exactly that case.
    The title may be the first line of the turn or follow a role prefix ("user: agents names below:").
    Returns names exactly as written, minus bullet decoration; deduplication is left to resolution.
    """
    text = str(episode_text or "")
    if ":" not in text or "\n" not in text:
        return None
    body = text.split(":", 1)[1] if _episode_role(text) != "unknown" else text
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln]
    if len(lines) < 3:                      # title line + >= 2 more; item count is checked below
        return None

    m = _LIST_TITLE_RE.match(lines[0])
    if m is None:
        return None
    title = m.group("title").strip()
    if not title or len(title.split()) > 8:
        return None

    # An item on the title line counts as the first item, not as part of the title.
    first = m.group("first").strip()
    items: list[str] = []
    for raw in ([first] if first else []) + lines[1:]:
        item = _LIST_BULLET_RE.sub("", raw).strip().rstrip(",;")
        if not item:
            return None
        if len(item.split()) > _LIST_ITEM_MAX_WORDS:
            return None
        if _LIST_VERB_RE.match(item):       # leading verb + object => a clause, not a name
            return None
        if any(ch in item for ch in ".!?"):  # sentence punctuation => prose, refuse the block
            return None
        items.append(item)
    if len(items) < 3:
        return None
    return title, items


def begin_extraction_receipt(
    episode_key: str,
    episode_text: str,
    *,
    source_description: str = "",
    relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None,
    self_identity: SelfIdentityContext | None = None,
    self_subject_endpoint: SelfSubjectEndpointEnvelope | None = None,
    self_bind_mode: SelfBindMode = SelfBindMode.OFF,
) -> CombinedExtractionReceipt:
    """Create and activate a fresh receipt for the current episode (call in the parent task).

    ``self_identity`` must be constructed by the caller from the claimed episode's persisted,
    gate-approved metadata. Omitting it fails closed: extraction proceeds with no self binding.
    """
    normalized_episode_key = str(episode_key or "")
    if self_subject_endpoint is not None:
        if self_bind_mode is not SelfBindMode.ENFORCE:
            raise InvalidSelfSubjectDeclarationError(
                "a self-subject endpoint may be activated only in enforce mode"
            )
        if (
            self_identity is None
            or self_identity.evidence_kind is not SelfEvidenceKind.TRUSTED_USER_TURN
        ):
            raise InvalidSelfSubjectDeclarationError(
                "a self-subject endpoint requires trusted user-turn evidence"
            )
        if (
            self_subject_endpoint.episode_uuid != normalized_episode_key.strip()
            or self_subject_endpoint.episode_uuid
            != str(self_identity.episode_uuid or "").strip()
            or self_subject_endpoint.namespace != self_identity.namespace
            or self_subject_endpoint.turn_evidence_uuid
            != str(self_identity.turn_evidence_uuid or "").strip()
        ):
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint scope does not match its extraction receipt"
            )
    receipt = CombinedExtractionReceipt(
        episode_key=normalized_episode_key,
        episode_text=str(episode_text or ""),
        source_description=str(source_description or ""),
        relationless_repair_context_loader=relationless_repair_context_loader,
        self_identity=self_identity,
        self_subject_endpoint=self_subject_endpoint,
        self_bind_mode=self_bind_mode,
    )
    if self_subject_endpoint is not None:
        from graphiti_core.nodes import EntityNode
        from graphiti_core.utils.datetime_utils import utc_now

        # This is the only production declaration producer. It selects a node Menhir CREATED,
        # before any model output, never a UUID selected by the extractor or a language parser.
        receipt.self_subject_node = EntityNode(
            name=self_subject_endpoint.marker,
            group_id=namespace_to_group_id(self_subject_endpoint.namespace),
            labels=["Entity"],
            created_at=utc_now(),
        )
        receipt.self_identity = declare_self_subject(
            self_identity, subject_node_uuid=receipt.self_subject_node.uuid
        )
    _extraction_receipt.set(receipt)
    return receipt


def _bind_subject_endpoint(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    receipt: CombinedExtractionReceipt,
) -> SelfBindResult:
    """Attach inferred relationships to the preallocated author, atomically before dedup.

    The model's marker node is a transport reference, NOT the declared identity. Discard it
    (including model-produced properties) and connect a copy of Menhir's existing author node.
    No interpretation of natural-language shape issues the declaration. Inaccurate relationship
    attribution remains a possible extraction error under the automatic-memory contract.
    """
    endpoint = receipt.self_subject_endpoint
    identity = receipt.self_identity
    owned = receipt.self_subject_node
    if (receipt.self_bind_mode is not SelfBindMode.ENFORCE or endpoint is None
            or identity is None or owned is None
            or not proves_self_subject(owned.uuid, identity)
            or owned.name != endpoint.marker
            or owned.group_id != namespace_to_group_id(endpoint.namespace)):
        raise InvalidSelfSubjectDeclarationError("self endpoint lacks its preallocated author")
    if (
        endpoint.episode_uuid != str(receipt.episode_key or "").strip()
        or endpoint.episode_uuid != str(identity.episode_uuid or "").strip()
        or endpoint.namespace != identity.namespace
        or endpoint.turn_evidence_uuid != str(identity.turn_evidence_uuid or "").strip()
    ):
        raise InvalidSelfSubjectDeclarationError("self endpoint scope differs from its receipt")

    reserved = [node for node in nodes if _is_reserved_subject_marker(getattr(node, "name", None))]
    if any(node.name != endpoint.marker for node in reserved):
        raise InvalidSelfSubjectDeclarationError("final payload contains a stale or malformed self-subject marker")
    if len(reserved) > 1:
        raise InvalidSelfSubjectDeclarationError("final payload contains more than one self-subject marker node")
    uuids = [str(getattr(node, "uuid", "") or "") for node in nodes]
    if any(not uuid for uuid in uuids) or len(uuids) != len(set(uuids)):
        raise InvalidSelfSubjectDeclarationError("extracted node UUIDs must be nonblank and unique")
    if owned.uuid in uuids or identity.self_uuid in uuids:
        raise InvalidSelfSubjectDeclarationError("extracted node pre-stamped a reserved self UUID")

    marker_uuid = str(reserved[0].uuid) if reserved else ""
    if reserved:
        if getattr(reserved[0], "group_id", None) != owned.group_id:
            raise InvalidSelfSubjectDeclarationError("marker node has a foreign physical group")
        marker_edges = [edge for edge in edges if marker_uuid in {
            str(getattr(edge, "source_node_uuid", "")), str(getattr(edge, "target_node_uuid", ""))
        }]
        if not marker_edges:
            raise InvalidSelfSubjectDeclarationError("marker node is not an endpoint of a current-episode edge")
        if not receipt.graphiti_episode_uuid or any(
            receipt.graphiti_episode_uuid not in (getattr(edge, "episodes", None) or [])
            for edge in marker_edges
        ):
            raise InvalidSelfSubjectDeclarationError("marker edge lacks current Graphiti episode attribution")
        if 0 not in index_map.get(marker_uuid, []):
            raise InvalidSelfSubjectDeclarationError("marker node lacks current-episode index attribution")

    # An ambiguous alias is never guessed into the author or persisted as a substitute self.
    # This can withhold a legitimate bare RBAC `user` in mixed prose; qualified names survive.
    unsafe = _unresolved_author_aliases(nodes, receipt)
    rejected = [edge for edge in edges if unsafe & _edge_endpoint_uuids(edge)]
    rejected_ids = {id(edge) for edge in rejected}
    kept_edges = [edge for edge in edges if id(edge) not in rejected_ids]
    retained = set().union(*(_edge_endpoint_uuids(edge) for edge in kept_edges))
    removed = unsafe | (set().union(*(_edge_endpoint_uuids(edge) for edge in rejected)) - retained)
    kept_nodes = [node for node in nodes if node.uuid not in removed]
    candidate_edges = deepcopy(kept_edges)
    candidate_index = {uuid: list(indices) for uuid, indices in index_map.items() if uuid not in removed}
    referenced = bool(marker_uuid and marker_uuid in retained)
    if referenced:
        author = deepcopy(owned)
        kept_nodes = [author if node.uuid == marker_uuid else node for node in kept_nodes]
        candidate_index[author.uuid] = candidate_index.pop(marker_uuid)
        for edge in candidate_edges:
            for attr in ("source_node_uuid", "target_node_uuid"):
                if getattr(edge, attr, None) == marker_uuid:
                    setattr(edge, attr, author.uuid)
        result = bind_canonical_self(kept_nodes, candidate_edges, candidate_index, identity, receipt.self_bind_mode)
    else:
        # The identity was established before extraction, but there is no usable reference to
        # persist. Do not fabricate an author fact or attach a node to an unrelated episode.
        result = SelfBindResult(SelfBindOutcome.NO_SELF_CANDIDATE, mode=receipt.self_bind_mode)

    # Publish only after transport validation, copies, quarantine, and binding all succeeded.
    nodes[:] = kept_nodes
    edges[:] = candidate_edges
    index_map.clear()
    index_map.update(candidate_index)
    receipt.unresolved_author_nodes_suppressed = len(unsafe)
    receipt.unresolved_author_edges_suppressed = len(rejected)
    if unsafe:
        logger.info("Unresolved author references withheld episode_id=%s nodes=%d edges=%d",
                    receipt.episode_key, len(unsafe), len(rejected))
    return result


def _record_self_binding(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    receipt: CombinedExtractionReceipt,
) -> SelfBindResult:
    """Run the binding decision and record it, without letting telemetry break extraction.

    A refusal is a DECISION, not an absence of one, so it is recorded on the same event as every
    other outcome. Recording it after the raise -- or not at all -- would make the one outcome an
    operator most needs to see during an observation window the only invisible one.

    Observe mode must also not fail the episode. Its entire purpose is to measure what enforce
    would do without changing behavior; propagating the refusal there would make merely observing
    a durable change in ingest success.
    """
    try:
        identity = receipt.self_identity
        if (
            identity is not None
            and identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
            and str(identity.episode_uuid or "").strip()
            != str(receipt.episode_key or "").strip()
        ):
            raise InvalidSelfSubjectDeclarationError(
                f"declared self subject belongs to episode {identity.episode_uuid!r}, not active "
                f"episode {receipt.episode_key!r}; refusing to bind"
            )
        result = (
            _bind_subject_endpoint(nodes, edges, index_map, receipt)
            if receipt.self_subject_endpoint is not None
            else bind_canonical_self(nodes, edges, index_map, identity, receipt.self_bind_mode)
        )
    except AmbiguousSelfBindingError:
        result = SelfBindResult(
            outcome=SelfBindOutcome.AMBIGUOUS,
            mode=receipt.self_bind_mode,
            self_like_without_subject_authority=sum(
                1 for n in nodes if is_self_alias(getattr(n, "name", None))
            ),
        )
        _record_self_binding_decision(result, receipt)
        if receipt.self_bind_mode is SelfBindMode.OBSERVE:
            return result
        raise
    _record_self_binding_decision(result, receipt)
    return result


async def _extract_nodes_combined_for_add_episode(
    clients: Any,
    episode: Any,
    previous_episodes: list[Any],
    entity_types: Any = None,
    excluded_entity_types: Any = None,
    custom_extraction_instructions: str | None = None,
) -> tuple[list[Any], dict[str, list[int]]]:
    nodes, edges, index_map = await _run_graphiti_combined_extraction(
        clients,
        episode,
        previous_episodes,
        entity_types,
        excluded_entity_types,
        custom_extraction_instructions,
    )
    _combined_extraction_cache.set((_episode_cache_key(episode), edges))
    return nodes, index_map


async def _extract_edges_from_combined_cache(
    clients: Any,
    episode: Any,
    extracted_nodes: list[Any],
    previous_episodes: list[Any],
    edge_type_map: Any,
    group_id: str,
    edge_types: Any = None,
    custom_extraction_instructions: str | None = None,
) -> list[Any]:
    cached = _combined_extraction_cache.get()
    _combined_extraction_cache.set(None)
    if cached is not None and cached[0] == _episode_cache_key(episode) and not edge_types:
        return cached[1]
    if _original_graphiti_extract_edges is None:
        raise RuntimeError("Graphiti edge extraction fallback was not initialized")
    return await _original_graphiti_extract_edges(
        clients,
        episode,
        extracted_nodes,
        previous_episodes,
        edge_type_map,
        group_id,
        edge_types,
        custom_extraction_instructions,
    )


def _patch_graphiti_combined_extraction() -> None:
    """Use Graphiti's typed combined extractor for single-episode ``add_episode``.

    Graphiti 0.29 documents the combined extractor as the path that prevents orphaned
    nodes by extracting entities and their relationships in one response, but its
    single-episode API still calls the older separate functions. Menhir's repeated
    extraction gate showed 10/10 capture for the live suburbs/downtown failure class,
    versus 0/10 for the separate path, with both false-positive controls held flat.

    The edge result is carried across node resolution in a ContextVar so concurrent
    namespaces cannot see each other's extraction state. Custom edge schemas fall back
    to Graphiti's original edge extractor because the node-stage signature does not
    expose those schemas to the combined call.
    """

    global _original_graphiti_extract_edges
    global _original_graphiti_extract_nodes
    global _graphiti_combined_extraction_module

    graphiti_module = None
    try:
        import graphiti_core.graphiti as graphiti_module

        if getattr(graphiti_module, "_menhir_combined_extraction_patched", False):
            return
        # Prove the replacement's own dependency FIRST, inside this guard. It used to be imported
        # lazily inside `_run_graphiti_combined_extraction`, where this except clause could not
        # reach it: the patch logged success and every add_episode then raised.
        from graphiti_core.utils.maintenance import combined_extraction as _combined_module

        _combined_module.extract_nodes_and_edges  # noqa: B018 - presence check, guarded above

        _original_graphiti_extract_nodes = graphiti_module.extract_nodes
        _original_graphiti_extract_edges = graphiti_module.extract_edges
        _graphiti_combined_extraction_module = _combined_module
        graphiti_module.extract_nodes = _extract_nodes_combined_for_add_episode
        graphiti_module.extract_edges = _extract_edges_from_combined_cache
        graphiti_module._menhir_combined_extraction_patched = True
        logger.debug("Graphiti single-episode combined extraction patch applied")
    except (ImportError, AttributeError) as exc:
        # Restore whatever was rebound before the failure, so a partial patch cannot leave
        # Graphiti pointing at a replacement whose dependency is missing. Without originals to
        # restore to, the old code left the process with no fallback at all.
        if graphiti_module is not None:
            if _original_graphiti_extract_nodes is not None:
                graphiti_module.extract_nodes = _original_graphiti_extract_nodes
            if _original_graphiti_extract_edges is not None:
                graphiti_module.extract_edges = _original_graphiti_extract_edges
        _original_graphiti_extract_nodes = None
        _original_graphiti_extract_edges = None
        _graphiti_combined_extraction_module = None
        logger.warning(
            "Failed to patch Graphiti combined extraction; left Graphiti on its own extractors: %s",
            exc,
        )
