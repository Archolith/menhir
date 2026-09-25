"""Pydantic schemas for Explorer Recall Lab requests, arms, and judge output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from menhir.domain.recall import QueryPreset
from menhir.domain.retrieval_tuning import RetrievalTuningConfig


class RecallLabTuning(BaseModel):
    """The tuning controls that have an implemented effect in RecallService."""

    model_config = ConfigDict(extra="forbid")

    hybrid_alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    enable_bm25: bool = False
    enable_assertion_shadow: bool = True
    enable_facet_shadow: bool = False
    enable_facet_candidates: bool = False
    facet_weight: float = Field(default=0.5, ge=0.0, le=10.0)
    enable_oracle_ranking: bool = False
    oracle_rank_weight: float = Field(default=0.25, ge=0.0, le=2.0)
    enable_intent_lens: bool = False
    enable_warden_gate: bool = False
    enable_diversity_gate: bool = False
    enable_contradiction_interrupt: bool = False
    enable_belief_gate: bool = False
    enable_evidence_anchor: bool = True
    enable_fact_edges: bool = False
    fact_edge_k: int = Field(default=20, ge=1, le=200)
    fact_edge_mode: Literal["pointer", "standalone"] = "pointer"
    similarity_scale: Literal["rrf", "normalized"] = "rrf"
    enable_content_vector: bool = False
    content_vector_replace_name: bool = False
    content_vector_k: int = Field(default=100, ge=1, le=200)
    content_vector_weight: float = Field(default=0.5, ge=0.0, le=10.0)
    fusion_admission_policy: Literal["attributed", "production_fused"] = "attributed"

    def to_domain(self) -> RetrievalTuningConfig:
        return RetrievalTuningConfig(**self.model_dump())


class RecallLabArm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    tuning: RecallLabTuning = Field(default_factory=RecallLabTuning)


class RecallLabRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    preset: QueryPreset = QueryPreset.KNOWLEDGE
    namespace: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)
    candidate_k: int = Field(default=50, ge=1, le=200)
    include_session: bool = False
    include_superseded: bool = False
    include_invalidated: bool = False
    judge: bool = False
    judge_passes: int = Field(default=2, ge=1, le=2)
    arms: list[RecallLabArm] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def validate_request(self) -> RecallLabRequest:
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("query must not be blank")
        if self.namespace is not None:
            self.namespace = self.namespace.strip() or None
        enabled = [arm for arm in self.arms if arm.enabled]
        if not enabled:
            raise ValueError("at least one arm must be enabled")
        ids = [arm.id for arm in self.arms]
        if len(ids) != len(set(ids)):
            raise ValueError("arm ids must be unique")
        if self.candidate_k < self.limit:
            raise ValueError("candidate_k must be greater than or equal to limit")
        return self


class RecallLabJudgeScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevance: float = Field(ge=0.0, le=5.0)
    completeness: float = Field(ge=0.0, le=5.0)
    ranking: float = Field(ge=0.0, le=5.0)
    noise_control: float = Field(ge=0.0, le=5.0)
    omission_safety: float = Field(ge=0.0, le=5.0)


class RecallLabJudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    winner: str
    ranking: list[str] = Field(min_length=1)
    scores: dict[str, RecallLabJudgeScore]
    serious_omissions: dict[str, list[str]] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
