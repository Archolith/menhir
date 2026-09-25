"""Edge-endpoint normalization, self-label classification, and grounding guards.

Owns graphiti-compatible endpoint-name normalization, the reserved subject-marker guard, the
self-like endpoint policy sets, and the literal-token grounding tests used by payload sanitation.
Extracted verbatim from ``graphiti_extraction_patches``; that module re-exports everything here.
"""

from __future__ import annotations

import re
from typing import Any

from menhir.domain.self_identity import SUBJECT_ENDPOINT_MARKER_PREFIX
from menhir.infrastructure.graphiti_extraction_patches_receipt import CombinedExtractionReceipt
from menhir.infrastructure.self_binding import SelfBindMode


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
#: prompt (`_RELATIONLESS_REPAIR_CORE`) instructs the model to emit relation labels -- so
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
