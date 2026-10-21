"""Gold scoring (exact + LLM fuzzy match) for the Explorer Extraction Lab.

Extracted verbatim from extraction_lab.py; the original module re-exports
every name defined here.
"""

from __future__ import annotations

import json
import logging

from menhir.explorer.extraction_lab_models import (
    ExtractionGoldScore,
    ExtractionResult,
    GoldExtraction,
)

logger = logging.getLogger(__name__)

def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation, collapse whitespace."""
    import re
    # Convert to lowercase
    normalized = text.lower().strip()
    # Remove leading/trailing punctuation
    normalized = re.sub(r'^[^\w]+|[^\w]+$', '', normalized)
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def _text_matches(extracted: str, gold: str) -> bool:
    """Check if extracted text matches gold text (after normalization)."""
    return _normalize_text(extracted) == _normalize_text(gold)


def _compute_set_metrics(extracted_set: set[str], gold_set: set[str]) -> tuple[float, float]:
    """Compute recall and precision for a set comparison.

    Returns (recall, precision).
    """
    if not gold_set:
        # No gold items expected; perfect score if nothing extracted, otherwise penalize
        return (1.0 if not extracted_set else 0.0, 1.0 if not extracted_set else 0.0)

    if not extracted_set:
        # Expected items but got nothing
        return (0.0, 0.0 if gold_set else 1.0)

    true_positives = len(extracted_set & gold_set)
    recall = true_positives / len(gold_set)
    precision = true_positives / len(extracted_set)
    return (recall, precision)


_FUZZY_MATCH_SYSTEM_PROMPT = """You are a precise semantic-equivalence classifier for an
extraction-quality evaluation harness. You compare a list of EXTRACTED items against a list
of GOLD (expected) items of the same kind (either entity mentions or factual propositions).

