"""Pydantic request/response models for the Explorer Extraction Lab.

Extracted verbatim from extraction_lab.py; the original module re-exports
every name defined here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
