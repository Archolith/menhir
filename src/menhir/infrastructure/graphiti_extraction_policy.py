"""Menhir extraction-receipt and canonical-self policy for the Graphiti fork.

This module is the Menhir-owned adapter half of the former combined-extraction
installers (#1/#2). The fork owns the routing mechanism
(``graphiti_core.extraction_routing``) and generic malformed-row sanitation
(``CombinedExtraction.sanitize_malformed_rows``); this module owns the Menhir
policy that rides on the fork's explicit ``SingleEpisodeExtractionHook``:

- per-episode extraction receipts (raw -> final counts, binding inputs),
- relation-completeness / endpoint-marker extraction instructions,
- Menhir payload sanitation (marker suppression, self-echo suppression,
  endpoint closure, titled-list synthesis, grounding guards),
- relationless repair and author-endpoint correction retries,
- canonical-self binding before resolution,

without rebinding any Graphiti symbol.
"""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import logging
import re
from typing import Any, Callable

from graphiti_core.extraction_routing import (
    ExtractionRoute,
    SingleEpisodeExtractionResult,
)
from graphiti_core.utils.maintenance.combined_extraction import extract_nodes_and_edges

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    SUBJECT_ENDPOINT_MARKER_PREFIX,
    SelfEvidenceKind,
    SelfIdentityContext,
    SelfSubjectEndpointEnvelope,
    declare_self_subject,
    is_self_alias,
    proves_self_subject,
)
from menhir.infrastructure.self_binding import (
    AmbiguousSelfBindingError,
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
    SelfBindOutcome,
    SelfBindResult,
    bind_canonical_self,
)
from menhir.infrastructure.edge_containment import prune_contained_edges
from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX
from menhir.infrastructure.model_profiles import ModelProfile, resolve_model_profile

logger = logging.getLogger(__name__)


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
    #: Edges restated inside a fuller edge, and the nodes that left orphaned (profile-gated).
    contained_edges_pruned: int = 0
    contained_orphans_dropped: int = 0
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


#: Relation emitted for a titled list. The list SYNTAX states membership -- "agents names below:"
#: followed by seven names asserts those are the agents -- so parsing it is reading the turn, not
#: inferring from it. Kept as one explicit relation rather than a guessed verb.
_MEMBERSHIP_RELATION = "MEMBER_OF"

#: A title line is `<title>:` optionally followed by the FIRST item on the same line. Real turns are
#: typed without care: the roster that motivated this is written `agents names below:Admon\nMagdy...`
#: with no newline after the colon, so requiring the colon to END the line refused the very case this
#: exists for. The colon itself is the load-bearing marker -- an explicit author-written "a list
#: follows" -- and the per-item guards below are what keep prose out, not the line break.
_LIST_TITLE_RE = re.compile(r"^(?P<title>[^:]{2,60}?)\s*:\s*(?P<first>.*)$")

#: Leading bullet/number decoration stripped from an item before it becomes an entity name.
_LIST_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")

#: An item must look like a NAME, not a sentence. Verbs and sentence punctuation disqualify the whole
#: block -- one prose line is enough to refuse, because a half-parsed list is worse than none.
_LIST_ITEM_MAX_WORDS = 6

#: Verbs that disqualify an item (and therefore the whole block) under the "items are NAMES, not
#: clauses" rule. A closed, conservative allowlist matched at word boundaries.
_LIST_VERBS: tuple[str, ...] = (
    "buy", "bought", "buying", "purchase", "purchased", "purchasing",
    "walk", "walked", "walking",
    "call", "called", "calling",
    "fix", "fixed", "fixing",
    "ship", "shipped", "shipping",
    "eat", "ate", "eating",
    "get", "got", "getting",
    "make", "made", "making",
    "go", "went", "going",
    "run", "ran", "running",
    "do", "did", "doing",
    "take", "took", "taking",
    "see", "saw", "seen", "seeing",
    "say", "said", "saying",
    "have", "had", "having",
    "finish", "finished", "finishing",
    "complete", "completed", "completing",
    "work", "worked", "working",
    "read", "reading",
    "write", "wrote", "written", "writing",
)
#: Personal pronouns. A NAME does not begin with one; a clause does.
_LIST_CLAUSE_PRONOUNS: tuple[str, ...] = (
    "i", "we", "you", "he", "she", "they", "it", "my", "our", "your", "their",
)

#: An item that BEGINS with a verb or a pronoun and continues is a clause, not a name.
_LIST_VERB_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(w) for w in (*_LIST_VERBS, *_LIST_CLAUSE_PRONOUNS))
    + r")\s+\S",
    re.IGNORECASE,
)


def parse_titled_list(episode_text: str) -> tuple[str, list[str]] | None:
    """Parse ``title:\n item\n item...`` into (title, items), or None when it is not clearly a list.

    DELIBERATELY STRICT -- refusing a real list costs one enrichment that behaves exactly as it does
    today, while accepting prose invents membership edges that are silently wrong.

      * the first line must contain ':' -- an explicit author-written "a list follows" marker
      * at least 3 items, so a colon in ordinary prose cannot produce a two-node "list"
      * every item <= 6 words, free of sentence punctuation, and free of allowlisted verbs
        (`_LIST_VERBS`) -- items are NAMES, not clauses
      * ONE non-conforming item refuses the WHOLE block (no partial parse)

    The FIRST ITEM MAY SIT ON THE TITLE LINE (`agents names below:Admon`). Returns names exactly as
    written, minus bullet decoration; deduplication is left to resolution.
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


def is_policy_empty_extraction(receipt: "CombinedExtractionReceipt | None") -> bool:
    """True when an empty extraction is an intentional no-op, not a collapse to be retried.

    An assistant turn that only restates the human's own facts (`user -> X`) has every edge
    suppressed by design, which leaves nothing to persist. That is the CORRECT outcome, and it is
    deterministic. Real collapses remain visible because either every surviving raw edge must be
    accounted for as echo, or every relationless entity must be a self label on an explicitly
    prefixed assistant turn.
    """
    if receipt is None:
        return False
    if receipt.assistant_self_only_relationless:
        return True
    if (receipt.relationless_repair_attempted
            and not receipt.relationless_repair_succeeded
            and receipt.initial_self_only_entities
            and receipt.repair_self_only_entities):
        return True
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


def get_extraction_receipt() -> CombinedExtractionReceipt | None:
    """Return the active extraction receipt for this task, if any."""
    return _extraction_receipt.get()


def clear_extraction_receipt() -> None:
    """Deactivate the extraction receipt (consume-once semantics)."""
    _extraction_receipt.set(None)


def _normalize_endpoint_name(name: Any) -> str:
    """Match graphiti's exact node-name normalization so endpoint checks agree with resolution."""
    from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact

    return _normalize_string_exact(str(name))


