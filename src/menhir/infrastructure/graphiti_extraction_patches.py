"""Combined Graphiti extraction and extraction-receipt compatibility patches."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
import re
from time import perf_counter
from typing import Any, Callable

from menhir.domain.self_identity import (
    SUBJECT_ENDPOINT_MARKER_PREFIX,
    SelfEvidenceKind,
    SelfIdentityContext,
    SelfSubjectEndpointEnvelope,
    declare_self_subject,
    is_self_alias,
    self_uuid_for_namespace,
)
from menhir.domain.self_authority import (
    SELF_ASSERTION_EDGE_EPISODE_PROPERTY,
    SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY,
    SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY,
    SELF_ASSERTION_POLICY_VERSION,
    SelfAssertionProposal,
    SelfAuthorizationDecision,
    canonical_json_bytes,
    canonical_temporal_value,
    make_self_assertion_proposal,
    proposal_from_confirmation_payload,
    proposal_matches_persisted_edge,
)
from menhir.infrastructure.self_binding import (
    AmbiguousSelfBindingError,
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
    SelfBindOutcome,
    SelfBindResult,
    bind_canonical_self,
)
from menhir.infrastructure.graphiti_helpers import (
    SYNTHETIC_FACT_PREFIX,
    _build_graphiti_failure_details,
    _describe_openai_client_base_url,
    _extract_first_json_payload,
    _normalize_graphiti_json_payload,
    _raw_preview,
    check_graphiti_version,
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
    #: Rollout control for this episode. ``OFF`` reproduces pre-change behavior exactly.
    self_bind_mode: "SelfBindMode" = SelfBindMode.OFF
    #: Outcome of the binding attempt, or ``None`` if binding never ran. Read by the resolver
    #: partition to know which UUID is already authoritative and must skip candidate search.
    self_bind_result: "SelfBindResult | None" = None
    #: Read-only, non-signing authority source. It may verify an exact owner-signed proposal; it
    #: cannot create a confirmation. Absence is the safe default and leaves every self edge as a
    #: proposal only.
    self_assertion_authorizer: Any | None = None
    #: Durable audit records for every final-payload edge that attempted to attach to the current
    #: speaker endpoint. Unauthorized records are removed from the Graphiti payload before dedup.
    self_assertion_proposals: list[dict[str, Any]] | None = None
    self_assertions_authorized: int = 0
    #: Object identities of edges authorized by this receipt. This in-memory capability prevents a
    #: model-authored lookalike attribute from activating the resolver preservation patch below.
    self_assertion_authorized_edge_ids: set[int] = field(default_factory=set)
    #: Marker edges remain proposal-local until ordinary node resolution has selected a persistent
    #: counterpart identity.  Only then can the exact UUID enter the owner-signed payload.
    self_assertion_pending_edges: list[Any] = field(default_factory=list)
    self_assertion_edge_buffer: list[Any] | None = None
    self_assertion_counterpart_by_edge_id: dict[int, str] = field(default_factory=dict)
    resolved_node_identity_by_extracted_uuid: dict[
        str, tuple[str, str, tuple[str, ...]]
    ] = field(
        default_factory=dict
    )
    resolved_node_was_persistent_by_extracted_uuid: dict[str, bool] = field(
        default_factory=dict
    )
    #: Audit signal that this episode proposed self facts. It is NOT permission to hydrate
    #: other episodes: enforce-mode hydration never consumes raw current/previous episode text.
    suppress_node_semantic_hydration: bool = False
    self_assertion_finalized: bool = False
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
    #: Endpoint-bearing edges rejected because their predicate/fact had no literal support outside
    #: the endpoint entity names in CURRENT MESSAGES. This stops a fabricated marker edge from
    #: turning a valid author capability into authority for an invented relation.
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

#: Verbs that disqualify an item (and therefore the whole block) under the "items are NAMES, not
#: clauses" rule. A closed, conservative allowlist matched at word boundaries; includes the
#: inflections observed on the CF-193 probes (`buy`, `walk`, `call`, `fixed`, `shipped`, `ate`).
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
#: Personal pronouns. A NAME does not begin with one; a clause does ("we are working today",
#: "i bought milk"). Same leading-token rule as the verbs, so this stays one concept.
_LIST_CLAUSE_PRONOUNS: tuple[str, ...] = (
    "i", "we", "you", "he", "she", "they", "it", "my", "our", "your", "their",
)

#: An item that BEGINS with a verb or a pronoun and continues is a clause, not a name.
#: Anchored at the start on purpose -- see the note above `_LIST_ITEM_MAX_WORDS`.
_LIST_VERB_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(w) for w in (*_LIST_VERBS, *_LIST_CLAUSE_PRONOUNS))
    + r")\s+\S",
    re.IGNORECASE,
)


def parse_titled_list(episode_text: str) -> tuple[str, list[str]] | None:
    """Parse ``title:\\n item\\n item...`` into (title, items), or None when it is not clearly a list.

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
    if receipt.raw_edge_count <= 0:
        return False
    usable_edges = receipt.raw_edge_count - receipt.malformed_edges_dropped
    policy_suppressed = (
        receipt.self_echo_edges_suppressed
        + receipt.subject_marker_edges_suppressed
    )
    return usable_edges > 0 and policy_suppressed >= usable_edges


