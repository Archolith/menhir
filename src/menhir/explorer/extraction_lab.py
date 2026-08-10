"""Read-only extraction harness experiments for the Explorer Extraction Lab.

Phase 0 (instrumentation): Replicate production extraction pipeline in a testable,
multi-arm format to measure prompt/schema effects on entity extraction recall/precision.

Fidelity contract: identical to production extraction except for the one tuning knob
under test (prompt_variant, model, or context_episode_count). See
.agent/plans/menhir-belief-supersession-code-mapped-plan.md phase-0 spec for details.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

# Prompt variant identifiers — must match .agent/plans/menhir-extraction-prompt-recency-recall-research.md
PROMPT_VARIANTS = Literal[
    "baseline",
    "minus_when_in_doubt",
    "minimal_recall_patch",
    "mention_first",
    "update_aware",
    "proposition_first",
    "mention_first_update_aware",
    "proposition_first_structured",
    "combined_extraction",
]


class ExtractionLabTuning(BaseModel):
    """Tuning controls for extraction lab arms."""

    model_config = ConfigDict(extra="forbid")

    prompt_variant: PROMPT_VARIANTS = "baseline"
    model: str | None = None  # None means use production default (resolved at runtime)
    context_episode_count: int = Field(default=10, ge=1, le=100)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # Phase 2 (menhir-belief-supersession-code-mapped-plan.md): extraction-time candidate
    # lookup. When True AND ExtractionLabRequest.source_namespace is set, look up existing
    # PERSISTENT entity names in that real namespace (independent of context_episode_count /
    # RELEVANT_SCHEMA_LIMIT) and surface any that appear in current_message as a "this name
    # is already known" signal to the extractor -- composes with prompt_variant, does not
    # replace it. No-op (fails safe) if source_namespace is unset, matching every other
    # candidate-lookup no-op path in this module.
    enable_candidate_lookup: bool = False
    # Phase 2 context-form ablation (menhir-extraction-context-ablation-handoff.md).
    # When not None, this list IS the known_entities signal for the arm -- bypasses
    # _lookup_known_entities entirely (no DB query), even when enable_candidate_lookup
    # is also True. Lets a synthetic condition (e.g. ablation condition B, "entity-name
    # signal only") assert an exact known-entity set deterministically, independent of
    # whatever the live graph happens to contain -- a real DB lookup would introduce
    # exactly the kind of run-to-run content variance the ablation needs to control
    # away, on top of the model's own sampling variance already being measured
    # separately (Phase 1).
    forced_known_entities: list[str] | None = None
    # Phase 2: injects a distinctly-labeled "<RETRIEVED CONTEXT>" block (see
    # _retrieved_context_section), separate from both PREVIOUS MESSAGES and KNOWN
    # ENTITIES -- models ablation condition J ("retrieved relevant historical episode,
    # independent of recency") as a delivery-format variable distinct from condition C
    # (the same content delivered as an ordinary previous_episode, inside the recency
    # window). None means no block is injected.
    retrieved_context: str | None = None


class ExtractionLabArm(BaseModel):
    """An extraction experiment arm (baseline or variant)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    tuning: ExtractionLabTuning = Field(default_factory=ExtractionLabTuning)