def _active_subject_marker(receipt: CombinedExtractionReceipt | None) -> str:
    endpoint = receipt.self_subject_endpoint if receipt is not None else None
    return endpoint.marker if endpoint is not None else ""


def _is_reserved_subject_marker(value: Any) -> bool:
    return str(value or "").casefold().startswith(
        SUBJECT_ENDPOINT_MARKER_PREFIX.casefold()
    )


def _subject_marker_guard_active(receipt: CombinedExtractionReceipt | None) -> bool:
    return receipt is not None and receipt.self_bind_mode is SelfBindMode.ENFORCE


# Pronoun / role-label endpoints that must never be synthesized as KG identities.
_NON_SYNTHESIZABLE_ENDPOINTS = frozenset(
    {
        "i", "me", "my", "mine", "myself",
        "we", "us", "our", "ours", "ourselves",
        "you", "your", "yours", "yourself", "yourselves",
        "he", "him", "his", "himself",
        "she", "her", "hers", "herself",
        "it", "its", "itself",
        "they", "them", "their", "theirs", "themselves",
        "this", "that", "these", "those",
        "who", "whom", "whose", "which", "what",
        "user", "the user", "assistant", "the assistant", "system",
        "someone", "somebody", "anyone", "anybody",
        "everyone", "everybody", "no one", "nobody", "none",
    }
)


#: Canonical self-entity display name. Mirrors `menhir.services.typed_scalar_rules
#: .SELF_SUBJECT_DISPLAY` deliberately by value rather than by import: infrastructure must not
#: depend on services.
_SELF_ENTITY_NAME = "user"

#: Labels denoting the HUMAN. DOMAIN: extracted entity NAMES.
_SELF_THIRD_PERSON = frozenset({"user", "the user"})
_SELF_FIRST_PERSON = frozenset({"i", "me", "my", "mine", "myself"})
_ASSISTANT_POLICY_SELF_LABELS = _SELF_THIRD_PERSON | _SELF_FIRST_PERSON


def _episode_role(episode_text: str) -> str:
    """'user' | 'assistant' | 'unknown' from the turn prefix the ingest writes."""
    head = str(episode_text or "").lstrip().lower()
    if head.startswith("user:"):
        return "user"
    if head.startswith("assistant:"):
        return "assistant"
    return "unknown"


def _is_unresolved_self_like_endpoint(normalized_name: str, episode_text: str) -> bool:
    """True when endpoint closure may retain this as an ORDINARY self-like entity.

    This helper does **not** establish identity and does **not** assign the canonical UUID. It only
    rewrites equivalent endpoint spellings to the display name ``user`` and lets ordinary Graphiti
    resolution decide where that node goes.

    ASSISTANT TURNS ARE EXCLUDED. A ``user -> X`` edge on an assistant turn is the model restating
    what the human already said in their own turn.
    """
    if _episode_role(episode_text) == "assistant":
        return False
    return normalized_name in _SELF_THIRD_PERSON or normalized_name in _SELF_FIRST_PERSON


def _contains_token_sequence(haystack: list[str], needle: list[str]) -> bool:
    """True when `needle` appears as a contiguous run of whole tokens in `haystack` (CF-192)."""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    span = len(needle)
    for i, token in enumerate(haystack):
        if token == first and haystack[i:i + span] == needle:
            return True
    return False


def _is_synthesizable_endpoint(
    name: Any,
    episode_text: str,
    previous_episode_texts: tuple[str, ...] = (),
) -> bool:
    """Return True when a missing edge endpoint may be materialized as a new entity.

    Conservative on purpose: reject pronoun/role labels outright, and — when extractor
    grounding text is available — require the name to appear literally (whole tokens) in either
    the current episode or the previous episodes Graphiti included in the extraction prompt.
    """
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    if not stripped:
        return False
    if _normalize_endpoint_name(stripped) in _NON_SYNTHESIZABLE_ENDPOINTS:
        return False
    grounding_texts = (episode_text, *previous_episode_texts)
    available_grounding = tuple(
        text for text in grounding_texts if isinstance(text, str) and text
    )
    if available_grounding:
        name_tokens = [t.casefold() for t in _CURRENT_MESSAGE_TOKEN_RE.findall(stripped)]
        if not name_tokens:
            return False
        for text in available_grounding:
            text_tokens = [t.casefold() for t in _CURRENT_MESSAGE_TOKEN_RE.findall(text)]
            if _contains_token_sequence(text_tokens, name_tokens):
                return True
        return False
    return True


def _sanitize_combined_entity(item: Any) -> dict[str, Any] | None:
    """Normalize one raw extracted-entity row, or return None if unusable."""
    if not isinstance(item, dict):
        return None
    item = dict(item)
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        # Tolerate the Qwen/DeepSeek key variants the separate-path patch also handles.
        for alt in ("entity_name", "entity"):
            alt_val = item.get(alt)
            if isinstance(alt_val, str) and alt_val.strip():
                name = alt_val
                break
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        type_id = int(item.get("entity_type_id"))
    except (TypeError, ValueError):
        type_id = -1  # generic Entity (upstream maps out-of-range -> "Entity")
    return {"name": name.strip(), "entity_type_id": type_id}