def begin_extraction_receipt(
    episode_key: str,
    episode_text: str,
    *,
    source_description: str = "",
    relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None,
    self_identity: SelfIdentityContext | None = None,
    self_subject_endpoint: SelfSubjectEndpointEnvelope | None = None,
    self_bind_mode: SelfBindMode = SelfBindMode.OFF,
    self_assertion_authorizer: Any | None = None,
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
        self_assertion_authorizer=self_assertion_authorizer,
        self_assertion_proposals=[],
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
    try:
        from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact

        return _normalize_string_exact(str(name))
    except Exception:  # pragma: no cover - fallback mirrors graphiti's implementation
        import re

        return re.sub(r"[\s]+", " ", str(name).lower()).strip()


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
# Synthesizing these would fragment identity (an incidental per-episode ``I``/``me``/
# ``user`` node) and pre-empt the deliberately deferred canonical self-identity feature.
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
#: depend on services. If that constant changes, change this with it.
_SELF_ENTITY_NAME = "user"

#: Labels denoting the HUMAN. Third-person ("user") is how gpt-4o-mini actually writes the speaker;
#: first-person is included for extractors that phrase it that way.
#: DOMAIN: extracted entity NAMES. Includes "my"/"mine" because an extractor can emit them as an
#: endpoint name; the scalar and event subject allowlists deliberately exclude them. Three sets, three
#: questions -- see ``domain/self_identity.SELF_ALIASES`` before changing any of them.
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

    WHY THIS EXISTS: gpt-4o-mini emits the speaker as the literal token ``user`` and never as
    ``I``. ``user`` is in `_NON_SYNTHESIZABLE_ENDPOINTS`, so every edge it anchors was dropped for
    want of an endpoint; graphiti then orphan-pruned every node those edges would have connected,
    and content-bearing episodes persisted nothing (CombinedExtractionCollapsedError). Measured on
    the cc5ded98 smoke: 5 of 6 USER turns collapsed this way -- the refusal was destroying
    precisely the user's own facts, which is the opposite of what it was protecting.

    This helper does **not** establish identity and does **not** assign the canonical UUID. It only
    rewrites equivalent endpoint spellings to the display name ``user`` and lets ordinary Graphiti
    resolution decide where that node goes. That can still create or reuse a fork. Canonical binding
    happens later and requires an exact node declaration; turn role plus this name shape is not one.

    ASSISTANT TURNS ARE EXCLUDED. A ``user -> X`` edge on an assistant turn is the model restating
    what the human already said in their own turn, so binding it mints a DUPLICATE of a fact that
    exists with better provenance on the user turn -- second-hand, in the assistant's paraphrase.
    Observed directly on cc5ded98: turn 8 (user) yields "User hopes to complete a few personal
    projects, such as building a simple web scraper", and turn 9 (assistant) yields "User wants to
    build a web scraper to apply their skills to real-world problems" -- the same fact twice, and
    before this fix ONLY the assistant's copy survived. First-person on an assistant turn is the
    ASSISTANT, so binding it would additionally misattribute the model's own statements ("I'm an
    AI, so I was trained on a massive dataset") to the human.

    This does NOT stop assistant turns being ingested: entity-to-entity facts (the recommendations
    that LongMemEval's `single-session-assistant` category asks about -- 56/500 items, e.g. "the
    Italian restaurant you recommended" -> Roscioli) are untouched. Only the `user -> X` echo is
    dropped. An assistant turn whose edges are ALL `user -> X` will therefore still collapse; that
    turn carried nothing but echo, so the loss is intended rather than a defect.

    Unknown role (no ``user:``/``assistant:`` prefix) is retained by this endpoint-closure rule, so
    content outside the benchmark's prefixed format keeps the collapse fix. It gains no canonical
    subject authority.
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
    grounding text is available — require the name to appear literally in either the
    current episode or the previous episodes Graphiti included in the extraction prompt.
    This admits a resolved antecedent such as ``Rachel`` for "She moved to Chicago"
    without admitting a name absent from the model's supplied conversation context.
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
        # CF-192(a): match on WORD BOUNDARIES, not bare substring containment.
        #
        # `normalized in text.casefold()` admitted any short hallucinated name that happened to sit
        # inside a longer word: against "I joined the channel yesterday" it accepted `Ann`
        # ("ch-ann-el"), `Chan`, `Ester` ("y-ester-day") and `Yes` ("yes-terday"). This guard stands
        # between a model-hallucinated edge endpoint and a materialized KG entity, and because
        # previous-episode texts join the grounding set it got WEAKER the more context the
        # extractor was given.
        #
        # The docstring above already required the name to "appear literally"; substring
        # containment is not that. Note this also FIXES the docstring's own worked example --
        # `Rachel` for "She moved to Chicago" was rejected before, because the antecedent is
        # resolved from a previous episode whose text must contain the token, and a substring test
        # gives no better answer there than a token test does.
        #
        # A multi-word name ("Service Mesh") is matched as a phrase of whole tokens, so internal
        # spacing and punctuation in the source text do not defeat it.
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
#:
#: CF-192(b): `relation_type` is deliberately absent. It is model-supplied boilerplate -- the repair
#: prompt (`_RELATIONLESS_REPAIR_INSTRUCTIONS`) instructs the model to emit relation labels -- so
#: counting its tokens as evidence lets the model ground its own edge. Measured: against
#: "Thanks, that helps me understand more." an edge whose endpoints and fact were copied entirely
#: from prior context was admitted, matching on `more` supplied by its own
#: `WANTS_TO_KNOW_MORE_ABOUT` label. An acknowledgement turn could persist a durable interest edge
#: about an entity the user never mentioned, with the receipt reporting 0 suppressed.
_EDGE_ANCHOR_EVIDENCE_FIELDS = ("source_entity_name", "target_entity_name", "fact")


def _edge_has_current_message_anchor(edge: dict[str, Any], episode_text: str) -> bool:
    """True when the edge shares a meaningful token with the CURRENT turn.

    A deterministic precision guard against the model re-emitting a claim from preceding context.
    Only fields carrying extracted CONTENT count as evidence -- see
    `_EDGE_ANCHOR_EVIDENCE_FIELDS`.
    """
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


def _anchor_token_forms(tokens: set[str]) -> set[str]:
    """Small literal morphology bridge for current-text grounding (``own``/``owns``)."""
    forms: set[str] = set()
    for token in tokens:
        folded = token.casefold()
        forms.add(folded)
        if len(folded) > 3 and folded.endswith("s"):
            forms.add(folded[:-1])
        if len(folded) > 4 and folded.endswith("es"):
            forms.add(folded[:-2])
        if len(folded) > 4 and folded.endswith("ed"):
            forms.add(folded[:-2])
        if len(folded) > 5 and folded.endswith("ing"):
            forms.add(folded[:-3])
    return forms


def _subject_edge_has_current_predicate_anchor(
    *, source_name: str, target_name: str, fact: str, marker: str, episode_text: str
) -> bool:
    """Require marker-edge content to be supported by one affirmative author clause.

    A shared token is insufficient: questions, negation, and quoted speech can all contain the same
    predicate as a fabricated positive edge.  Every meaningful non-endpoint fact token must be
    present in one accepted author clause, and the other endpoint must overlap that same clause.
    Relation labels remain excluded because they are model output rather than source evidence.
    """
    marker_folded = str(marker or "").casefold()
    source_is_marker = str(source_name or "").casefold() == marker_folded
    target_is_marker = str(target_name or "").casefold() == marker_folded
    if source_is_marker == target_is_marker:
        return False
    other_endpoint = target_name if source_is_marker else source_name
    endpoint_tokens = {
        raw.casefold()
        for value in (source_name, target_name, marker)
        for raw in _CURRENT_MESSAGE_TOKEN_RE.findall(str(value or ""))
    }
    fact_tokens = {
        token
        for token in _current_message_anchor_tokens(fact)
        if token not in endpoint_tokens
        and token not in {"author", "current", "message", "speaker", "user", "human"}
    }
    other_endpoint_tokens = _anchor_token_forms({
        token for token in _current_message_anchor_tokens(str(other_endpoint or ""))
        if token not in {"author", "current", "message", "speaker", "user", "human"}
    })
    if not fact_tokens or not other_endpoint_tokens:
        return False
    return any(
        all(_anchor_token_forms({token}) & clause_tokens for token in fact_tokens)
        and bool(other_endpoint_tokens & clause_tokens)
        for clause in _author_assertion_clauses(episode_text)
        if (clause_tokens := _anchor_token_forms(_current_message_anchor_tokens(clause)))
    )


def _sanitize_combined_payload(
    data: Any,
    receipt: CombinedExtractionReceipt | None,
    episode_text: str,
) -> Any:
    """Sanitize a raw combined-extraction payload and close missing edge endpoints.

    Order (per remediation contract): record raw counts -> drop malformed edge rows
    -> normalize extracted entities -> add missing usable edge endpoints -> hand back
    to Graphiti for its normal resolution. Runs BEFORE ``CombinedExtraction`` is
    validated so a single malformed row cannot invalidate the whole batch, and BEFORE
    Graphiti's edge/orphan pruning so a legitimate edge is not dropped for lack of a
    listed endpoint.
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
            and _is_reserved_subject_marker(norm["name"])
            and norm["name"] != marker
        ):
            # A stale, malformed, or model-invented reserved endpoint is never an ordinary entity.
            # Only the exact capability token on this task's receipt may survive sanitation.
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
            if marker_occurs_in_text and (
                not endpoint_uses_marker or not active_marker_occurs
            ):
                # A marker in prose without the exact marker endpoint has no authority path that
                # can scrub it before persistence. Drop the edge rather than leak a capability.
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
                # This is the assistant restating a fact the human already gave first-hand. The
                # decision is made on ROLE + LABEL alone and is tested BEFORE `known` membership,
                # because enforcement used to rely on leaving the endpoint unbound so graphiti
                # would drop the edge -- and Menhir's own `_RELATION_COMPLETENESS_INSTRUCTIONS`
                # tells the model to include `user` in extracted_entities, which puts the endpoint
                # in `known` and silently disabled the whole policy. Break: a doomed edge must not
                # go on to mint a synthesized endpoint entity for its other side.
                edge_is_self_echo = True
                break
            if norm_key in known:
                continue
            marker = _active_subject_marker(receipt)
            if marker and endpoint_name == marker:
                # The marker is grounded by the receipt, not by user text.  Materialize it only
                # when the extractor used it as an endpoint; a standalone marker node is not proof
                # that the episode asserted anything about its author.
                entities.append({"name": marker, "entity_type_id": -1})
                known.add(norm_key)
                synthesized += 1
                continue
            if _is_unresolved_self_like_endpoint(norm_key, episode_text):
                # Normalize the endpoint spelling and materialize it ONCE per payload so Graphiti
                # does not drop the edge. This is availability recovery, not identity resolution:
                # the node remains an ordinary candidate unless a separate structured producer
                # declares its exact UUID after extraction.
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
            # Otherwise leave it missing. NOTE: graphiti drops this one edge during resolution --
            # true locally, but if it was the LAST edge every node it would have connected is then
            # orphan-pruned and the whole episode collapses. The self-like case above is retained
            # only to avoid that cascade; it is deliberately not promoted to canonical self.
        if edge_is_self_echo:
            self_echo_edges += 1
            continue
        surviving_edges.append(edge)

    # Echo edges are dropped EXPLICITLY rather than by leaving an endpoint unbound. The old
    # implicit enforcement only worked when the model omitted `user` from extracted_entities;
    # when it lists `user` -- which the extraction prompt asks it to do -- the endpoint resolves
    # and the echo edge survived, duplicating the user's own first-hand fact under the assistant's
    # paraphrase while the receipt reported zero suppressed.
    # The titled-list fallback below keeps its ORIGINAL trigger: it asks whether the extractor
    # produced any edge at all, which is the question it was written to ask. Testing the post-drop
    # list instead would newly fire list synthesis on echo-only assistant turns -- a separate
    # decision that this fix deliberately does not make.
    extractor_produced_edges = bool(edges)
    edges = surviving_edges

    # Titled list: the turn states membership through SYNTAX rather than a verb, so the extractor
    # returns names with no relation between them. Every node is then orphan-pruned for want of an
    # edge and the whole episode collapses -- the content is correct and is lost anyway. Emit the
    # membership the list states, which keeps the names connected and makes the collapse moot.
    # Only when the extractor found NO usable edges: if it did state relations, they are the truth
    # of the turn and a synthetic membership edge must not compete with them.
    list_edges_added = 0
    if not extractor_produced_edges:
        parsed = parse_titled_list(episode_text)
        if parsed is not None:
            container, items = parsed
            container_key = _normalize_endpoint_name(container)
            extracted_keys = {_normalize_endpoint_name(e["name"]) for e in entities}
            # Require the extractor to have independently seen the items. The parse decides they are
            # a LIST; the extractor decides they are ENTITIES. Needing both means a mis-parse of
            # prose cannot mint nodes on its own.
            matched = [it for it in items if _normalize_endpoint_name(it) in extracted_keys]
            if len(matched) >= 3:
                if container_key not in extracted_keys:
                    entities.append({"name": container, "entity_type_id": -1})
                    extracted_keys.add(container_key)
                for item in matched:
                    # Built through the SAME sanitizer every model-produced edge goes through, so a
                    # synthetic edge can never carry a shape the real path would have rejected or
                    # normalized differently (e.g. `episode_indices`, which graphiti uses to map the
                    # edge to its source episode and to pick its reference time).
                    synthetic = _sanitize_combined_edge({
                        "relation_type": _MEMBERSHIP_RELATION,
                        "source_entity_name": item,
                        "target_entity_name": container,
                        # Menhir built this fact, not the model. It carries the synthetic marker so
                        # the storage boundary classifies it honestly instead of stamping
                        # fact_source="original" on a sentence the model never asserted.
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
        # The validator runs once per extraction call and cannot see which pass it is in, so the
        # repair flag -- set by `_run_graphiti_combined_extraction` BEFORE the second call -- is the
        # discriminator. Writing both passes into one field would let the repair's shape overwrite
        # the first pass's evidence, which is exactly what `is_policy_empty_extraction` must not
        # lose sight of.
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
# Graphiti single-episode combined extraction patch
# ---------------------------------------------------------------------------


_RELATION_COMPLETENESS_INSTRUCTIONS = """\
MENHIR RELATION COMPLETENESS:
- Do not return an entity without a relationship when CURRENT MESSAGES state what the speaker
  does, owns, uses, prefers, plans, experiences, believes, or explicitly wants to learn about
  that entity.
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
- Do not invent a relationship merely to connect an entity. If the current text truly states no
  relationship, omit the entity as well.
"""


def _relation_completeness_instructions(
    endpoint: SelfSubjectEndpointEnvelope | None,
) -> str:
    """Render one non-contradictory author endpoint into the first extraction prompt."""
    if endpoint is None:
        return _RELATION_COMPLETENESS_INSTRUCTIONS
    return f"""\
MENHIR RELATION COMPLETENESS:
- Do not return an entity without a relationship when CURRENT MESSAGES state what the speaker
  does, owns, uses, prefers, plans, experiences, believes, or explicitly wants to learn about
  that entity.
- In a human-authored first-person statement, represent I/me/my with the exact opaque entity
  `{endpoint.marker}` and emit the direct speaker-to-target relationship. Include
  `{endpoint.marker}` in extracted_entities.
- Explicit first-person informational intent is relationship-bearing. Emit
  `{endpoint.marker}` -> `WANTS_TO_KNOW_MORE_ABOUT` or `INTERESTED_IN` -> the target.
- Apply that rule only when CURRENT MESSAGES explicitly state the speaker's informational intent.
  A bare request or question such as "Can you tell me about X?" does not by itself assert durable
  interest in X.
- Do not invent a relationship merely to connect an entity. If the current text truly states no
  relationship, omit the entity as well.
"""

_RELATIONLESS_REPAIR_INSTRUCTIONS = """\
CORRECTIVE RE-EXTRACTION:
Your previous extraction returned one or more entities but no usable relationship, so every entity
would be orphan-pruned and the memory would be lost. Re-read CURRENT MESSAGES and return a complete
entity-and-edge extraction. Pay special attention to first-person predicates such as "I use...",
"I own...", "I prefer...", "I plan...", "I'd like to know more about X", and "I'm interested in
understanding X"; bind a human first-person speaker to `user`. Explicit informational intent must
emit `WANTS_TO_KNOW_MORE_ABOUT` or `INTERESTED_IN`. A bare request or question such as "Can you tell
me about X?" does not by itself assert durable interest. Do not invent facts. If the text truly
contains no relationship, return both lists empty.
"""


def _relationless_repair_instructions(
    endpoint: SelfSubjectEndpointEnvelope | None,
) -> str:
    if endpoint is None:
        return _RELATIONLESS_REPAIR_INSTRUCTIONS
    return f"""\
CORRECTIVE RE-EXTRACTION:
Your previous extraction returned one or more entities but no usable relationship, so every entity
would be orphan-pruned and the memory would be lost. Re-read CURRENT MESSAGES and return a complete
entity-and-edge extraction. For first-person predicates such as "I use...", "I own...", "I
prefer...", or "I plan...", bind the current human speaker to the exact opaque entity
`{endpoint.marker}`. Explicit informational intent must emit `WANTS_TO_KNOW_MORE_ABOUT` or
`INTERESTED_IN`. A bare request or question does not by itself assert durable interest. Do not
invent facts. If the text truly contains no relationship, return both lists empty.
"""


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


_AUTHOR_ASSERTION_RE = re.compile(
    r"(?:^\s*(?:[-*+]\s+)?|[.!;]\s+)"
    r"(?:(?:yes|today|currently|actually|also|personally|now)\s*[,;]\s*)?"
    r"(?P<subject>i(?:['’](?:m|ve|d|ll))?\b|my\b)"
    r"(?P<body>[^.!?\r\n]*)(?P<terminal>[.!?]|$)",
    re.IGNORECASE | re.MULTILINE,
)
_AUTHOR_ASSERTION_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|neither|cannot|cant|don't|dont|doesn't|doesnt|didn't|didnt|"
    r"won't|wont|wouldn't|wouldnt|isn't|isnt|aren't|arent|wasn't|wasnt|weren't|"
    r"werent|haven't|havent|hasn't|hasnt|hadn't|hadnt|without)\b",
    re.IGNORECASE,
)


def _strip_same_delimiter_spans(
    text: str, delimiter: str, *, ignore_word_internal: bool = False
) -> str:
    """Blank paired or unterminated quote/code spans while preserving line boundaries."""
    chars = list(text)
    inside = False
    for index, char in enumerate(text):
        if char == delimiter:
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if ignore_word_internal and previous.isalnum() and following.isalnum():
                continue
            inside = not inside
            chars[index] = " "
            continue
        if inside and char not in "\r\n":
            chars[index] = " "
    return "".join(chars)


def _strip_distinct_delimiter_spans(text: str, opener: str, closer: str) -> str:
    """Blank curly-quote spans; an unmatched opener conservatively blanks the remainder."""
    chars = list(text)
    inside = False
    for index, char in enumerate(text):
        if not inside and char == opener:
            inside = True
            chars[index] = " "
            continue
        if inside and char == closer:
            inside = False
            chars[index] = " "
            continue
        if inside and char not in "\r\n":
            chars[index] = " "
    return "".join(chars)


def _strip_author_quote_spans(text: str) -> str:
    stripped = _strip_same_delimiter_spans(text, '"')
    stripped = _strip_distinct_delimiter_spans(stripped, "“", "”")
    stripped = _strip_distinct_delimiter_spans(stripped, "‘", "’")
    stripped = _strip_same_delimiter_spans(
        stripped, "'", ignore_word_internal=True
    )
    return _strip_same_delimiter_spans(stripped, "`")


def _current_author_surface(episode_text: str) -> str:
    """Return current-message prose with common quote/code and blockquote spans removed."""
    current = str(episode_text or "")
    role, separator, body = current.partition(":")
    if separator and role.strip().casefold() in {"user", "assistant", "tool", "agent"}:
        current = body
    visible_lines: list[str] = []
    fence_char = ""
    fence_width = 0
    for line in current.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence_char:
            if (
                fence_match is not None
                and fence_match.group(1)[0] == fence_char
                and len(fence_match.group(1)) >= fence_width
            ):
                fence_char = ""
                fence_width = 0
            continue
        if fence_match is not None:
            fence_char = fence_match.group(1)[0]
            fence_width = len(fence_match.group(1))
            continue
        if stripped.startswith(">"):
            continue
        visible_lines.append(line)
    current = "\n".join(visible_lines)
    return _strip_author_quote_spans(current)


_CURRENT_AUTHOR_REFERENCE_RE = re.compile(
    r"\b(?:i|me|my|mine|myself)(?:['’](?:m|ve|d|ll))?\b",
    re.IGNORECASE,
)


def _current_message_mentions_author(episode_text: str) -> bool:
    """Detect an author reference without deciding grammar, polarity, or truth.

    This is a refusal-only fallback for an extractor that ignores the opaque endpoint.  It never
    grants identity or assertion authority; questions and negated/adverb-prefixed statements are
    intentionally included so they cannot fall back to an ordinary ``user`` identity.
    """

    return bool(_CURRENT_AUTHOR_REFERENCE_RE.search(_current_author_surface(episode_text)))


def _author_assertion_clauses(episode_text: str) -> tuple[str, ...]:
    """Conservative affirmative clauses whose grammatical subject is the current author."""
    clauses: list[str] = []
    for match in _AUTHOR_ASSERTION_RE.finditer(_current_author_surface(episode_text)):
        body = str(match.group("body") or "")
        if match.group("terminal") == "?" or _AUTHOR_ASSERTION_NEGATION_RE.search(body):
            continue
        clauses.append(match.group(0).lstrip(".!; \t-*+"))
    return tuple(clauses)


def _requires_declared_author_endpoint(episode_text: str) -> bool:
    """Conservative evidence that CURRENT MESSAGES assert a relation about their author.

    Affirmative first-person subjects outside common quote/code spans qualify at a sentence/list
    boundary or after a small set of discourse prefixes. Questions and clauses containing explicit
    negation do not authorize correction or binding.
    """
    return bool(_author_assertion_clauses(episode_text))


def _subject_endpoint_instructions(
    endpoint: SelfSubjectEndpointEnvelope | None,
) -> str | None:
    if endpoint is None:
        return None
    return f"""\
MENHIR VERIFIED CURRENT-MESSAGE AUTHOR ENDPOINT:
- The exact opaque entity name `{endpoint.marker}` denotes the author of CURRENT MESSAGES only.
- Use `{endpoint.marker}` as the endpoint for every relation asserted by I/me/my or the current
  message's author. Do not substitute a generic speaker label.
- Do not use the marker for a person speaking inside quoted or reported speech.
- Do not replace third-person users, customers, roles, tables, collections, or application actors
  with the marker.
- Preserve source-qualified ordinary names such as `application user` when available rather
  than collapsing them to the ambiguous bare label `user`.
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


def _episode_cache_key(episode: Any) -> str:
    episodes = episode if isinstance(episode, list) else [episode]
    return "|".join(str(getattr(item, "uuid", id(item))) for item in episodes)


def _combine_extraction_instructions(*parts: str | None) -> str:
    """Append Menhir instructions without discarding a caller's custom extraction contract."""
    return "\n\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


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


#: The section delimiters graphiti's prompt templates wrap `previous_episodes` in. Stored turn
#: text is rendered inside them via `to_prompt_json`, which is `json.dumps` -- it escapes quotes
#: and newlines but NOT angle brackets, so a turn containing the closing tag reproduces it
#: verbatim in the rendered prompt and can appear to end the quoted section (CF-194).
#:
#: This is coupled to the vendored template by construction: if graphiti renames these tags the
#: neutralisation goes stale silently. The pairing is asserted in the extraction-patch tests.
_PROMPT_SECTION_TAGS = ("<PREVIOUS MESSAGES>", "</PREVIOUS MESSAGES>",
                        "<CURRENT MESSAGE>", "</CURRENT MESSAGE>")


def _neutralize_prompt_delimiters(text: str) -> str:
    """Defang the prompt's own structural tags inside attacker-influenced context text.

    Deliberately narrow: only the exact tags are rewritten, and only by breaking the angle
    brackets, so ordinary prose and code in a captured turn survive unchanged. Escaping every
    `<`/`>` would mangle legitimate content for no additional guarantee.
    """
    out = text
    for tag in _PROMPT_SECTION_TAGS:
        if tag.lower() in out.lower():
            # Case-insensitive replace without regex, preserving surrounding text.
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


def _canonical_self_edge_text(value: Any, marker: str) -> str:
    """Mirror the binder's marker-to-display rewrite in the owner-signable proposal."""

    return re.sub(re.escape(marker), "user", str(value or ""), flags=re.IGNORECASE)


def _self_assertion_proposal_for_edge(
    *,
    marker_uuid: str,
    marker: str,
    edge: Any,
    node_names: dict[str, str],
    node_labels: dict[str, list[str]],
    resolved_counterpart_uuid: str | None = None,
    receipt: CombinedExtractionReceipt,
) -> SelfAssertionProposal:
    """Freeze one final-payload marker edge into the exact owner-signable contract."""

    source_uuid = str(getattr(edge, "source_node_uuid", "") or "").strip()
    target_uuid = str(getattr(edge, "target_node_uuid", "") or "").strip()
    if source_uuid == marker_uuid and target_uuid != marker_uuid:
        direction = "self_to_entity"
        counterpart_uuid = target_uuid
    elif target_uuid == marker_uuid and source_uuid != marker_uuid:
        direction = "entity_to_self"
        counterpart_uuid = source_uuid
    else:
        raise InvalidSelfSubjectDeclarationError(
            "self-subject marker edge must have exactly one marker endpoint"
        )
    identity = receipt.self_identity
    if identity is None:
        raise InvalidSelfSubjectDeclarationError(
            "self-subject proposal lacks identity context"
        )
    attributes = getattr(edge, "attributes", None)
    polarity = (
        str(attributes.get("polarity") or "affirmed")
        if isinstance(attributes, dict)
        else "affirmed"
    )
    return make_self_assertion_proposal(
        principal_id=identity.principal_id,
        namespace=identity.namespace,
        episode_uuid=receipt.episode_key,
        turn_evidence_uuid=identity.turn_evidence_uuid,
        evidence_text=receipt.episode_text,
        lane="graphiti_edge",
        direction=direction,
        polarity=polarity,
        assertion={
            "counterpart": {
                "labels": node_labels.get(counterpart_uuid, []),
                "name": node_names.get(counterpart_uuid, ""),
                "uuid": str(resolved_counterpart_uuid or counterpart_uuid),
            },
            "fact": _canonical_self_edge_text(getattr(edge, "fact", ""), marker),
            "predicate": _canonical_self_edge_text(getattr(edge, "name", ""), marker),
            "subject": {"kind": "canonical_self"},
        },
        temporal_scope={
            "expired_at": canonical_temporal_value(getattr(edge, "expired_at", None)),
            "invalid_at": canonical_temporal_value(getattr(edge, "invalid_at", None)),
            "valid_at": canonical_temporal_value(getattr(edge, "valid_at", None)),
        },
    )


def _unmarked_author_fallbacks(
    nodes: list[Any],
    edges: list[Any],
    receipt: CombinedExtractionReceipt,
) -> tuple[list[Any], set[str]]:
    """Find ambiguous unmarked author references across the entire payload.

    This is refusal-only, never identity authority. A bare self alias in an author-bearing
    turn is ambiguous even when a different edge uses the marker correctly. Marker-bearing
    edges are excluded here: their exact counterpart (which may legitimately be named
    ``user``) must pass the separate persistent-UUID/owner-confirmation gate.
    """

    if (
        receipt.self_bind_mode is not SelfBindMode.ENFORCE
        or receipt.self_subject_endpoint is None
        or not _current_message_mentions_author(receipt.episode_text)
    ):
        return [], set()
    unsafe_node_uuids = {
        str(getattr(node, "uuid", "") or "").strip()
        for node in nodes
        if is_self_alias(getattr(node, "name", None))
    } - {""}
    if not unsafe_node_uuids:
        return [], set()
    marker_uuids = {
        str(getattr(node, "uuid", "") or "").strip()
        for node in nodes
        if getattr(node, "name", None) == receipt.self_subject_endpoint.marker
    } - {""}
    rejected_edges: list[Any] = []
    rejected_endpoints: set[str] = set()
    surviving_endpoints: set[str] = set()
    for edge in edges:
        endpoints = {
            str(getattr(edge, "source_node_uuid", "") or "").strip(),
            str(getattr(edge, "target_node_uuid", "") or "").strip(),
        } - {""}
        if endpoints & unsafe_node_uuids and not endpoints & marker_uuids:
            rejected_edges.append(edge)
            rejected_endpoints.update(endpoints)
        else:
            surviving_endpoints.update(endpoints)
    # Include isolated aliases, not just nodes reached by rejected edges. Otherwise an
    # orphan ``user`` could still enter candidate acquisition alongside a valid marker.
    pruned_uuids = (unsafe_node_uuids | rejected_endpoints) - surviving_endpoints
    return rejected_edges, pruned_uuids


def _quarantine_unmarked_author_fallbacks(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    receipt: CombinedExtractionReceipt,
) -> None:
    """Remove ambiguous author fallbacks without binding or deleting stored identities.

    Third-person-only application/RBAC users retain ordinary resolution. In a mixed turn,
    an unmarked bare alias cannot be proven ordinary merely from model-authored edge text;
    retain its raw evidence and refusal receipt rather than inventing a durable self fork.
    Source-qualified ordinary names and owner-gated marker counterparts remain distinct.
    """

    rejected_edges, pruned_uuids = _unmarked_author_fallbacks(nodes, edges, receipt)
    if not rejected_edges and not pruned_uuids:
        return
    rejected_ids = {id(edge) for edge in rejected_edges}
    edges[:] = [edge for edge in edges if id(edge) not in rejected_ids]
    nodes[:] = [
        node
        for node in nodes
        if str(getattr(node, "uuid", "") or "").strip() not in pruned_uuids
    ]
    for uuid in pruned_uuids:
        index_map.pop(uuid, None)
    receipt.subject_marker_edges_suppressed += len(rejected_edges)
    receipt.suppress_node_semantic_hydration = True
    if receipt.self_assertion_proposals is None:
        receipt.self_assertion_proposals = []
    receipt.self_assertion_proposals.append(
        {
            "authorization": {
                "authorized": False,
                "authority_key_id": "",
                "reason": "unmarked_author_reference_quarantined",
            },
            "episode_uuid": receipt.episode_key,
            "evidence_sha256": sha256(receipt.episode_text.encode("utf-8")).hexdigest(),
            "kind": "unresolved_author_reference",
            "policy_version": SELF_ASSERTION_POLICY_VERSION,
        }
    )


def _declare_subject_endpoint(
    nodes: list[Any],
    edges: list[Any],
    index_map: dict[str, list[int]],
    receipt: CombinedExtractionReceipt,
) -> None:
    """Declare structural self and hold its semantic edges pending ordinary node resolution."""

    endpoint = receipt.self_subject_endpoint
    if endpoint is None:
        return
    identity = receipt.self_identity
    if receipt.self_bind_mode is not SelfBindMode.ENFORCE:
        raise InvalidSelfSubjectDeclarationError(
            "self-subject endpoint reached final extraction outside enforce mode"
        )
    if identity is None or identity.evidence_kind is not SelfEvidenceKind.TRUSTED_USER_TURN:
        raise InvalidSelfSubjectDeclarationError(
            "self-subject endpoint lacks trusted user-turn identity evidence"
        )
    if (
        endpoint.episode_uuid != str(receipt.episode_key or "").strip()
        or endpoint.episode_uuid != str(identity.episode_uuid or "").strip()
        or endpoint.namespace != identity.namespace
        or endpoint.turn_evidence_uuid
        != str(identity.turn_evidence_uuid or "").strip()
    ):
        raise InvalidSelfSubjectDeclarationError(
            "self-subject endpoint scope does not match the active extraction receipt"
        )

    reserved_nodes = [
        node for node in nodes if _is_reserved_subject_marker(getattr(node, "name", None))
    ]
    marker_nodes = [
        node for node in reserved_nodes if getattr(node, "name", None) == endpoint.marker
    ]
    if len(reserved_nodes) != len(marker_nodes):
        raise InvalidSelfSubjectDeclarationError(
            "final payload contains a stale or malformed self-subject marker"
        )
    if len(marker_nodes) > 1:
        raise InvalidSelfSubjectDeclarationError(
            "final payload contains more than one self-subject marker node"
        )
    # Validate every unmarked reference, including mixed payloads. A valid marker on one
    # relationship is not a blanket exemption for ordinary-looking relationships elsewhere.
    _quarantine_unmarked_author_fallbacks(nodes, edges, index_map, receipt)
    if not marker_nodes:
        return

    marker_node = marker_nodes[0]
    marker_uuid = str(getattr(marker_node, "uuid", "") or "").strip()
    if not marker_uuid:
        raise InvalidSelfSubjectDeclarationError(
            "self-subject marker node has no in-memory UUID"
        )
    marker_edges = [
        edge
        for edge in edges
        if marker_uuid
        in {
            str(getattr(edge, "source_node_uuid", "") or "").strip(),
            str(getattr(edge, "target_node_uuid", "") or "").strip(),
        }
    ]
    if not marker_edges:
        raise InvalidSelfSubjectDeclarationError(
            "self-subject marker node is not an endpoint of a current-episode edge"
        )
    graphiti_episode_uuid = str(receipt.graphiti_episode_uuid or "").strip()
    if not graphiti_episode_uuid or not all(
        graphiti_episode_uuid
        in {str(value) for value in (getattr(edge, "episodes", None) or [])}
        for edge in marker_edges
    ):
        raise InvalidSelfSubjectDeclarationError(
            "self-subject marker has no edge attributed to the current Graphiti episode"
        )
    if 0 not in index_map.get(marker_uuid, []):
        raise InvalidSelfSubjectDeclarationError(
            "self-subject marker node lacks current-episode index attribution"
        )
    for edge in marker_edges:
        attributes = getattr(edge, "attributes", None)
        if not isinstance(attributes, dict):
            attributes = {}
            setattr(edge, "attributes", attributes)
        # This property is server-owned.  A model-produced value can never survive to the
        # persistence payload, even when authorization later fails closed.
        attributes.pop(SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY, None)
        attributes.pop(SELF_ASSERTION_EDGE_EPISODE_PROPERTY, None)
        attributes.pop(SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY, None)
        source_uuid = str(getattr(edge, "source_node_uuid", "") or "").strip()
        target_uuid = str(getattr(edge, "target_node_uuid", "") or "").strip()
        receipt.self_assertion_counterpart_by_edge_id[id(edge)] = (
            target_uuid if source_uuid == marker_uuid else source_uuid
        )
    receipt.self_assertion_pending_edges.extend(marker_edges)
    receipt.self_assertion_edge_buffer = edges
    receipt.suppress_node_semantic_hydration = True

    receipt.self_identity = declare_self_subject(
        identity,
        subject_node_uuid=marker_uuid,
    )


def _record_unresolved_self_assertion(
    receipt: CombinedExtractionReceipt,
    *,
    reason: str,
    extracted_counterpart_uuid: str,
) -> None:
    """Retain a bounded refusal when a signable counterpart cannot be constructed."""

    if receipt.self_assertion_proposals is None:
        receipt.self_assertion_proposals = []
    record: dict[str, Any] = {
        "authorization": {
            "authorized": False,
            "authority_key_id": "",
            "reason": reason,
        },
        "episode_uuid": receipt.episode_key,
        "evidence_sha256": sha256(receipt.episode_text.encode("utf-8")).hexdigest(),
        "kind": "unresolved_self_assertion",
        "policy_version": SELF_ASSERTION_POLICY_VERSION,
    }
    if extracted_counterpart_uuid:
        record["extracted_counterpart_uuid"] = extracted_counterpart_uuid[:512]
    receipt.self_assertion_proposals.append(record)


def finalize_self_assertion_authority_after_node_resolution(
    receipt: CombinedExtractionReceipt,
) -> set[str]:
    """Authorize pending marker edges against their resolved persistent counterpart UUID.

    Returns extracted node UUIDs supported only by rejected self edges.  The node-resolution patch
    removes those rows before Graphiti can hydrate or persist them.
    """

    if receipt.self_assertion_finalized:
        return set()
    receipt.self_assertion_finalized = True
    pending_edges = list(receipt.self_assertion_pending_edges)
    if not pending_edges:
        return set()
    edge_buffer = receipt.self_assertion_edge_buffer
    if edge_buffer is None:
        raise InvalidSelfSubjectDeclarationError(
            "canonical-self proposal lost its extraction edge buffer"
        )
    identity = receipt.self_identity
    endpoint = receipt.self_subject_endpoint
    if identity is None or endpoint is None:
        raise InvalidSelfSubjectDeclarationError(
            "canonical-self proposal lost its structural identity context"
        )
    marker_uuid = self_uuid_for_namespace(identity.namespace)
    proposal_records = receipt.self_assertion_proposals
    if proposal_records is None:
        proposal_records = []
        receipt.self_assertion_proposals = proposal_records
    authorized_ids: set[int] = set()
    rejected_counterparts: set[str] = set()

    for edge in pending_edges:
        edge_id = id(edge)
        original_counterpart_uuid = receipt.self_assertion_counterpart_by_edge_id.get(
            edge_id, ""
        )
        resolved_counterpart = receipt.resolved_node_identity_by_extracted_uuid.get(
            original_counterpart_uuid
        )
        if resolved_counterpart is None:
            _record_unresolved_self_assertion(
                receipt,
                reason="counterpart_identity_not_resolved",
                extracted_counterpart_uuid=original_counterpart_uuid,
            )
            rejected_counterparts.add(original_counterpart_uuid)
            continue
        resolved_uuid, resolved_name, resolved_labels = resolved_counterpart
        try:
            proposal = _self_assertion_proposal_for_edge(
                marker_uuid=marker_uuid,
                marker=endpoint.marker,
                edge=edge,
                node_names={original_counterpart_uuid: resolved_name},
                node_labels={original_counterpart_uuid: list(resolved_labels)},
                resolved_counterpart_uuid=resolved_uuid,
                receipt=receipt,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Canonical-self proposal was not authorizable episode_id=%s reason=%s",
                receipt.episode_key,
                exc,
            )
            _record_unresolved_self_assertion(
                receipt,
                reason="counterpart_proposal_invalid",
                extracted_counterpart_uuid=original_counterpart_uuid,
            )
            rejected_counterparts.add(original_counterpart_uuid)
            continue

        if not receipt.resolved_node_was_persistent_by_extracted_uuid.get(
            original_counterpart_uuid, False
        ):
            decision = SelfAuthorizationDecision(
                False, "counterpart_identity_not_persistent"
            )
        else:
            authorizer = receipt.self_assertion_authorizer
            if authorizer is None:
                decision = SelfAuthorizationDecision(
                    False, "owner_confirmation_not_configured"
                )
            else:
                try:
                    decision = authorizer.authorize(proposal)
                except Exception:  # noqa: BLE001 - authority errors always fail closed
                    logger.exception(
                        "Canonical-self owner confirmation check failed episode_id=%s",
                        receipt.episode_key,
                    )
                    decision = SelfAuthorizationDecision(
                        False, "owner_confirmation_check_failed"
                    )
        proposal_records.append(proposal.audit_record(decision))
        if not decision.authorized:
            rejected_counterparts.add(original_counterpart_uuid)
            continue

        authorized_ids.add(edge_id)
        receipt.self_assertion_authorized_edge_ids.add(edge_id)
        edge.attributes = {
            SELF_ASSERTION_EDGE_EPISODE_PROPERTY: proposal.episode_uuid,
            SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY: receipt.graphiti_episode_uuid,
            SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY: canonical_json_bytes(
                proposal.confirmation_payload()
            ).decode("utf-8"),
        }

    pending_ids = {id(edge) for edge in pending_edges}
    edge_buffer[:] = [
        edge
        for edge in edge_buffer
        if id(edge) not in pending_ids or id(edge) in authorized_ids
    ]
    rejected_count = len(pending_edges) - len(authorized_ids)
    receipt.subject_marker_edges_suppressed += rejected_count
    receipt.self_assertions_authorized += len(authorized_ids)
    receipt.self_assertion_pending_edges.clear()

    surviving_endpoints = {
        endpoint_uuid
        for edge in edge_buffer
        for endpoint_uuid in (
            str(getattr(edge, "source_node_uuid", "") or "").strip(),
            str(getattr(edge, "target_node_uuid", "") or "").strip(),
        )
        if endpoint_uuid
    }
    return {
        uuid
        for uuid in rejected_counterparts
        if uuid and uuid not in surviving_endpoints
    }


def _wrap_self_authority_edge_resolver(original: Any) -> Any:
    """Keep a verified edge exact through Graphiti's post-extraction resolver.

    Graphiti normally clears attributes on untyped edges and may infer timestamps after combined
    extraction. Either would make the persisted relationship differ from the signed payload. The
    wrapper recognizes only an edge object explicitly authorized in the active Menhir receipt,
    reuses an existing edge only on an exact fact/predicate/endpoint/temporal match, and otherwise
    resolves it as a new edge with dedup/invalidation disabled. It then restores the signed
    temporal fields and server-owned payload. Ordinary edges retain Graphiti's behavior.
    """

    async def _resolve_preserving_owner_authority(
        llm_client: Any,
        extracted_edge: Any,
        related_edges: list[Any],
        existing_edges: list[Any],
        episode: Any,
        edge_type_candidates: Any = None,
    ) -> Any:
        receipt = _extraction_receipt.get()
        attributes = getattr(extracted_edge, "attributes", None)
        payload_json = (
            attributes.get(SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY)
            if isinstance(attributes, dict)
            else None
        )
        protected_self_uuid = ""
        if (
            receipt is not None
            and receipt.self_bind_mode is SelfBindMode.ENFORCE
            and receipt.self_identity is not None
        ):
            protected_self_uuid = self_uuid_for_namespace(receipt.self_identity.namespace)
        is_protected_self_edge = bool(
            protected_self_uuid
            and protected_self_uuid
            in {
                str(getattr(extracted_edge, "source_node_uuid", "") or "").strip(),
                str(getattr(extracted_edge, "target_node_uuid", "") or "").strip(),
            }
        )

        async def _resolve_as_ordinary_edge() -> Any:
            # The authority property is server-owned. Strip any lookalike that lacks the active
            # receipt capability before handing the edge back to Graphiti, whose no-candidate fast
            # path otherwise preserves arbitrary untyped attributes.
            current_attributes = dict(getattr(extracted_edge, "attributes", None) or {})
            current_attributes.pop(SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY, None)
            current_attributes.pop(SELF_ASSERTION_EDGE_EPISODE_PROPERTY, None)
            current_attributes.pop(SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY, None)
            extracted_edge.attributes = current_attributes
            return await original(
                llm_client,
                extracted_edge,
                related_edges,
                existing_edges,
                episode,
                edge_type_candidates,
            )

        if not is_protected_self_edge:
            return await _resolve_as_ordinary_edge()
        if (
            receipt is None
            or id(extracted_edge) not in receipt.self_assertion_authorized_edge_ids
            or not isinstance(payload_json, str)
        ):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self edge reached resolution without its owner-authorized capability"
            )
        try:
            proposal = proposal_from_confirmation_payload(json.loads(payload_json))
        except (TypeError, ValueError):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self edge reached resolution with a malformed authority payload"
            ) from None
        expected_self_uuid = protected_self_uuid
        assertion = json.loads(proposal.assertion_json)
        counterpart = assertion.get("counterpart")
        original_counterpart_uuid = receipt.self_assertion_counterpart_by_edge_id.get(
            id(extracted_edge), ""
        )
        resolved_counterpart = receipt.resolved_node_identity_by_extracted_uuid.get(
            original_counterpart_uuid
        )
        actual_counterpart_uuid = (
            str(getattr(extracted_edge, "target_node_uuid", "") or "").strip()
            if proposal.direction == "self_to_entity"
            else str(getattr(extracted_edge, "source_node_uuid", "") or "").strip()
        )
        if (
            not isinstance(counterpart, dict)
            or not str(counterpart.get("name") or "").strip()
            or resolved_counterpart is None
            or resolved_counterpart[0] != actual_counterpart_uuid
        ):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self edge lacks an exact resolved counterpart identity"
            )

        from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact

        if _normalize_string_exact(str(counterpart["name"])) != _normalize_string_exact(
            resolved_counterpart[1]
        ):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self counterpart changed during ordinary entity resolution"
            )
        signed_counterpart_labels = counterpart.get("labels")
        if (
            not isinstance(signed_counterpart_labels, list)
            or any(not isinstance(label, str) for label in signed_counterpart_labels)
            or tuple(sorted(signed_counterpart_labels)) != resolved_counterpart[2]
        ):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self counterpart labels changed during ordinary entity resolution"
            )
        if not proposal_matches_persisted_edge(
            proposal,
            expected_self_uuid=expected_self_uuid,
            source_node_uuid=getattr(extracted_edge, "source_node_uuid", None),
            target_node_uuid=getattr(extracted_edge, "target_node_uuid", None),
            counterpart_name=resolved_counterpart[1],
            counterpart_labels=resolved_counterpart[2],
            group_id=getattr(extracted_edge, "group_id", None),
            episode_uuids=getattr(extracted_edge, "episodes", None),
            authority_episode_uuid=attributes.get(SELF_ASSERTION_EDGE_EPISODE_PROPERTY),
            authority_graphiti_episode_uuid=attributes.get(
                SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY
            ),
            predicate=getattr(extracted_edge, "name", None),
            fact=getattr(extracted_edge, "fact", None),
            valid_at=getattr(extracted_edge, "valid_at", None),
            invalid_at=getattr(extracted_edge, "invalid_at", None),
            expired_at=getattr(extracted_edge, "expired_at", None),
        ):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self edge changed after owner authorization"
            )
        if (
            str(attributes.get(SELF_ASSERTION_EDGE_EPISODE_PROPERTY) or "").strip()
            != str(receipt.episode_key or "").strip()
            or str(
                attributes.get(SELF_ASSERTION_EDGE_GRAPHITI_EPISODE_PROPERTY) or ""
            ).strip()
            != str(receipt.graphiti_episode_uuid or "").strip()
        ):
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self edge authority lineage does not match the active episode"
            )
        authorizer = receipt.self_assertion_authorizer
        try:
            decision = (
                authorizer.authorize(proposal)
                if authorizer is not None
                else SelfAuthorizationDecision(False, "owner_confirmation_not_configured")
            )
        except Exception:  # noqa: BLE001 - final write gate always fails closed
            logger.exception(
                "Canonical-self owner confirmation recheck failed episode_id=%s",
                receipt.episode_key,
            )
            decision = SelfAuthorizationDecision(False, "owner_confirmation_check_failed")
        if not decision.authorized:
            raise InvalidSelfSubjectDeclarationError(
                "canonical-self owner confirmation was absent at final edge resolution"
            )

        signed_semantics = {
            name: getattr(extracted_edge, name, None)
            for name in (
                "source_node_uuid",
                "target_node_uuid",
                "name",
                "fact",
                "group_id",
            )
        }
        signed_temporal = {
            name: getattr(extracted_edge, name, None)
            for name in ("valid_at", "invalid_at", "expired_at")
        }
        signed_episodes = list(getattr(extracted_edge, "episodes", None) or [])
        signed_attributes = dict(attributes)
        for candidate in [*related_edges, *existing_edges]:
            if (
                str(getattr(candidate, "source_node_uuid", "") or "").strip()
                == str(getattr(extracted_edge, "source_node_uuid", "") or "").strip()
                and str(getattr(candidate, "target_node_uuid", "") or "").strip()
                == str(getattr(extracted_edge, "target_node_uuid", "") or "").strip()
                and str(getattr(candidate, "name", "") or "")
                == str(getattr(extracted_edge, "name", "") or "")
                and getattr(candidate, "group_id", None) is not None
                and str(getattr(candidate, "group_id", "") or "").strip()
                == str(getattr(extracted_edge, "group_id", "") or "").strip()
                and str(getattr(candidate, "fact", "") or "")
                == str(getattr(extracted_edge, "fact", "") or "")
                and all(
                    canonical_temporal_value(getattr(candidate, name, None))
                    == canonical_temporal_value(value)
                    for name, value in signed_temporal.items()
                )
            ):
                candidate.attributes = dict(signed_attributes)
                episode_uuid = str(getattr(episode, "uuid", "") or "").strip()
                candidate_episodes = list(getattr(candidate, "episodes", None) or [])
                if episode_uuid and episode_uuid not in candidate_episodes:
                    candidate_episodes.append(episode_uuid)
                    candidate.episodes = candidate_episodes
                return candidate, [], []

        resolved_edge, _invalidated, _duplicates = await original(
            llm_client,
            extracted_edge,
            [],
            [],
            episode,
            edge_type_candidates,
        )
        resolved_edge.attributes = dict(signed_attributes)
        for name, value in signed_semantics.items():
            setattr(resolved_edge, name, value)
        for name, value in signed_temporal.items():
            setattr(resolved_edge, name, value)
        resolved_edge.episodes = signed_episodes
        return resolved_edge, [], []

    return _resolve_preserving_owner_authority