Two items match if they refer to the same real-world entity or assert the same fact, even if
worded differently (e.g. "the suburbs" matches "suburban area"; "Rachel moved back to the
suburbs" matches "Rachel currently resides in the suburbs"). They do NOT match merely because
they share a topic or are both plausible -- the meaning must be equivalent.

Return JSON only: {"matches": [{"extracted_index": int, "gold_index": int}, ...]}
Each extracted_index and each gold_index may appear in at most one pair. Omit items with no
match. Do not include markdown."""


def _fuzzy_match_prompt(kind: str, extracted_items: list[str], gold_items: list[str]) -> str:
    extracted_block = "\n".join(f"{i}: {text!r}" for i, text in enumerate(extracted_items))
    gold_block = "\n".join(f"{i}: {text!r}" for i, text in enumerate(gold_items))
    return (
        f"KIND: {kind}\n\n"
        f"EXTRACTED ITEMS:\n{extracted_block}\n\n"
        f"GOLD ITEMS:\n{gold_block}\n\n"
        'Return JSON only, e.g. {"matches": [{"extracted_index": 0, "gold_index": 1}]}'
    )


def _parse_fuzzy_matches(raw: str) -> list[dict[str, int]]:
    import re as _re

    cleaned = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = _re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=_re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return []
    matches = parsed.get("matches")
    if not isinstance(matches, list):
        return []
    return [
        m for m in matches
        if isinstance(m, dict)
        and isinstance(m.get("extracted_index"), int)
        and isinstance(m.get("gold_index"), int)
    ]


async def _fuzzy_matched_counts(
    llm_backend: object | None,
    kind: str,
    unmatched_extracted: list[str],
    unmatched_gold: list[str],
) -> tuple[int, int]:
    """Ask the judge LLM which of the still-unmatched extracted/gold items are semantic
    matches. Returns (extracted_items_validated, gold_items_covered) -- NOT necessarily
    equal, since one extracted item validates one gold item per matched pair, but the
    caller adds these counts on top of exact-match counts for the final recall/precision.

    Narrow by design (fidelity contract for the harness's SCORING layer, not the
    extraction call itself, so no restore/concurrency concerns here): only called on the
    leftover items exact string matching couldn't resolve, one classification call per
    (kind, arm), not one call per item pair. Fails safe to (0, 0) -- no LLM configured,
    an unparseable response, or an API error all just mean "no fuzzy matches found",
    never a crash and never an inflated score.
    """
    if llm_backend is None or not unmatched_extracted or not unmatched_gold:
        return (0, 0)
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        return (0, 0)
    try:
        raw = await create(
            system_prompt=_FUZZY_MATCH_SYSTEM_PROMPT,
            user_prompt=_fuzzy_match_prompt(kind, unmatched_extracted, unmatched_gold),
            operation="extraction_lab_fuzzy_match",
            max_tokens=600,
            temperature=0.0,
        )
    except Exception:
        logger.warning("Extraction Lab fuzzy-match call failed", exc_info=True)
        return (0, 0)

    pairs = _parse_fuzzy_matches(raw)
    used_extracted: set[int] = set()
    used_gold: set[int] = set()
    for pair in pairs:
        ei, gi = pair["extracted_index"], pair["gold_index"]
        if not (0 <= ei < len(unmatched_extracted)) or not (0 <= gi < len(unmatched_gold)):
            continue
        if ei in used_extracted or gi in used_gold:
            continue
        used_extracted.add(ei)
        used_gold.add(gi)
    return (len(used_extracted), len(used_gold))


async def _score_extraction(
    extracted: ExtractionResult,
    gold: GoldExtraction,
    *,
    llm_backend: object | None = None,
) -> ExtractionGoldScore:
    """Score extracted entities/edges against gold ground truth.

    Two-tier: exact normalized-string set comparison first (deterministic, free), then an
    LLM-assisted fuzzy-match pass over whatever exact matching left unmatched on both
    sides (see _fuzzy_matched_counts). Without the fuzzy tier, real LLM extraction output
    ("Rachel moved back to the suburbs again.") almost never exactly matches hand-written
    gold text ("Rachel moved to or currently resides in the suburbs") even when the
    extraction is correct -- confirmed against a live Phase 1 run before this was added
    (proposition_recall read 0.00 across every arm despite update_aware genuinely
    extracting the right fact). llm_backend=None (the default) skips the fuzzy tier
    entirely and falls back to pure exact matching -- callers that don't want the extra
    LLM call (e.g. the Phase 0 unit tests) get the old deterministic-only behavior.
    """
    # Extract normalized mention texts
    extracted_mentions = [m.get("text", "") for m in extracted.mentions if m.get("text")]
    gold_mentions = list(gold.mentions)

    mention_recall, mention_precision, mention_unsupported = await _scored_set_comparison(
        llm_backend, "entity mention", extracted_mentions, gold_mentions
    )

    # Extract propositions (simplified: just the fact text)
    extracted_props = [p.get("fact", "") for p in extracted.propositions if p.get("fact")]
    gold_props = list(gold.propositions)

    proposition_recall, proposition_precision, prop_unsupported = await _scored_set_comparison(
        llm_backend, "factual proposition", extracted_props, gold_props
    )

    # Check for update language capture
    extracted_text = " ".join(str(m.get("text", "")) for m in extracted.mentions)
    update_capture_rate = 1.0
    if gold.update_language:
        captured = sum(
            1 for update_phrase in gold.update_language
            if _normalize_text(update_phrase) in _normalize_text(extracted_text)
        )
        update_capture_rate = captured / len(gold.update_language)

    total_unsupported = mention_unsupported + prop_unsupported
    total_extracted = len(extracted_mentions) + len(extracted.propositions)
    unsupported_inference_rate = (
        total_unsupported / total_extracted if total_extracted > 0 else 0.0
    )

    return ExtractionGoldScore(
        mention_recall=mention_recall,
        mention_precision=mention_precision,
        proposition_recall=proposition_recall,
        proposition_precision=proposition_precision,
        update_capture_rate=update_capture_rate,
        unsupported_inference_rate=unsupported_inference_rate,
    )


async def _scored_set_comparison(
    llm_backend: object | None,
    kind: str,
    extracted_items: list[str],
    gold_items: list[str],
) -> tuple[float, float, int]:
    """Exact match first, then fuzzy-match the leftovers. Returns (recall, precision,
    unsupported_count) -- unsupported_count is extracted items matched to nothing, exact
    or fuzzy, for the caller's unsupported_inference_rate."""
    extracted_norm = [_normalize_text(x) for x in extracted_items]
    gold_norm = [_normalize_text(x) for x in gold_items]

    # Exact matching: greedily pair each extracted item to an unused identical gold item.
    gold_available = list(range(len(gold_norm)))
    exact_matched_extracted: set[int] = set()
    exact_matched_gold: set[int] = set()
    for ei, etext in enumerate(extracted_norm):
        for gi in gold_available:
            if gi in exact_matched_gold:
                continue
            if gold_norm[gi] == etext:
                exact_matched_extracted.add(ei)
                exact_matched_gold.add(gi)
                break

    unmatched_extracted = [extracted_items[i] for i in range(len(extracted_items)) if i not in exact_matched_extracted]
    unmatched_gold = [gold_items[i] for i in range(len(gold_items)) if i not in exact_matched_gold]

    fuzzy_extracted_count, fuzzy_gold_count = await _fuzzy_matched_counts(
        llm_backend, kind, unmatched_extracted, unmatched_gold
    )

    matched_extracted_total = len(exact_matched_extracted) + fuzzy_extracted_count
    matched_gold_total = len(exact_matched_gold) + fuzzy_gold_count

    if not gold_items:
        recall = 1.0 if not extracted_items else 0.0
        precision = 1.0 if not extracted_items else 0.0
    elif not extracted_items:
        recall, precision = 0.0, 0.0
    else:
        recall = matched_gold_total / len(gold_items)
        precision = matched_extracted_total / len(extracted_items)

    unsupported_count = len(extracted_items) - matched_extracted_total
    return (recall, precision, max(0, unsupported_count))