def _sanitize_combined_edge(item: Any) -> dict[str, Any] | None:
    """Normalize one raw edge row, or return None when an indispensable field is missing."""
    if not isinstance(item, dict):
        return None
    item = dict(item)
    cleaned: dict[str, Any] = {}
    for key in ("source_entity_name", "target_entity_name", "relation_type", "fact"):
        val = item.get(key)
        if not isinstance(val, str) or not val.strip():
            return None  # missing/blank endpoint, relation, or fact -> drop this edge only
        cleaned[key] = val
    idx = item.get("episode_indices")
    if isinstance(idx, list):
        clean_idx = [i for i in idx if isinstance(i, int) and not isinstance(i, bool)]
        cleaned["episode_indices"] = clean_idx or [0]
    else:
        cleaned["episode_indices"] = [0]
    return cleaned


_CURRENT_MESSAGE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CURRENT_MESSAGE_ANCHOR_STOPWORDS = frozenset(
    {
        "a",
        "advice",
        "an",
        "and",
        "are",
        "assistant",
        "be",
        "been",
        "being",
        "for",
        "fine",
        "from",
        "good",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "okay",
        "on",
        "or",
        "our",
        "point",
        "sounds",
        "starting",
        "sure",
        "thank",
        "thanks",
        "that",
        "the",
        "this",
        "to",
        "think",
        "user",
        "was",
        "we",
        "were",
        "with",
        "yes",
        "you",
        "your",
    }
)


def _current_message_anchor_tokens(episode_text: str) -> set[str]:
    """Meaningful literal tokens an assisted repair must carry back into each emitted edge."""

    current = str(episode_text or "")
    role, separator, body = current.partition(":")
    if separator and role.strip().casefold() in {"user", "assistant", "tool", "agent"}:
        current = body
    return {
        token
        for token in (
            raw.casefold() for raw in _CURRENT_MESSAGE_TOKEN_RE.findall(current)
        )
        if (token.isdigit() or len(token) >= 3)
        and token not in _CURRENT_MESSAGE_ANCHOR_STOPWORDS
    }


#: Edge fields that may serve as EVIDENCE that an edge is grounded in the current turn.
#: `relation_type` is deliberately absent (model-supplied boilerplate).
_EDGE_ANCHOR_EVIDENCE_FIELDS = ("source_entity_name", "target_entity_name", "fact")


def _edge_has_current_message_anchor(edge: dict[str, Any], episode_text: str) -> bool:
    """True when the edge shares a meaningful token with the CURRENT turn."""
    current_tokens = _current_message_anchor_tokens(episode_text)
    if not current_tokens:
        return False
    edge_text = " ".join(
        str(edge.get(field) or "") for field in _EDGE_ANCHOR_EVIDENCE_FIELDS
    )
    edge_tokens = {
        token.casefold() for token in _CURRENT_MESSAGE_TOKEN_RE.findall(edge_text)
    }
    return bool(current_tokens & edge_tokens)