def _patch_graphiti_self_authority_edge_resolution() -> bool:
    """Install exact signed-edge preservation at Graphiti's final resolver low point."""

    try:
        import graphiti_core.utils.maintenance.edge_operations as edge_operations

        if getattr(edge_operations, "_menhir_self_authority_edge_patched", False):
            return True
        edge_operations.resolve_extracted_edge = _wrap_self_authority_edge_resolver(  # type: ignore[assignment]
            edge_operations.resolve_extracted_edge
        )
        edge_operations._menhir_self_authority_edge_patched = True
        logger.debug("Graphiti canonical-self authority edge resolver patch applied")
        return True
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti canonical-self edge resolver: %s", exc)
        return False


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
        if receipt.self_subject_endpoint is not None:
            _declare_subject_endpoint(nodes, edges, index_map, receipt)
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
        result = bind_canonical_self(
            nodes, edges, index_map, identity, receipt.self_bind_mode
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


async def _run_graphiti_combined_extraction(
    clients: Any,
    episode: Any,
    previous_episodes: list[Any],
    entity_types: Any,
    excluded_entity_types: Any,
    custom_extraction_instructions: str | None,
) -> tuple[list[Any], list[Any], dict[str, list[int]]]:
    # Resolved at patch time (see `_patch_graphiti_combined_extraction`) so the patch's own
    # ImportError guard covers this dependency. Importing it here instead put the replacement
    # function's real dependency outside the guard: the patch reported success and every
    # subsequent add_episode raised.
    extract_nodes_and_edges = _resolve_combined_extractor()

    receipt = _extraction_receipt.get()
    if receipt is not None:
        receipt.graphiti_episode_uuid = str(getattr(episode, "uuid", "") or "").strip()
        receipt.previous_episode_texts = tuple(
            content
            for item in (previous_episodes or [])
            if isinstance((content := getattr(item, "content", None)), str)
            and content.strip()
        )

    declared_endpoint = receipt.self_subject_endpoint if receipt is not None else None
    # The endpoint is trusted transport metadata, not a grammatical conclusion.  Every enforce
    # projection receives it; text only tells the extractor whether an edge should use it.
    endpoint = declared_endpoint
    if declared_endpoint is not None:
        # Eligibility is rare and enforce-only.  Pay the bounded graph read up front so a marker
        # collision in repair context is rejected before even the first model dispatch; the same
        # cached context is reused if relationless repair is actually needed.
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

    effective_instructions = _combine_extraction_instructions(
        custom_extraction_instructions,
        _relation_completeness_instructions(endpoint),
        endpoint_instructions,
    )
    nodes, edges, index_map = await extract_nodes_and_edges(
        clients,
        episode,
        previous_episodes,
        entity_types=entity_types,
        excluded_entity_types=excluded_entity_types,
        custom_extraction_instructions=effective_instructions,
    )
    if _needs_relationless_repair(receipt, edges):
        assert receipt is not None  # narrowed by _needs_relationless_repair
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
            _relationless_repair_instructions(endpoint),
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
        nodes, edges, index_map = await extract_nodes_and_edges(
            clients,
            episode,
            repair_previous_episodes,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=repair_instructions,
        )
        receipt.relationless_repair_succeeded = bool(edges)
        if not edges:
            # The repair prompt permits a truly relation-free turn to return both lists empty.
            # Do not let that second response erase the first response's evidence that content was
            # extracted and then lost: stamp_and_finalize must still take the visible failure path,
            # not misreport this as an ordinary zero-extraction success.
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
        and receipt is not None
        and any(_unmarked_author_fallbacks(nodes, edges, receipt))
    ):
        # Real models can privilege a familiar `user` convention even when a later instruction
        # declares a safer opaque endpoint. Do not reinterpret that string as provenance. Give the
        # model one bounded correction with no conflicting Menhir-authored `user` instruction;
        # final validation still fails closed if it does not emit the exact marker.
        assert receipt is not None
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
        nodes, edges, index_map = await extract_nodes_and_edges(
            clients,
            episode,
            correction_previous_episodes,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=correction_instructions,
        )
    # Bind the proven human AFTER the relationless-repair branch above: a repair re-runs
    # extraction and replaces nodes/edges/index_map wholesale, so binding before it would be
    # discarded. This is the last point where the payload is final and Graphiti has not yet
    # acquired candidates.
    if receipt is not None and receipt.self_identity is not None:
        receipt.self_bind_result = _record_self_binding(
            nodes, edges, index_map, receipt
        )

    if receipt is not None:
        receipt.resolved_node_count = len(nodes)
        receipt.resolved_edge_count = len(edges)
        surviving_inputs = (
            receipt.raw_entity_count
            - receipt.malformed_entities_dropped
            + receipt.endpoints_synthesized
        )
        receipt.orphan_nodes_dropped = max(0, surviving_inputs - len(nodes))
    return nodes, edges, index_map


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


