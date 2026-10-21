"""Prompt-variant constants and graphiti-core prompt patching for the
Explorer Extraction Lab.

Extracted verbatim from extraction_lab.py; the original module re-exports
every name defined here.
"""

from __future__ import annotations

from typing import Any, Callable

from menhir.explorer.extraction_lab_models import PROMPT_VARIANTS

#: The exact sentence in graphiti-core's real default extraction prompt
#: (graphiti_core/prompts/extract_nodes.py, extract_message()'s "4. Exclusions" section)
#: that the RCA identified as the mechanism behind under-extraction on sparse context.
#: Matched by substring, not exact line/whitespace, so this survives minor upstream
#: formatting changes without silently no-op'ing (the bug this replaces).
_WHEN_IN_DOUBT_SENTENCE = "When in doubt, do NOT extract."

#: Variant A's "Specific removal or modification" replacement (menhir-extraction-prompt-
#: recency-recall-research.md) -- a minimal, surgical swap of the one sentence, distinct
#: from `minus_when_in_doubt` (pure removal, condition 2 of the plan's 8-condition matrix)
#: and from the richer variants below (which layer on a whole additional section instead
#: of touching this sentence).
_MINIMAL_RECALL_PATCH_REPLACEMENT = (
    "When in doubt about whether content was explicitly stated, do not invent it.\n"
    "   - When the content is explicit but identity resolution is uncertain, extract it "
    "and preserve that uncertainty."
)

#: Richer variants (conditions 4-8) append a new numbered section to the real prompt
#: rather than rewriting it wholesale -- this keeps the live entity-type/context
#: injection and the production few-shot <EXAMPLE> blocks intact (Fidelity contract:
#: only the declared tuning parameter differs), while still delivering each variant's
#: proposed guidance verbatim from the research plan's "Proposed instructions" sections.
_VARIANT_APPEND_SECTIONS: dict[str, str] = {
    "mention_first": """7. **Mention-First Extraction (variant: mention_first):**
   Your task has two stages.
   STAGE 1 -- MENTION CAPTURE: identify concrete entities explicitly mentioned in the
   CURRENT MESSAGE. Capture explicit mentions even when the entity is not globally
   unique, the mention is informal, the mention depends on earlier conversational
   context, or the entity cannot yet be linked to an existing graph node. Examples that
   should be captured: Rachel, the suburbs, her old job, their new apartment, the
   previous doctor.
   STAGE 2 -- NORMALIZATION HINT: for each mention, provide the most reasonable
   normalized name and type. If normalization is uncertain, preserve the original text,
   mark resolution as uncertain, and do not omit the mention. Never invent a person,
   place, or object not explicitly present in the current message.""",
    "update_aware": """7. **Update-Aware Extraction (variant: update_aware):**
   Pay special attention to statements that update, correct, reverse, or refine earlier
   information. Update indicators include: actually, now, no longer, moved back,
   changed to, instead, again, recently, turns out, I was wrong, correction. When one of
   these indicators appears, extract all concrete participants and the newly asserted
   state from the CURRENT MESSAGE, even if the prior state is not visible in the
   supplied context. The absence of the prior state must not prevent extraction of the
   new state. Example: "Rachel actually just moved back to the suburbs again." requires
   extracting Rachel (person), suburbs (location/residence-area), and the proposition
   that Rachel moved or resides in the suburbs -- without requiring the prior Chicago
   statement to be visible.""",
    "proposition_first": """7. **Proposition-First Extraction (variant: proposition_first):**
   First identify every concrete factual proposition asserted by the CURRENT MESSAGE --
   a person, object, organization, event, preference, possession, location,
   relationship, or state that could matter in a future conversation. Then identify the
   entities required to represent each proposition. Do not omit a proposition merely
   because one entity is informal, an entity requires later resolution, the value is
   relative rather than canonical, or the proposition refers to a prior state not
   included in context.""",
    "mention_first_update_aware": """7. **Mention-First + Update-Aware (variant: mention_first_update_aware):**
   Your task has two stages, with special attention to updates.
   STAGE 1 -- MENTION CAPTURE (PRIORITIZE UPDATES): identify concrete entities explicitly
   mentioned in the CURRENT MESSAGE. Pay special attention to statements that update,
   correct, reverse, or refine earlier information (indicators: actually, now, no
   longer, moved back, changed to, instead, again, recently, turns out, I was wrong,
   correction) -- when one appears, extract all concrete participants and the newly
   asserted state even if prior context is not visible. Capture explicit mentions even
   when the entity is not globally unique, the mention is informal, or the entity cannot
   yet be linked to an existing graph node.
   STAGE 2 -- NORMALIZATION HINT: preserve the original text and mark resolution as
   uncertain rather than omitting a mention. Never invent a person, place, or object not
   explicitly present in the current message.""",
    "proposition_first_structured": """7. **Proposition-First + Structured Uncertainty (variant: proposition_first_structured):**
   First identify every concrete factual proposition asserted by the CURRENT MESSAGE,
   with explicit uncertainty markers. For each proposition, identify: required entities,
   update language (if any), resolution status (resolved, unresolved, non-canonical),
   and confidence (0.0-1.0). Do not omit a proposition merely because one entity is
   informal, requires later resolution, is relative rather than canonical, or refers to
   a prior state not included in context. Uncertain identity does not mean absent fact.""",
}