def _sanitize_combined_payload(
    data: Any,
    receipt: CombinedExtractionReceipt | None,
    episode_text: str,
) -> Any:
    """Sanitize a raw combined-extraction payload and close missing edge endpoints.

    Order (per remediation contract): record raw counts -> drop malformed edge rows
    -> normalize extracted entities -> add missing usable edge endpoints -> hand back
    to Graphiti for its normal resolution. Runs BEFORE ``CombinedExtraction`` is
    validated so a single malformed row cannot invalidate the whole batch.
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)
    raw_entities = data.get("extracted_entities")
    raw_edges = data.get("edges")
    raw_entities = raw_entities if isinstance(raw_entities, list) else []
    raw_edges = raw_edges if isinstance(raw_edges, list) else []

    if receipt is not None:
        receipt.raw_entity_count = len(raw_entities)
        receipt.raw_edge_count = len(raw_edges)

    entities: list[dict[str, Any]] = []
    entities_dropped = 0
    for item in raw_entities:
        norm = _sanitize_combined_entity(item)
        if norm is None:
            entities_dropped += 1
            continue
        marker = _active_subject_marker(receipt)
        if (
            _subject_marker_guard_active(receipt)
            and SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in norm["name"].casefold()
            and norm["name"] != marker
        ):
            # A stale, malformed, or model-invented reserved endpoint is never an ordinary entity.
            entities_dropped += 1
            continue
        entities.append(norm)

    edges: list[dict[str, Any]] = []
    edges_dropped = 0
    subject_marker_edges_suppressed = 0
    for item in raw_edges:
        norm = _sanitize_combined_edge(item)
        if norm is None:
            edges_dropped += 1
            continue
        marker = _active_subject_marker(receipt)
        if _subject_marker_guard_active(receipt) and any(
            _is_reserved_subject_marker(norm[key]) and norm[key] != marker
            for key in ("source_entity_name", "target_entity_name")
        ):
            edges_dropped += 1
            continue
        if _subject_marker_guard_active(receipt):
            endpoint_uses_marker = any(
                norm[key] == marker
                for key in ("source_entity_name", "target_entity_name")
            )
            marker_text = " ".join(
                norm[key] for key in ("relation_type", "fact")
            )
            marker_occurs_in_text = (
                SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in marker_text.casefold()
            )
            active_marker_occurs = bool(
                marker and marker.casefold() in marker_text.casefold()
            )
            foreign_marker_occurs = SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in (
                re.sub(re.escape(marker), "", marker_text, flags=re.IGNORECASE)
                if marker else marker_text
            ).casefold()
            if marker_occurs_in_text and (
                not endpoint_uses_marker or not active_marker_occurs or foreign_marker_occurs
            ):
                edges_dropped += 1
                subject_marker_edges_suppressed += 1
                continue
        edges.append(norm)

    context_unsupported_edges = 0
    if (
        receipt is not None
        and receipt.relationless_repair_attempted
        and receipt.relationless_repair_context_texts
        and edges
    ):
        grounded_edges = [
            edge
            for edge in edges
            if _edge_has_current_message_anchor(edge, episode_text)
        ]
        context_unsupported_edges = len(edges) - len(grounded_edges)
        edges = grounded_edges

    known = {_normalize_endpoint_name(e["name"]) for e in entities}
    self_key = _normalize_endpoint_name(_SELF_ENTITY_NAME)
    is_assistant_turn = _episode_role(episode_text) == "assistant"
    _all_self_labels = bool(
        entities
        and not raw_edges
        and entities_dropped == 0
        and all(
            _normalize_endpoint_name(entity["name"])
            in _ASSISTANT_POLICY_SELF_LABELS
            for entity in entities
        )
    )
    assistant_self_only_relationless = bool(is_assistant_turn and _all_self_labels)
    synthesized = 0
    self_like_endpoints_retained = 0
    self_echo_edges = 0
    surviving_edges: list[dict[str, Any]] = []
    for edge in edges:
        edge_is_self_echo = False
        for endpoint_key in ("source_entity_name", "target_entity_name"):
            endpoint_name = edge[endpoint_key]
            norm_key = _normalize_endpoint_name(endpoint_name)
            if is_assistant_turn and (
                norm_key in _SELF_THIRD_PERSON or norm_key in _SELF_FIRST_PERSON
            ):
                # This is the assistant restating a fact the human already gave first-hand.
                edge_is_self_echo = True
                break
            if norm_key in known:
                continue
            marker = _active_subject_marker(receipt)
            if marker and endpoint_name == marker:
                # The marker is grounded by the receipt, not by user text.
                entities.append({"name": marker, "entity_type_id": -1})
                known.add(norm_key)
                synthesized += 1
                continue
            if _is_unresolved_self_like_endpoint(norm_key, episode_text):
                # Normalize the endpoint spelling and materialize it ONCE per payload so Graphiti
                # does not drop the edge. Availability recovery, not identity resolution.
                edge[endpoint_key] = _SELF_ENTITY_NAME
                if self_key not in known:
                    entities.append({"name": _SELF_ENTITY_NAME, "entity_type_id": -1})
                    known.add(self_key)
                self_like_endpoints_retained += 1
                continue
            previous_episode_texts = (
                (
                    *receipt.previous_episode_texts,
                    *receipt.relationless_repair_context_texts,
                )
                if receipt is not None
                else ()
            )
            if _is_synthesizable_endpoint(
                endpoint_name,
                episode_text,
                previous_episode_texts,
            ):
                entities.append({"name": endpoint_name.strip(), "entity_type_id": -1})
                known.add(norm_key)
                synthesized += 1
        if edge_is_self_echo:
            self_echo_edges += 1
            continue
        surviving_edges.append(edge)

    extractor_produced_edges = bool(edges)
    edges = surviving_edges

    # Titled list: the turn states membership through SYNTAX rather than a verb.
    list_edges_added = 0
    if not extractor_produced_edges:
        parsed = parse_titled_list(episode_text)
        if parsed is not None:
            container, items = parsed
            container_key = _normalize_endpoint_name(container)
            extracted_keys = {_normalize_endpoint_name(e["name"]) for e in entities}
            matched = [it for it in items if _normalize_endpoint_name(it) in extracted_keys]
            if len(matched) >= 3:
                if container_key not in extracted_keys:
                    entities.append({"name": container, "entity_type_id": -1})
                    extracted_keys.add(container_key)
                for item in matched:
                    synthetic = _sanitize_combined_edge({
                        "relation_type": _MEMBERSHIP_RELATION,
                        "source_entity_name": item,
                        "target_entity_name": container,
                        "fact": f"{SYNTHETIC_FACT_PREFIX}{item} is listed under {container}",
                        "episode_indices": [0],
                    })
                    if synthetic is None:      # unreachable today; fail closed rather than emit junk
                        continue
                    edges.append(synthetic)
                    list_edges_added += 1

    if receipt is not None:
        receipt.malformed_entities_dropped = entities_dropped
        receipt.malformed_edges_dropped = edges_dropped
        receipt.endpoints_synthesized = synthesized
        receipt.self_echo_edges_suppressed = self_echo_edges
        receipt.list_membership_edges_added = list_edges_added
        receipt.context_unsupported_edges_suppressed = context_unsupported_edges
        receipt.subject_marker_edges_suppressed = subject_marker_edges_suppressed
        receipt.assistant_self_only_relationless = assistant_self_only_relationless
        if receipt.relationless_repair_attempted:
            receipt.repair_self_only_entities = _all_self_labels
        else:
            receipt.initial_self_only_entities = _all_self_labels

    if (
        entities_dropped
        or edges_dropped
        or synthesized
        or self_like_endpoints_retained
        or self_echo_edges
        or list_edges_added
        or context_unsupported_edges
    ):
        logger.info(
            "Combined-extraction sanitation: entities_dropped=%d edges_dropped=%d "
            "endpoints_synthesized=%d self_like_endpoints_retained=%d "
            "self_echo_edges_suppressed=%d "
            "list_membership_edges_added=%d context_unsupported_edges_suppressed=%d "
            "(raw entities=%d edges=%d)",
            entities_dropped,
            edges_dropped,
            synthesized,
            self_like_endpoints_retained,
            self_echo_edges,
            list_edges_added,
            context_unsupported_edges,
            len(raw_entities),
            len(raw_edges),
        )

    data["extracted_entities"] = entities
    data["edges"] = edges
    return data


# ---------------------------------------------------------------------------
# Menhir extraction instructions
# ---------------------------------------------------------------------------


#: Subject-neutral half of the relation-completeness contract.
_RELATION_COMPLETENESS_CORE = """\
MENHIR RELATION COMPLETENESS:
- Do not return an entity without a relationship when CURRENT MESSAGES state what the subject
  does, owns, uses, prefers, plans, experiences, believes, or explicitly wants to learn about
  that entity.