def _patch_graphiti_combined_extraction() -> bool:
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
            return True
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
        return True
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
        return False


def _patch_graphiti_combined_extraction_models() -> None:
    """Harden the combined-extraction response model: sanitize + close edge endpoints.

    Menhir forces single-episode ``add_episode`` through Graphiti's combined extractor
    (``extract_nodes_and_edges``), whose response model ``CombinedExtraction`` — unlike
    the separate-path ``ExtractedEntities`` that ``_patch_graphiti_entity_extraction``
    already hardens — has NO malformed-row tolerance and NO edge-endpoint closure. That
    left the path Menhir mandates with two live defects:

    1. A single malformed edge row (e.g. missing ``target_entity_name``) fails the whole
       ``CombinedExtraction(**llm_response)`` construction, zeroing the episode.
    2. An edge whose endpoint is absent from ``extracted_entities`` (e.g. ``Alice`` in
       ``Alice -OWNS-> Alice's coins`` when only the possessive was extracted) is dropped
       by Graphiti, then its now-unconnected partner is orphan-pruned — persisting zero
       entities from a content-bearing episode.

    This wraps ``CombinedExtraction`` with a ``mode="before"`` validator that drops only
    malformed rows and materializes missing edge endpoints (generic ``Entity``, gated
    against pronouns/names absent from the current and previous episode context) BEFORE
    validation and Graphiti's own resolution.

    The hardening is scoped to Menhir's forced path via the extraction-receipt ContextVar:
    when no receipt is active (any other combined-extraction caller, e.g. extraction_lab),
    the validator passes the payload through unchanged. The symbol is replaced in BOTH the
    prompts module (source of truth) and the maintenance module (which imports it directly),
    mirroring the dual-module pattern the separate-extraction patch already uses.
    """
    try:
        import graphiti_core.prompts.extract_nodes_and_edges as _ene_module
        import graphiti_core.utils.maintenance.combined_extraction as _ce_module
        from pydantic import BaseModel, Field, model_validator

        if getattr(_ce_module, "_menhir_combined_models_patched", False):
            return

        _CombinedEntity = _ene_module.CombinedEntity
        _CombinedFact = _ene_module.CombinedFact

        class PatchedCombinedExtraction(BaseModel):
            # Field declarations are copied VERBATIM from upstream CombinedExtraction: both
            # required, both described. `model_json_schema()` is what the structured-output path
            # sends as `response_format.json_schema`, so relaxing these to `default_factory=list`
            # told the model both arrays were optional and stripped their descriptions -- weakening
            # the constraint on exactly the local models this patch family exists to compensate for
            # -- and turned a `{}` or typo'd-key response from a loud upstream ValidationError into
            # a silent, successful zero-extraction. The tolerance belongs in the `mode="before"`
            # validator below, which runs ahead of required-field checking and can supply the
            # defaults without changing the schema handed to the model.
            extracted_entities: list[_CombinedEntity] = Field(  # type: ignore[valid-type]
                ..., description="List of extracted entities"
            )
            edges: list[_CombinedFact] = Field(  # type: ignore[valid-type]
                ..., description="List of extracted relationship facts"
            )

            @model_validator(mode="before")
            @classmethod
            def _menhir_sanitize(cls, data: Any) -> Any:
                receipt = _extraction_receipt.get()
                if receipt is None:
                    return data  # not Menhir's forced path — leave payload untouched
                return _sanitize_combined_payload(data, receipt, receipt.episode_text)

        _ene_module.CombinedExtraction = PatchedCombinedExtraction  # type: ignore[assignment]
        _ce_module.CombinedExtraction = PatchedCombinedExtraction  # type: ignore[assignment]
        _ce_module._menhir_combined_models_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti combined-extraction model hardening patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti combined-extraction models: %s", exc)
