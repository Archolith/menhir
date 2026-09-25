"""Prompt-instruction rendering and final-payload query helpers for combined extraction.

Owns the relation-completeness and repair instruction contracts, the first-person gate, the
author-alias query, adjacent-context loading and delimiting, and the repair-episode builder.
Extracted verbatim from ``graphiti_extraction_patches``; that module re-exports everything here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from menhir.domain.self_identity import SelfSubjectEndpointEnvelope, is_self_alias
from menhir.infrastructure.graphiti_extraction_patches_receipt import CombinedExtractionReceipt

logger = logging.getLogger(__name__)

#: Subject-neutral half of the relation-completeness contract. "The subject", not "the
#: speaker": for third-person text the speaker is precisely the wrong thing to steer toward.
_RELATION_COMPLETENESS_CORE = """\
MENHIR RELATION COMPLETENESS:
- Do not return an entity without a relationship when CURRENT MESSAGES state what the subject
  does, owns, uses, prefers, plans, experiences, believes, or explicitly wants to learn about
  that entity.
- Do not invent a relationship merely to connect an entity. If the current text truly states no
  relationship, omit the entity as well.
"""

#: First-person half. Appended ONLY when the episode text actually contains a first-person
#: reference (`_is_first_person`). Issue #90: appended unconditionally, gpt-4o-mini applied
#: "represent I/me/my with `user`" to a third-person subject and rewrote "Alice owns 37 coins"
#: as "User owns 37 coins" -- no `Alice` node was ever created, and the bogus `user` cascaded
#: into a self-fork, a self-subject perceiver proposal, and a missing View.
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
    """Render the relation-completeness contract for one episode.

    The self-binding rules are appended only when `episode_text` is first-person. The gate is
    the SAME predicate `_unresolved_author_aliases` uses to decide whether a `user` node is the
    author, so the prompt that produces `user` and the post-processing that trusts it cannot
    disagree about what counts as first-person.
    """
    if not _is_first_person(episode_text):
        # NOTHING for third-person text -- not even the subject-neutral core. Live evidence
        # (2026-09-12, fix commit 7e0d4124): with core alone, "Alice wakes up at 7:30 AM."
        # extracted zero entities, because "omit the entity if no relationship is stated"
        # with no relationship shape to follow made the model drop everything. The
        # 2026-07-20 run with no block at all persisted `Alice` + `7:30 AM` and materialized
        # the View. 811dd41b introduced this block to repair relationless FIRST-PERSON
        # memories; third-person text never needed it and is worse off with any of it.
        return ""
    core = _RELATION_COMPLETENESS_CORE
    marker = endpoint.marker if endpoint is not None else None
    # The self-binding bullets go in front of the closing "do not invent" rule so that rule
    # stays the block's final word, as it was before the split.
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


# A refusal hint, NOT proof of authorship or subjecthood. Do not add quote/grammar
# exceptions: a match can only withhold an ambiguous alias, never authorize a bind.
_AUTHOR_REFERENCE_RE = re.compile(r"\b(?:i|me|my|mine|myself)\b", re.IGNORECASE)


def _is_first_person(text: str) -> bool:
    """True when `text` contains a first-person singular reference.

    The single source of truth for "is the author speaking?" -- consulted by the prompt that
    asks the model to emit `user` (`_relation_completeness_instructions`) AND by the
    post-processing that decides whether an emitted `user` is the author
    (`_unresolved_author_aliases`). Plural first person (we/our/us) is deliberately not here;
    that is pre-existing behaviour and a separate question.
    """
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