class EpisodeFixture(BaseModel):
    """A prior episode (previous context for extraction)."""

    model_config = ConfigDict(extra="forbid")

    # 8000, not 2000: real LME assistant turns run long (observed up to ~3552 chars in
    # the 3 RCA fixtures) -- a tight bound here would force truncating real conversation
    # text, which is a worse fidelity violation than a generous bound.
    text: str = Field(min_length=1, max_length=8000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GoldExtraction(BaseModel):
    """Expected extraction results (ground truth) for scoring."""

    model_config = ConfigDict(extra="forbid")

    mentions: list[str] = Field(default_factory=list)  # Expected entity mention texts
    propositions: list[str] = Field(default_factory=list)  # Expected fact/edge texts
    update_language: list[str] = Field(default_factory=list)  # Update indicators (e.g., "actually", "moved back")


class ExtractionLabRequest(BaseModel):
    """Request to run extraction lab experiments."""

    model_config = ConfigDict(extra="forbid")

    current_message: str = Field(min_length=1, max_length=2000)
    previous_episodes: list[EpisodeFixture] = Field(default_factory=list)
    gold: GoldExtraction = Field(default_factory=GoldExtraction)
    arms: list[ExtractionLabArm] = Field(min_length=1, max_length=16)
    # Real graphiti-core namespace (group_id) to query for Phase 2's candidate lookup, e.g.
    # "lme-830ce83f". Shared across all arms of one request (it describes the fixture's real
    # source graph, not a per-arm tuning choice) -- only arms with
    # tuning.enable_candidate_lookup=True actually use it. None (the default) means no arm
    # can do a lookup even if it requests one; synthetic fixtures with no real backing graph
    # correctly leave this unset.
    source_namespace: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_request(self) -> ExtractionLabRequest:
        self.current_message = self.current_message.strip()
        if not self.current_message:
            raise ValueError("current_message must not be blank")
        enabled = [arm for arm in self.arms if arm.enabled]
        if not enabled:
            raise ValueError("at least one arm must be enabled")
        ids = [arm.id for arm in self.arms]
        if len(ids) != len(set(ids)):
            raise ValueError("arm ids must be unique")
        return self


class ExtractionResult(BaseModel):
    """Extracted entities and edges from a single arm."""

    model_config = ConfigDict(extra="forbid")

    mentions: list[dict[str, Any]] = Field(default_factory=list)  # {text, labels, ...}
    propositions: list[dict[str, Any]] = Field(default_factory=list)  # {fact, source, target, ...}


class ExtractionGoldScore(BaseModel):
    """Gold-based scoring for an extraction result."""

    model_config = ConfigDict(extra="forbid")

    mention_recall: float = Field(ge=0.0, le=1.0)
    mention_precision: float = Field(ge=0.0, le=1.0)
    proposition_recall: float = Field(ge=0.0, le=1.0)
    proposition_precision: float = Field(ge=0.0, le=1.0)
    update_capture_rate: float = Field(ge=0.0, le=1.0)
    unsupported_inference_rate: float = Field(ge=0.0, le=1.0)


class ExtractionLabArmResult(BaseModel):
    """Results from a single arm run."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    ok: bool = True
    enabled: bool = True
    elapsed_ms: float
    tuning: dict[str, Any]
    error: str | None = None
    extraction: ExtractionResult | None = None
    gold_scores: ExtractionGoldScore | None = None
    degraded: bool = False
    # Phase 2 transparency: what the candidate lookup actually found for this arm, if
    # enabled. Empty list covers both "lookup disabled" and "lookup ran, found nothing"
    # -- distinguishable from the tuning dict (enable_candidate_lookup + whether the
    # request had a source_namespace at all).
    known_entities_used: list[str] = []


class ExtractionLabRunPayload(BaseModel):
    """Complete extraction lab run result."""

    model_config = ConfigDict(extra="forbid")

    current_message: str
    arms: list[ExtractionLabArmResult]


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


async def _run_extraction_arm(
    arm: ExtractionLabArm,
    request: ExtractionLabRequest,
    *,
    llm_backend: object | None = None,
) -> ExtractionLabArmResult:
    """Run a single extraction arm: apply patches, extract, score."""
    started = perf_counter()
    restore_prompt: Callable[[], None] | None = None

    try:
        # Import here to ensure patches are applied at construction time if needed
        from menhir.config import MemorySettings
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from graphiti_core.nodes import EpisodicNode, EpisodeType
        from graphiti_core.utils.datetime_utils import utc_now
        from graphiti_core.utils.maintenance.node_operations import (
            extract_nodes,
            resolve_extracted_nodes,
        )
        from graphiti_core.utils.maintenance.edge_operations import (
            extract_edges,
            resolve_extracted_edges,
        )
        from graphiti_core.utils.bulk_utils import resolve_edge_pointers
        from graphiti_core.utils.maintenance.combined_extraction import (
            extract_nodes_and_edges as extract_combined,
        )

        # Build GraphitiClient via production path (patches are applied here).
        # MemorySettings.from_env(), NOT the bare constructor -- every other call site
        # in the codebase uses .from_env() to actually read the runtime environment
        # (provider, API keys, Neo4j URI, model). The bare constructor silently ignores
        # the environment and falls back to dataclass defaults -- found the hard way
        # during Phase 1's first live run: env-var provider/model overrides were
        # silently no-ops and the harness tried to reach a Neo4j that was never
        # configured, exactly the kind of harness/production divergence the fidelity
        # contract exists to catch.
        settings = MemorySettings.from_env()
        graphiti_client = GraphitiClient.from_settings(settings)
        clients = graphiti_client.client.clients

        # Apply model/temperature overrides. Safe to mutate directly (unlike the prompt
        # patch below) because from_settings() built a fresh client for THIS arm alone --
        # no shared global state, so no restore/race concerns. model=None means "use
        # production's resolved default", per the fidelity contract (don't hardcode a
        # model in the harness that could drift from the real default).
        if arm.tuning.model:
            clients.llm_client.config.model = arm.tuning.model
        clients.llm_client.temperature = arm.tuning.temperature

        # Phase 2: candidate lookup against the real backing namespace, independent of
        # context_episode_count. No-op (empty list) unless both the arm opts in AND the
        # request carries a real source_namespace -- synthetic fixtures correctly get no
        # signal here, matching _lookup_known_entities' documented fail-safe behavior.
        known_entities: list[str] = []
        if arm.tuning.forced_known_entities is not None:
            # Ablation override -- bypass the DB lookup entirely (see field docstring).
            known_entities = list(arm.tuning.forced_known_entities)
        elif arm.tuning.enable_candidate_lookup and request.source_namespace:
            known_entities = await _lookup_known_entities(
                clients, request.source_namespace, request.current_message
            )

        # Apply prompt variant + candidate-lookup monkey-patch if needed. prompt_library
        # is process-global shared state (see _apply_extraction_patches docstring) --
        # restore is MANDATORY even on exception, or a later arm/request would silently
        # inherit this arm's variant.
        restore_prompt = _apply_extraction_patches(
            arm.tuning.prompt_variant, known_entities, arm.tuning.retrieved_context
        )

        # Build isolated EpisodicNode for current message
        now = utc_now()
        current_episode = EpisodicNode(
            name="extraction-lab-test",
            group_id="extraction-lab",
            labels=[],
            source=EpisodeType.message,
            content=request.current_message,
            source_description="extraction_lab",
            created_at=now,
            valid_at=now,
        )

        # Build previous episodes list (respecting context_episode_count limit)
        previous_episodes = []
        for episode_fixture in request.previous_episodes[-arm.tuning.context_episode_count:]:
            prev_ep = EpisodicNode(
                name="extraction-lab-previous",
                group_id="extraction-lab",
                labels=[],
                source=EpisodeType.message,
                content=episode_fixture.text,
                source_description="extraction_lab",
                created_at=episode_fixture.created_at,
                valid_at=episode_fixture.created_at,
            )
            previous_episodes.append(prev_ep)

        # Step 1: extract nodes (and, for the fallback candidate, edges in the same
        # typed response). Graphiti 0.29 ships this combined path but does not use it
        # from single-episode add_episode even though its own docstring says it avoids
        # orphaned nodes by letting the model see the proposition and entities together.
        combined_edges = None
        if arm.tuning.prompt_variant == "combined_extraction":
            extracted_nodes, combined_edges, index_map = await extract_combined(
                clients, current_episode, previous_episodes, None, None, None, None, None
            )
        else:
            extracted_nodes, index_map = await extract_nodes(
                clients, current_episode, previous_episodes, None, None, None
            )

        # Step 2: resolve_extracted_nodes
        resolved_nodes, uuid_map, duplicates = await resolve_extracted_nodes(
            clients, extracted_nodes, current_episode, previous_episodes, None,
        )

        # Step 3: extract_edges
        edge_type_map = {("Entity", "Entity"): []}
        extracted_edges = combined_edges
        if extracted_edges is None:
            extracted_edges = await extract_edges(
                clients,
                current_episode,
                extracted_nodes,
                previous_episodes,
                edge_type_map,
                group_id="extraction-lab",
            )

        # Step 4: resolve_extracted_edges
        edges = resolve_edge_pointers(extracted_edges, uuid_map)
        resolved_edges, invalidated_edges, new_edges = await resolve_extracted_edges(
            clients, edges, current_episode, resolved_nodes, {}, edge_type_map,
        )

        # Serialize results
        mentions = [
            {
                "text": node.name,
                "labels": node.labels or [],
                "uuid": str(node.uuid),
            }
            for node in resolved_nodes
        ]

        propositions = [
            {
                "fact": edge.fact,
                "source_uuid": str(edge.source_node_uuid),
                "target_uuid": str(edge.target_node_uuid),
                "uuid": str(edge.uuid),
            }
            for edge in (resolved_edges + new_edges)
        ]

        extraction = ExtractionResult(
            mentions=mentions,
            propositions=propositions,
        )

        # Score against gold
        gold_scores = await _score_extraction(extraction, request.gold, llm_backend=llm_backend)

        # Close client
        await graphiti_client.client.close()

        return ExtractionLabArmResult(
            id=arm.id,
            label=arm.label,
            ok=True,
            enabled=arm.enabled,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            tuning=arm.tuning.model_dump(mode="json"),
            extraction=extraction,
            gold_scores=gold_scores,
            known_entities_used=known_entities,
        )

    except Exception as exc:
        return ExtractionLabArmResult(
            id=arm.id,
            label=arm.label,
            ok=False,
            enabled=arm.enabled,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            tuning=arm.tuning.model_dump(mode="json"),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        # Mandatory even on the success path above (which already returned) -- this
        # covers every exception exit. prompt_library.extract_nodes.extract_message is
        # process-global; leaving a variant patched after this arm fails would silently
        # corrupt every subsequent arm/request until the process restarts.
        if restore_prompt is not None:
            restore_prompt()


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


async def _lookup_known_entities(
    clients: object,
    namespace: str | None,
    message: str,
) -> list[str]:
    """Phase 2's candidate lookup: existing PERSISTENT entity names in `namespace`
    (a real graphiti-core group_id, e.g. "lme-830ce83f") that appear literally in
    `message` -- independent of RELEVANT_SCHEMA_LIMIT / context_episode_count, which
    is the entire point (see the plan's Phase 2 spec). Deliberately the cheapest
    possible signal for a first test: exact case-insensitive substring match, no
    embedding similarity -- the belief-supersession plan's own "Candidate Retrieval"
    philosophy is "intentionally over-include, let the downstream step filter," and
    that downstream filter here is the extractor's own judgment (the KNOWN ENTITIES
    block states a fact, not a command).

    Fails safe to [] on any error, missing namespace, or missing driver -- a failed
    lookup must never crash extraction, and an empty result is exactly the
    "no additional signal" case every other code path here already handles.
    """
    if not namespace:
        return []
    driver = getattr(clients, "driver", None)
    if driver is None:
        return []
    try:
        # graphiti_core.driver.neo4j_driver.Neo4jDriver.execute_query(cypher, **kwargs)
        # pops kwargs["params"] as the query parameter dict and auto-fills database_
        # from the driver's own bound database -- NOT a bare namespace=... kwarg (that
        # would be forwarded straight to the underlying neo4j driver's execute_query,
        # which does not accept arbitrary query params that way).
        result = await driver.execute_query(
            """
            MATCH (n:Entity)
            WHERE n.group_id = $namespace AND n.scope = 'PERSISTENT'
            RETURN DISTINCT n.name AS name
            """,
            params={"namespace": namespace},
        )
        records = result.records
    except Exception:
        logger.warning("Extraction Lab candidate lookup failed for namespace=%s", namespace, exc_info=True)
        return []

    message_lower = message.lower()
    matched: list[str] = []
    for record in records:
        name = str(record.get("name") or "").strip()
        # Skip short/generic names (e.g. "user", "it") -- a 2-3 char substring match
        # against arbitrary message text is noise, not signal, and inflates the
        # unsupported_inference_rate without helping recall.
        if len(name) >= 3 and name.lower() in message_lower:
            matched.append(name)
    return matched




async def run_extraction_lab(
    request: ExtractionLabRequest,
) -> ExtractionLabRunPayload:
    """Run all enabled extraction arms.

    Deliberately SEQUENTIAL, unlike recall_lab.py's run_recall_lab (which fans arms out
    concurrently via asyncio.gather). Recall Lab's arms are safe to run concurrently
    because RetrievalTuningConfig is passed as a plain function argument -- no shared
    mutable state. Extraction arms are not: _apply_extraction_patches patches
    graphiti_core.prompts.prompt_library, a process-global object, for the duration of
    one arm's extraction call. Running arms concurrently would let one arm's patch
    apply while another arm's extraction call is still in flight, silently corrupting
    both results with a race that would not show up as an error -- it would just look
    like a possibly-wrong extraction with no signal that anything went wrong. Sequential
    execution is the only correct mode here; it costs latency (arms x LLM call time
    instead of max(arms)), which is an acceptable tradeoff for a correctness-first lab
    tool that is not on any user-facing request path.

    Builds one shared chat backend for gold-scoring's fuzzy-match tier (see
    _score_extraction), reused across every arm in this run rather than rebuilt per
    arm -- this is scoring infrastructure, not part of the per-arm extraction call, so
    it sits outside the fidelity contract entirely. Falls back to exact-match-only
    scoring (llm_backend=None) if the backend can't be built; never fatal.
    """
    enabled = [arm for arm in request.arms if arm.enabled]

    llm_backend: object | None = None
    try:
        from menhir.config import MemorySettings
        from menhir.infrastructure.providers import build_chat_backend

        llm_backend = build_chat_backend(MemorySettings.from_env())
    except Exception:
        logger.warning(
            "Extraction Lab: could not build a scoring LLM backend; "
            "falling back to exact-match-only gold scoring",
            exc_info=True,
        )

    arm_results = [
        await _run_extraction_arm(arm, request, llm_backend=llm_backend) for arm in enabled
    ]

    return ExtractionLabRunPayload(
        current_message=request.current_message,
        arms=arm_results,
    )