- Do not invent a relationship merely to connect an entity. If the current text truly states no
  relationship, omit the entity as well.
"""

#: First-person half. Appended ONLY when the episode text actually contains a first-person
#: reference (`_is_first_person`).
_FIRST_PERSON_SELF_BINDING = """\
- In a human-authored first-person statement, represent I/me/my with the canonical entity `user`
  and emit the direct speaker-to-target relationship. Include `user` in extracted_entities.
- Example: "I'm actually using a new app I recently downloaded." must include entities `user`
  and `new app`, plus `user` -> `USES` -> `new app` with a self-contained fact.
- Explicit first-person informational intent is relationship-bearing. For example, "I'd like to
  know more about X", "I'm looking to learn more about X", or "I'm interested in understanding X"
  must emit `user` -> `WANTS_TO_KNOW_MORE_ABOUT` or `INTERESTED_IN` -> `X`.
- Apply that rule only when CURRENT MESSAGES explicitly state the speaker's informational intent.
  A bare request or question such as "Can you tell me about X?" does not by itself assert durable
  interest in X.
"""


def _first_person_self_binding(marker: str | None) -> str:
    """The first-person rules, bound to `user` or to an opaque endpoint marker."""
    if marker is None:
        return _FIRST_PERSON_SELF_BINDING
    return f"""\
- In a human-authored first-person statement, represent I/me/my with the exact opaque entity
  `{marker}` and emit the direct speaker-to-target relationship. Include
  `{marker}` in extracted_entities.
- Explicit first-person informational intent is relationship-bearing. Emit
  `{marker}` -> `WANTS_TO_KNOW_MORE_ABOUT` or `INTERESTED_IN` -> the target.
- Apply that rule only when CURRENT MESSAGES explicitly state the speaker's informational intent.
  A bare request or question such as "Can you tell me about X?" does not by itself assert durable
  interest in X.
"""


_DO_NOT_INVENT = "- Do not invent a relationship"


def _relation_completeness_instructions(
    endpoint: SelfSubjectEndpointEnvelope | None,
    episode_text: str,
) -> str:
    """Render the relation-completeness contract for one episode."""
    if not _is_first_person(episode_text):
        # NOTHING for third-person text -- not even the subject-neutral core.
        return ""
    core = _RELATION_COMPLETENESS_CORE
    marker = endpoint.marker if endpoint is not None else None
    head, sep, tail = core.partition(_DO_NOT_INVENT)
    return head + _first_person_self_binding(marker) + sep + tail


_RELATIONLESS_REPAIR_CORE = """\
CORRECTIVE RE-EXTRACTION:
Your previous extraction returned one or more entities but no usable relationship, so every entity
would be orphan-pruned and the memory would be lost. Re-read CURRENT MESSAGES and return a complete
entity-and-edge extraction. Do not invent facts. If the text truly contains no relationship, return
both lists empty.
"""

_REPAIR_FIRST_PERSON_USER = (
    'Pay special attention to first-person predicates such as "I use...", "I own...", '
    '"I prefer...", "I plan...", "I\'d like to know more about X", and "I\'m interested in '
    'understanding X"; bind a human first-person speaker to `user`. Explicit informational '
    "intent must emit `WANTS_TO_KNOW_MORE_ABOUT` or `INTERESTED_IN`. A bare request or "
    'question such as "Can you tell me about X?" does not by itself assert durable interest. '
)


def _relationless_repair_instructions(
    endpoint: SelfSubjectEndpointEnvelope | None,
    episode_text: str,
) -> str:
    """Repair-pass instructions, with the first-person rules gated the same way."""
    if not _is_first_person(episode_text):
        return _RELATIONLESS_REPAIR_CORE
    if endpoint is None:
        first_person = _REPAIR_FIRST_PERSON_USER
    else:
        first_person = (
            'For first-person predicates such as "I use...", "I own...", "I prefer...", or '
            f'"I plan...", bind the current human speaker to the exact opaque entity `{endpoint.marker}`. '
            "Explicit informational intent must emit `WANTS_TO_KNOW_MORE_ABOUT` or `INTERESTED_IN`. "
            "A bare request or question does not by itself assert durable interest. "
        )
    head, sep, tail = _RELATIONLESS_REPAIR_CORE.partition("Do not invent facts.")
    return head + first_person + sep + tail


def _subject_endpoint_correction_instructions(
    endpoint: SelfSubjectEndpointEnvelope,
) -> str:
    return f"""\
MENHIR INVALID AUTHOR-ENDPOINT CORRECTION:
- Your previous extraction used a self-like entity without the declared current-author endpoint.
- Discard that extraction and re-extract CURRENT MESSAGES.
- For every relationship whose subject or object is I/me/my or the current message's author, use
  the exact opaque entity name `{endpoint.marker}` as that endpoint.
