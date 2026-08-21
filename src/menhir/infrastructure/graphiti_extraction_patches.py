"""Combined Graphiti extraction and extraction-receipt compatibility patches."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import re
from time import perf_counter
from typing import Any, Callable

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
    return usable_edges > 0 and receipt.self_echo_edges_suppressed >= usable_edges


def begin_extraction_receipt(
    episode_key: str,
    episode_text: str,
    *,
    source_description: str = "",
    relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None,
) -> CombinedExtractionReceipt:
    """Create and activate a fresh receipt for the current episode (call in the parent task)."""
    receipt = CombinedExtractionReceipt(
        episode_key=str(episode_key or ""),
        episode_text=str(episode_text or ""),
        source_description=str(source_description or ""),
        relationless_repair_context_loader=relationless_repair_context_loader,
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


def _is_self_endpoint(normalized_name: str, episode_text: str) -> bool:
    """True when this endpoint denotes the HUMAN and may bind to the canonical self entity.

    WHY THIS EXISTS: gpt-4o-mini emits the speaker as the literal token ``user`` and never as
    ``I``. ``user`` is in `_NON_SYNTHESIZABLE_ENDPOINTS`, so every edge it anchors was dropped for
    want of an endpoint; graphiti then orphan-pruned every node those edges would have connected,
    and content-bearing episodes persisted nothing (CombinedExtractionCollapsedError). Measured on
    the cc5ded98 smoke: 5 of 6 USER turns collapsed this way -- the refusal was destroying
    precisely the user's own facts, which is the opposite of what it was protecting.

    Binding to ONE canonical ``user`` node per namespace is the intended identity, not the
    fragmentation the original guard feared: graphiti dedups by normalized name within `group_id`,
    so repeated turns converge on the same node.

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

    Unknown role (no ``user:``/``assistant:`` prefix) binds, so content outside the benchmark's
    prefixed format keeps the collapse fix rather than silently regressing.
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
        entities.append(norm)

    edges: list[dict[str, Any]] = []
    edges_dropped = 0
    for item in raw_edges:
        norm = _sanitize_combined_edge(item)
        if norm is None:
            edges_dropped += 1
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
    self_bound = 0
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
            if _is_self_endpoint(norm_key, episode_text):
                # Rewrite to the canonical self display and materialize it ONCE per payload, so
                # every self-anchored edge in this episode converges on a single node instead of
                # being dropped for a missing endpoint. See `_is_self_endpoint` for why this is
                # the intended identity rather than the fragmentation the old guard feared.
                edge[endpoint_key] = _SELF_ENTITY_NAME
                if self_key not in known:
                    entities.append({"name": _SELF_ENTITY_NAME, "entity_type_id": -1})
                    known.add(self_key)
                self_bound += 1
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
            # orphan-pruned and the whole episode collapses. That cascade is why self endpoints are
            # bound above instead of refused.
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
        or self_bound
        or self_echo_edges
        or list_edges_added
        or context_unsupported_edges
    ):
        logger.info(
            "Combined-extraction sanitation: entities_dropped=%d edges_dropped=%d "
            "endpoints_synthesized=%d self_endpoints_bound=%d self_echo_edges_suppressed=%d "
            "list_membership_edges_added=%d context_unsupported_edges_suppressed=%d "
            "(raw entities=%d edges=%d)",
            entities_dropped,
            edges_dropped,
            synthesized,
            self_bound,
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
        receipt.previous_episode_texts = tuple(
            content
            for item in (previous_episodes or [])
            if isinstance((content := getattr(item, "content", None)), str)
            and content.strip()
        )

    effective_instructions = _combine_extraction_instructions(
        custom_extraction_instructions,
        _RELATION_COMPLETENESS_INSTRUCTIONS,
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
        receipt.relationless_repair_context_texts = _load_relationless_repair_context(
            receipt
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
            _RELATIONLESS_REPAIR_INSTRUCTIONS,
            (
                _RELATIONLESS_REPAIR_CONTEXT_INSTRUCTIONS
                if receipt.relationless_repair_context_texts
                else None
            ),
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