#: Marker in graphiti-core's real prompt (extract_nodes.py) that closes the CURRENT
#: MESSAGE block, right before the numbered extraction rules begin. The Phase 2
#: candidate-lookup block is inserted immediately after this, so it reads as
#: context adjacent to the message being extracted, not buried near the examples.
_CURRENT_MESSAGE_CLOSE_MARKER = "</CURRENT MESSAGE>"


def _known_entities_section(known_entities: list[str]) -> str:
    """Phase 2 (menhir-belief-supersession-code-mapped-plan.md): the candidate-lookup
    signal injected into the extraction prompt. Deliberately short and declarative --
    this is a fact ("these names are already known"), not an instruction to extract
    them unconditionally; the model still judges whether the CURRENT MESSAGE actually
    asserts something about them."""
    names = "\n".join(f"- {name}" for name in known_entities)
    return (
        "<KNOWN ENTITIES>\n"
        "The following names are already established entities in this conversation's "
        "memory graph (found via a name-match lookup against the graph directly, "
        "independent of which earlier messages are visible above in PREVIOUS MESSAGES). "
        "If the CURRENT MESSAGE mentions one of these by name, that mention refers to an "
        "existing, trackable entity -- do not withhold extracting it merely because its "
        "establishing context is not visible above.\n"
        f"{names}\n"
        "</KNOWN ENTITIES>"
    )


def _retrieved_context_section(text: str) -> str:
    """Phase 2 context-form ablation, condition J ("retrieved relevant historical
    episode, independent of recency"): a distinctly-labeled block, separate from
    both PREVIOUS MESSAGES and KNOWN ENTITIES. Models delivering the SAME content as
    condition C (an ordinary previous_episode inside the recency window) through a
    different channel -- framed as retrieved-by-relevance, not recency-windowed --
    so the ablation can separate "does content matter" from "does delivery format
    matter" as two different questions."""
    return (
        "<RETRIEVED CONTEXT>\n"
        "The following prior conversation content was retrieved as directly relevant "
        "to the CURRENT MESSAGE (found via a relevance-based search, independent of "
        "how recently it occurred in the conversation):\n\n"
        f"{text}\n"
        "</RETRIEVED CONTEXT>"
    )