- Do not emit `user`, `I`, `me`, or `my` as a substitute for the current author.
- Keep third-person users, roles, customers, and quoted or reported speakers distinct.
"""


# A refusal hint, NOT proof of authorship or subjecthood.
_AUTHOR_REFERENCE_RE = re.compile(r"\b(?:i|me|my|mine|myself)\b", re.IGNORECASE)


def _is_first_person(text: str) -> bool:
    """True when `text` contains a first-person singular reference."""
    return bool(_AUTHOR_REFERENCE_RE.search(text or ""))


def _unresolved_author_aliases(
    nodes: list[Any], receipt: CombinedExtractionReceipt,
) -> set[str]:
    if receipt.self_subject_endpoint is None:
        return set()
    names = [str(getattr(node, "name", "") or "") for node in nodes]
    if (receipt.self_subject_endpoint.marker not in names
            and not _is_first_person(receipt.episode_text)):
        return set()  # Ordinary third-person/RBAC-only `user` is not the author.
    return {
        str(getattr(node, "uuid", "") or "") for node in nodes
        if is_self_alias(getattr(node, "name", None))
    }


def _subject_endpoint_instructions(
    endpoint: SelfSubjectEndpointEnvelope | None,
) -> str | None:
    if endpoint is None:
        return None
    return f"""\
MENHIR STRUCTURAL CURRENT-MESSAGE AUTHOR ENDPOINT:
- The exact opaque entity name `{endpoint.marker}` denotes the author of CURRENT MESSAGES only.
- Use `{endpoint.marker}` as the endpoint for every relation asserted by I/me/my or the current
  message's author. Do not substitute a generic speaker label.
- Do not use the marker for a person speaking inside quoted or reported speech.
- Do not replace third-person users, customers, roles, tables, collections, or application actors
  with the marker.
- Preserve source-qualified names such as `application user`; a bare `user` in mixed author text
  is ambiguous. Omit an uncertain attribution rather than inventing a substitute author entity.
- Preserve negation. Questions, hypothetical statements, and other speakers' claims are not
  affirmative facts about the author. Relation interpretation is inference, not owner confirmation.
- Emit `{endpoint.marker}` only when at least one extracted edge about the current author uses it.
"""

_RELATIONLESS_REPAIR_CONTEXT_INSTRUCTIONS = """\
ADJACENT TRANSCRIPT CONTEXT:
PREVIOUS MESSAGES are context, not current claims. Use them only to resolve what a pronoun,
shorthand reply, bare choice, or bare number in CURRENT MESSAGES refers to. If the current speaker
selects a value offered in PREVIOUS MESSAGES, that selection is a current claim; recover its subject
and unit from the context. Emit relationships only for claims or choices made in CURRENT MESSAGES.
Do not extract a claim merely because it appears in PREVIOUS MESSAGES.
"""

_RELATIONLESS_REPAIR_CONTEXT_MAX_CHARS = 6000


def _combine_extraction_instructions(*parts: str | None) -> str:
    """Append Menhir instructions without discarding a caller's custom extraction contract."""
    return "\n\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def _model_profile_for_clients(clients: Any) -> ModelProfile:
    """Profile of the extraction LLM; anything without a string model name gets the default."""
    llm = getattr(clients, "llm_client", None)
    model = getattr(llm, "model", None)
    base_url = getattr(getattr(llm, "config", None), "base_url", None)
    return resolve_model_profile(
        model if isinstance(model, str) else None,
        endpoint=base_url if isinstance(base_url, str) else None,
    )


def _load_relationless_repair_context(
    receipt: CombinedExtractionReceipt,
) -> tuple[str, ...]:
    """Load and bound adjacent transcript turns once, failing open to the existing repair path."""

    loader = receipt.relationless_repair_context_loader
    receipt.relationless_repair_context_loader = None
    if loader is None:
        return ()
    try:
        loaded = loader()
    except Exception:
        logger.warning(
            "Unable to load adjacent transcript context for relationless repair episode_id=%s",
            receipt.episode_key,
            exc_info=True,
        )
        return ()

    remaining = _RELATIONLESS_REPAIR_CONTEXT_MAX_CHARS
    bounded_reversed: list[str] = []
    for raw_text in reversed(tuple(loaded or ())):
        text = str(raw_text or "").strip()
        if not text or remaining <= 0:
            continue
        if len(text) > remaining:
            text = text[-remaining:]
        bounded_reversed.append(text)
        remaining -= len(text)
    return tuple(reversed(bounded_reversed))


#: The section delimiters graphiti's prompt templates wrap `previous_episodes` in (CF-194).
_PROMPT_SECTION_TAGS = ("<PREVIOUS MESSAGES>", "</PREVIOUS MESSAGES>",
                        "<CURRENT MESSAGE>", "</CURRENT MESSAGE>")


def _neutralize_prompt_delimiters(text: str) -> str:
    """Defang the prompt's own structural tags inside attacker-influenced context text."""
    out = text
    for tag in _PROMPT_SECTION_TAGS:
        if tag.lower() in out.lower():
            lowered, needle, cursor, pieces = out.lower(), tag.lower(), 0, []
            while True:
                hit = lowered.find(needle, cursor)
                if hit == -1:
                    pieces.append(out[cursor:])
                    break
                pieces.append(out[cursor:hit])
                pieces.append(out[hit:hit + len(tag)].replace("<", "(").replace(">", ")"))
                cursor = hit + len(tag)
            out = "".join(pieces)
            lowered = out.lower()
    return out


def _relationless_repair_previous_episodes(
    episode: Any,
    previous_episodes: list[Any],
    context_texts: tuple[str, ...],
) -> list[Any]:
    """Append raw adjacent turns through Graphiti's native previous-episode prompt channel."""

    if not context_texts:
        return previous_episodes

    from graphiti_core.nodes import EpisodeType, EpisodicNode
    from graphiti_core.utils.datetime_utils import utc_now

    episodes = episode if isinstance(episode, list) else [episode]
    primary_episode = episodes[0]
    now = utc_now()
    valid_at = getattr(primary_episode, "valid_at", None) or now
    created_at = getattr(primary_episode, "created_at", None) or valid_at
    repair_context_episodes = [
        EpisodicNode(
            name=f"menhir-relationless-repair-context-{index}",
            group_id=str(getattr(primary_episode, "group_id", "") or ""),
            labels=[],
            source=EpisodeType.message,
            source_description="menhir_relationless_repair_context",
            content=_neutralize_prompt_delimiters(text),
            created_at=created_at,
            valid_at=valid_at,
        )
        for index, text in enumerate(context_texts)
    ]
    return [*(previous_episodes or []), *repair_context_episodes]