def _apply_extraction_patches(
    variant: PROMPT_VARIANTS,
    known_entities: list[str] | None = None,
    retrieved_context: str | None = None,
) -> Callable[[], None]:
    """Monkey-patch graphiti-core's real extraction prompt for one variant, optionally
    composed with Phase 2's candidate-lookup signal.

    Returns a restore() callable that MUST be invoked (see _run_extraction_arm's
    finally block) -- prompt_library is process-global shared state, not
    request-scoped, so a caller that forgets to restore leaves every subsequent
    extraction call (in this process, including production traffic if this ever
    ran in the same process) silently running under the last-applied variant.

    Faithful to the fidelity contract: builds on graphiti-core's REAL live prompt
    (via extract_message(context), the same function node_operations.extract_nodes
    calls through prompt_library.extract_nodes.extract_message) rather than a
    hand-copied template, so this only ever diverges from production by the
    documented per-variant text edit (and, if known_entities is non-empty, the one
    additional KNOWN ENTITIES block) -- the real entity-type/context injection and
    the production few-shot <EXAMPLE> blocks are always preserved unchanged.

    For baseline with no known_entities, this is a no-op and returns a no-op restore.
    """
    if (
        variant in ("baseline", "combined_extraction")
        and not known_entities
        and not retrieved_context
    ):
        return lambda: None

    from graphiti_core.prompts import prompt_library
    from graphiti_core.prompts.extract_nodes import extract_message as _default_extract_message
    from graphiti_core.prompts.lib import VersionWrapper
    from graphiti_core.prompts.models import Message

    original_wrapper = prompt_library.extract_nodes.extract_message

    def _apply_variant_edit(content: str) -> str:
        if variant == "minus_when_in_doubt":
            # Condition 2 of the plan's 8-condition matrix: pure removal, nothing added.
            return content.replace(f"   - {_WHEN_IN_DOUBT_SENTENCE}\n", "").replace(
                _WHEN_IN_DOUBT_SENTENCE, ""
            )
        if variant == "minimal_recall_patch":
            # Condition 3: surgical single-sentence swap (Variant A's own "Specific
            # removal or modification", not its longer "Proposed instructions" prose --
            # that prose is closer in scope to mention_first, so using it here would
            # make conditions 3 and 4 not meaningfully distinct).
            return content.replace(_WHEN_IN_DOUBT_SENTENCE, _MINIMAL_RECALL_PATCH_REPLACEMENT)
        append_section = _VARIANT_APPEND_SECTIONS.get(variant)
        if append_section is None:
            return content
        # Conditions 4-8: append as a new numbered section before the production
        # <EXAMPLE> few-shot blocks, so those examples (and the entity-type/context
        # injection above them) are untouched -- additive, not a rewrite.
        marker = "\n<EXAMPLE>"
        if marker in content:
            return content.replace(marker, f"\n{append_section}\n{marker}", 1)
        return content + f"\n\n{append_section}"

    def _apply_extra_blocks(content: str) -> str:
        blocks: list[str] = []
        if known_entities:
            blocks.append(_known_entities_section(known_entities))
        if retrieved_context:
            blocks.append(_retrieved_context_section(retrieved_context))
        if not blocks:
            return content
        combined = "\n\n".join(blocks)
        if _CURRENT_MESSAGE_CLOSE_MARKER in content:
            return content.replace(
                _CURRENT_MESSAGE_CLOSE_MARKER, f"{_CURRENT_MESSAGE_CLOSE_MARKER}\n\n{combined}", 1
            )
        return content + f"\n\n{combined}"

    def _edit_user_prompt(content: str) -> str:
        # Composable, not exclusive: a variant edit, the known-entities block, and the
        # retrieved-context block can all apply to the same underlying real prompt in
        # one patch/restore cycle.
        return _apply_extra_blocks(_apply_variant_edit(content))

    def variant_extract_message(context: dict[str, Any]) -> list[Message]:
        messages = _default_extract_message(context)
        for message in messages:
            if message.role == "user":
                message.content = _edit_user_prompt(message.content)
        return messages

    prompt_library.extract_nodes.extract_message = VersionWrapper(variant_extract_message)

    def _restore() -> None:
        prompt_library.extract_nodes.extract_message = original_wrapper

    return _restore