def _needs_relationless_repair(
    receipt: CombinedExtractionReceipt | None,
    edges: list[Any],
) -> bool:
    """True only for an entity-bearing, edge-empty first pass that sanitation could not repair."""
    return bool(
        receipt is not None
        and receipt.raw_entity_count > 0
        and receipt.raw_edge_count == 0
        and receipt.list_membership_edges_added == 0
        and not receipt.assistant_self_only_relationless
        and not edges
    )


def _edge_endpoint_uuids(edge: Any) -> set[str]:
    return {str(getattr(edge, "source_node_uuid", "") or ""),
            str(getattr(edge, "target_node_uuid", "") or "")} - {""}


def _bind_subject_endpoint(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    receipt: CombinedExtractionReceipt,
) -> SelfBindResult:
    """Attach inferred relationships to the preallocated author, atomically before dedup."""
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
    """Run the binding decision and record it, without letting telemetry break extraction."""
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


def _record_self_binding_decision(
    result: SelfBindResult, receipt: CombinedExtractionReceipt
) -> None:
    try:
        from menhir.infrastructure.telemetry.recorders import record_lifecycle_event

        record_lifecycle_event(
            component="self_binding",
            event="canonical_self_decision",
            state=str(result.outcome),
            episode_uuid=receipt.episode_key or None,
            details=result.telemetry_details(receipt.self_identity),
        )
    except Exception:  # noqa: BLE001 - observability must never fail an ingest
        logger.exception("Failed to record canonical-self binding telemetry")


# ---------------------------------------------------------------------------
# Menhir single-episode extraction hook (fork extension point)
# ---------------------------------------------------------------------------


class _ClientsView:
    """Per-call clients view whose ``llm_client`` is the sanitizing proxy.

    Everything else delegates to the real clients bundle. This is composition over
    a per-call argument — the real bundle is never mutated.
    """

    def __init__(self, clients: Any, llm_client: Any) -> None:
        self._clients = clients
        self.llm_client = llm_client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._clients, name)


class _PayloadSanitizingLLMClient:
    """Per-call LLM-client view that applies Menhir payload sanitation.

    The fork's ``extract_nodes_and_edges`` validates the raw model payload through
    ``CombinedExtraction``. Menhir's payload policy (marker suppression, echo
    suppression, endpoint closure, titled-list synthesis, grounding guards) must run
    BEFORE that validation, so the extraction hook passes the extractor a shallow
    per-call copy of the clients bundle whose ``llm_client`` is this proxy. The proxy
    delegates everything to the real client and rewrites only the combined-extraction
    response dict. This is composition over a per-call argument — no Graphiti symbol
    is rebound and the real clients bundle is not mutated.
    """

    def __init__(self, inner: Any, receipt: CombinedExtractionReceipt) -> None:
        self._clients = inner
        self._receipt = receipt

    @property
    def _inner(self) -> Any:
        return self._clients.llm_client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def generate_response(self, messages: Any, response_model: Any = None, **kwargs: Any) -> Any:
        response = await self._inner.generate_response(
            messages, response_model=response_model, **kwargs
        )
        if (
            response_model is not None
            and getattr(response_model, "__name__", "") == "CombinedExtraction"
            and isinstance(response, dict)
        ):
            return _sanitize_combined_payload(response, self._receipt, self._receipt.episode_text)
        return response


async def _run_graphiti_combined_extraction(
    clients: Any,
    episode: Any,
    previous_episodes: list[Any],
    entity_types: Any,
    excluded_entity_types: Any,
    custom_extraction_instructions: str | None,
    receipt: CombinedExtractionReceipt | None = None,
) -> tuple[list[Any], list[Any], dict[str, list[int]]]:
    """Run combined extraction under the active receipt's Menhir policy."""
    if receipt is None:
        receipt = get_extraction_receipt()
    assert receipt is not None
    receipt.graphiti_episode_uuid = str(getattr(episode, "uuid", "") or "").strip()
    receipt.previous_episode_texts = tuple(
        content
        for item in (previous_episodes or [])
        if isinstance((content := getattr(item, "content", None)), str)
        and content.strip()
    )

    declared_endpoint = receipt.self_subject_endpoint
    endpoint = declared_endpoint  # Transport exists regardless of grammar or model output.
    if declared_endpoint is not None:
        if receipt.relationless_repair_context_loader is not None:
            receipt.relationless_repair_context_texts = _load_relationless_repair_context(
                receipt
            )
        collision_texts = (
            receipt.episode_text,
            *receipt.previous_episode_texts,
            *receipt.relationless_repair_context_texts,
        )
        if any(
            SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in text.casefold()
            for text in collision_texts
        ):
            raise InvalidSelfSubjectDeclarationError(
                "reserved self-subject marker prefix occurs in extraction text or context"
            )
    endpoint_instructions = _subject_endpoint_instructions(endpoint)

    # The repair pass builds on effective_instructions, so the profile block reaches both passes.
    effective_instructions = _combine_extraction_instructions(
        custom_extraction_instructions,
        _relation_completeness_instructions(endpoint, receipt.episode_text),
        endpoint_instructions,
        _model_profile_for_clients(clients).extraction_instructions(),
    )

    def _extract(
        previous: list[Any],
        instructions: str | None,
    ) -> Any:
        proxied_clients = _ClientsView(
            clients, _PayloadSanitizingLLMClient(clients, receipt)
        )
        return extract_nodes_and_edges(
            proxied_clients,
            episode,
            previous,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=instructions,
        )

    nodes, edges, index_map = await _extract(previous_episodes, effective_instructions)
    if _needs_relationless_repair(receipt, edges):
        receipt.relationless_repair_attempted = True
        receipt.relationless_initial_entity_count = receipt.raw_entity_count
        receipt.relationless_initial_edge_count = receipt.raw_edge_count
        if not receipt.relationless_repair_context_texts:
            receipt.relationless_repair_context_texts = _load_relationless_repair_context(
                receipt
            )
        if declared_endpoint is not None and any(
            SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in text.casefold()
            for text in receipt.relationless_repair_context_texts
        ):
            raise InvalidSelfSubjectDeclarationError(
                "reserved self-subject marker prefix occurs in repair context"
            )
        logger.warning(
            "Relationless combined extraction; running one corrective retry "
            "episode_id=%s raw_entities=%d raw_edges=%d source=%s adjacent_context_turns=%d",
            receipt.episode_key,
            receipt.raw_entity_count,
            receipt.raw_edge_count,
            receipt.source_description,
            len(receipt.relationless_repair_context_texts),
        )
        repair_instructions = _combine_extraction_instructions(
            effective_instructions,
            _relationless_repair_instructions(endpoint, receipt.episode_text),
            (
                _RELATIONLESS_REPAIR_CONTEXT_INSTRUCTIONS
                if receipt.relationless_repair_context_texts
                else None
            ),
            endpoint_instructions,
        )
        repair_previous_episodes = _relationless_repair_previous_episodes(
            episode,
            previous_episodes,
            receipt.relationless_repair_context_texts,
        )
        nodes, edges, index_map = await _extract(
            repair_previous_episodes, repair_instructions
        )
        receipt.relationless_repair_succeeded = bool(edges)
        if not edges:
            receipt.raw_entity_count = max(
                receipt.raw_entity_count,
                receipt.relationless_initial_entity_count,
            )
            receipt.raw_edge_count = max(
                receipt.raw_edge_count,
                receipt.relationless_initial_edge_count,
            )
        logger.info(
            "Relationless combined extraction repair complete "
            "episode_id=%s succeeded=%s raw_entities=%d raw_edges=%d",
            receipt.episode_key,
            receipt.relationless_repair_succeeded,
            receipt.raw_entity_count,
            receipt.raw_edge_count,
        )
    if (
        endpoint is not None
        and _unresolved_author_aliases(nodes, receipt)
    ):
        # Real models can privilege a familiar `user` convention even when a later instruction
        # declares a safer opaque endpoint. One bounded correction.
        logger.warning(
            "Eligible extraction used an undeclared self-like endpoint; running one corrective "
            "retry episode_id=%s",
            receipt.episode_key,
        )
        correction_instructions = _combine_extraction_instructions(
            effective_instructions,
            _subject_endpoint_correction_instructions(endpoint),
            endpoint_instructions,
        )
        correction_previous_episodes = _relationless_repair_previous_episodes(
            episode,
            previous_episodes,
            receipt.relationless_repair_context_texts,
        )
        nodes, edges, index_map = await _extract(
            correction_previous_episodes, correction_instructions
        )
    # Bind the proven human AFTER the repair branches above: a repair re-runs extraction and
    # replaces nodes/edges/index_map wholesale.
    if receipt.self_identity is not None:
        receipt.self_bind_result = _record_self_binding(
            nodes, edges, index_map, receipt
        )

    receipt.resolved_node_count = len(nodes)
    receipt.resolved_edge_count = len(edges)
    surviving_inputs = (
        receipt.raw_entity_count
        - receipt.malformed_entities_dropped
        + receipt.endpoints_synthesized
    )
    receipt.orphan_nodes_dropped = max(0, surviving_inputs - len(nodes))
    if _model_profile_for_clients(clients).prune_contained_edges:
        # After binding, so the bound author node is known and never pruned as an orphan.
        protected = {
            uuid
            for uuid in (
                getattr(receipt.self_bind_result, "self_uuid", None),
                getattr(receipt.self_subject_node, "uuid", None),
            )
            if uuid
        }
        pruned, dropped = prune_contained_edges(nodes, edges, index_map, protected)
        receipt.contained_edges_pruned = pruned
        receipt.contained_orphans_dropped = dropped
        receipt.resolved_node_count = len(nodes)
        receipt.resolved_edge_count = len(edges)
        if pruned:
            logger.info(
                "Pruned contained edges episode_id=%s edges=%d orphan_nodes=%d",
                receipt.episode_key, pruned, dropped,
            )
    return nodes, edges, index_map


class MenhirExtractionHook:
    """Menhir policy adapter on the fork's ``SingleEpisodeExtractionHook`` seam.

    With no active extraction receipt the hook returns ``None`` so the fork's default
    routing applies untouched. With an active receipt it performs the combined
    extraction itself under Menhir's payload/instruction/binding policy and supplies
    the result to the fork through ``SingleEpisodeExtractionResult``; edges then flow
    into native resolution as ``precomputed_edges``. Episodes with custom edge
    schemas keep the fork's SEPARATE compatibility route.
    """

    async def extract_single_episode(self, context: Any) -> Any:
        from menhir.infrastructure.graphiti_resolution_policy import (
            start_resolution_telemetry,
        )

        receipt = get_extraction_receipt()
        if receipt is None:
            return None
        if context.edge_types:
            return ExtractionRoute.SEPARATE
        start_resolution_telemetry()
        nodes, edges, index_map = await _run_graphiti_combined_extraction(
            context.clients,
            context.episode,
            context.previous_episodes,
            context.entity_types,
            context.excluded_entity_types,
            context.custom_extraction_instructions,
            receipt,
        )
        return SingleEpisodeExtractionResult(
            nodes=nodes,
            edges=edges,
            node_episode_index_map=index_map,
        )
