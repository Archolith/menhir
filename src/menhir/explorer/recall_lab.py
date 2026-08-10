"""Read-only, concurrent recall experiments for the Explorer Recall Lab."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import asdict, is_dataclass
from enum import Enum
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from menhir.domain.recall import QueryPreset
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.privacy import MASK, redact_mapping, redact_text

logger = logging.getLogger(__name__)


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


_JUDGE_SYSTEM_PROMPT = """You are an impartial retrieval-quality judge.
You will compare anonymous ranked memory results for a user query. Judge only the
visible result text and order. Do not infer which retrieval algorithm produced a
result set. A missing expected memory is more damaging than a mildly irrelevant
extra result.

Score every result set from 0 to 5 on:
- relevance: results directly help answer the query
- completeness: the useful aspects of the query are covered
- ranking: the best evidence appears earliest
- noise_control: irrelevant or redundant memories are minimized
- omission_safety: important likely evidence is not conspicuously absent

Every value inside "scores" is a QUALITY SCORE from 0 through 5. In particular,
"ranking" is a 0-5 quality score, not the result set's ordinal position; never
put 6, 7, or 8 in any score field. Use the separate top-level "ranking" array
to order the result-set identifiers.

Return JSON only using the exact dynamic output template supplied with the result
sets. Use "tie" only when the sets are practically equivalent. Include every
result-set identifier exactly once in ranking and scores. Do not include markdown."""


def _judge_prompt(query: str, anonymous_arms: list[tuple[str, dict[str, Any]]]) -> str:
    sections = [f"USER QUERY:\n{query}", "ANONYMOUS RESULT SETS:"]
    for alias, arm in anonymous_arms:
        lines = [f"\n{alias}:"]
        if not arm.get("ok") or arm.get("degraded"):
            lines.append("[RETRIEVAL FAILED OR DEGRADED]")
        results = arm.get("_judge_results") or []
        if not results:
            lines.append("[NO RESULTS]")
        for row in results:
            name = str(row.get("name") or "").strip()
            content = str(row.get("content") or "").strip()
            text = f"{name}: {content}" if content and content != name else (content or name)
            # Keep judging calls bounded while preserving enough context to assess a hit.
            lines.append(f"{int(row.get('rank') or 0)}. {text[:800]}")
        sections.append("\n".join(lines))
    aliases = [alias for alias, _arm in anonymous_arms]
    zero_scores = {
        "relevance": 0,
        "completeness": 0,
        "ranking": 0,
        "noise_control": 0,
        "omission_safety": 0,
    }
    template = {
        "winner": f"one of: {', '.join(aliases)}, or tie",
        "ranking": aliases,
        "scores": {alias: dict(zero_scores) for alias in aliases},
        "serious_omissions": {alias: [] for alias in aliases},
        "reason": "brief concrete comparison",
    }
    sections.extend([
        "REQUIRED IDENTIFIERS:\n" + ", ".join(aliases),
        "EXACT OUTPUT TEMPLATE (replace values, preserve every key):\n"
        + json.dumps(template, indent=2),
    ])
    return "\n\n".join(sections)


def _parse_judge_json(raw: str) -> RecallLabJudgeResponse:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge did not return a JSON object")
    return RecallLabJudgeResponse.model_validate(json.loads(cleaned[start:end + 1]))


async def _judge_pass(
    llm: object,
    query: str,
    arms: list[dict[str, Any]],
) -> dict[str, Any]:
    backend = getattr(llm, "backend", None)
    if backend is None:
        raise RuntimeError("configured judge LLM is unavailable")

    shuffled = list(arms)
    random.SystemRandom().shuffle(shuffled)
    anonymous = [(f"R{index}", arm) for index, arm in enumerate(shuffled, start=1)]
    alias_to_id = {alias: str(arm["id"]) for alias, arm in anonymous}
    raw = await backend.create_chat_completion(
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        user_prompt=_judge_prompt(query, anonymous),
        operation="recall_lab_judge",
        max_tokens=1400,
        temperature=0.0,
    )
    parsed = _parse_judge_json(raw)
    expected = set(alias_to_id)
    if set(parsed.ranking) != expected or set(parsed.scores) != expected:
        raise ValueError("judge response did not score and rank every anonymous arm exactly once")
    if parsed.winner != "tie" and parsed.winner not in expected:
        raise ValueError("judge winner was not an anonymous arm or tie")

    return {
        "winner_id": alias_to_id.get(parsed.winner),
        "ranking_ids": [alias_to_id[alias] for alias in parsed.ranking],
        "scores": {
            alias_to_id[alias]: score.model_dump(mode="json")
            for alias, score in parsed.scores.items()
        },
        "serious_omissions": {
            alias_to_id[alias]: omissions
            for alias, omissions in parsed.serious_omissions.items()
            if alias in alias_to_id
        },
        "reason": parsed.reason,
    }


async def judge_recall_lab(
    llm: object,
    query: str,
    arms: list[dict[str, Any]],
    *,
    passes: int,
) -> dict[str, Any]:
    """Run blinded judging passes and aggregate their arm-id-level verdicts."""

    started = perf_counter()
    try:
        judgments = [await _judge_pass(llm, query, arms) for _ in range(passes)]
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "passes_requested": passes,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }

    arm_ids = [str(arm["id"]) for arm in arms]
    winner_votes = {arm_id: 0 for arm_id in arm_ids}
    tie_votes = 0
    score_totals = {
        arm_id: {field: 0.0 for field in RecallLabJudgeScore.model_fields}
        for arm_id in arm_ids
    }
    for judgment in judgments:
        winner_id = judgment["winner_id"]
        if winner_id is None:
            tie_votes += 1
        else:
            winner_votes[winner_id] += 1
        for arm_id, scores in judgment["scores"].items():
            for field, value in scores.items():
                score_totals[arm_id][field] += float(value)

    max_votes = max(winner_votes.values(), default=0)
    vote_leaders = [arm_id for arm_id, votes in winner_votes.items() if votes == max_votes and votes > 0]
    winner_id = (
        vote_leaders[0]
        if len(vote_leaders) == 1 and max_votes > len(judgments) / 2
        else None
    )
    tied_ids = [] if winner_id else (vote_leaders if len(vote_leaders) > 1 else arm_ids)
    labels = {str(arm["id"]): str(arm["label"]) for arm in arms}
    return {
        "ok": True,
        "model": str(getattr(llm, "chat_model", "") or "configured chat model"),
        "passes_requested": passes,
        "passes_completed": len(judgments),
        "stable": len({judgment["winner_id"] for judgment in judgments}) == 1,
        "winner_id": winner_id,
        "winner_label": labels.get(winner_id) if winner_id else None,
        "tied_ids": tied_ids,
        "tie_votes": tie_votes,
        "winner_votes": winner_votes,
        "average_scores": {
            arm_id: {
                field: round(total / len(judgments), 2)
                for field, total in fields.items()
            }
            for arm_id, fields in score_totals.items()
        },
        "passes": judgments,
        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
    }


def _default_tuning(**overrides: Any) -> dict[str, Any]:
    values = RecallLabTuning().model_dump()
    values.update(overrides)
    return values


DEFAULT_ARMS: list[dict[str, Any]] = [
    {
        "id": "production",
        "label": "Production",
        "enabled": True,
        "tuning": _default_tuning(),
    },
    {
        "id": "a",
        "label": "A · attributed control",
        "enabled": True,
        "tuning": _default_tuning(enable_bm25=True, fusion_admission_policy="production_fused"),
    },
    {
        "id": "b",
        "label": "B · add content vector",
        "enabled": True,
        "tuning": _default_tuning(
            enable_bm25=True,
            enable_content_vector=True,
            fusion_admission_policy="production_fused",
        ),
    },
    {
        "id": "c",
        "label": "C · replace name vector",
        "enabled": True,
        "tuning": _default_tuning(
            enable_bm25=True,
            enable_content_vector=True,
            content_vector_replace_name=True,
            fusion_admission_policy="production_fused",
        ),
    },
    {
        "id": "d",
        "label": "D · active facet + warden + oracle",
        "enabled": True,
        "tuning": _default_tuning(
            enable_facet_candidates=True,
            facet_weight=0.5,
            enable_oracle_ranking=True,
            enable_intent_lens=True,
            enable_warden_gate=True,
        ),
    },
    {
        "id": "e",
        "label": "E · production base + frontier (soft evidence)",
        "enabled": True,
        "tuning": _default_tuning(
            enable_facet_candidates=True,
            facet_weight=0.5,
            enable_oracle_ranking=True,
            enable_intent_lens=True,
            enable_warden_gate=True,
            enable_evidence_anchor=False,
        ),
    },
    {
        "id": "f",
        "label": "F · production base + facet ranking only",
        "enabled": True,
        "tuning": _default_tuning(
            enable_assertion_shadow=False,
            enable_facet_candidates=True,
            facet_weight=0.5,
        ),
    },
    {
        "id": "g",
        "label": "G · production base + oracle ranking only",
        "enabled": True,
        "tuning": _default_tuning(
            enable_assertion_shadow=False,
            enable_oracle_ranking=True,
            enable_intent_lens=True,
        ),
    },
    {
        "id": "h",
        "label": "H · E frontier without warden",
        "enabled": True,
        "tuning": _default_tuning(
            enable_facet_candidates=True,
            facet_weight=0.5,
            enable_oracle_ranking=True,
            enable_intent_lens=True,
            enable_warden_gate=False,
            enable_evidence_anchor=False,
        ),
    },
]


def _value(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _mapping(value: object | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if is_dataclass(value):
        raw = asdict(value)
    elif isinstance(value, dict):
        raw = dict(value)
    else:
        return None
    return {key: _enum_value(item) for key, item in raw.items()}


def _trace_row(trace: object | None, uuid: str) -> object | None:
    for row in _value(trace, "candidates", []) or []:
        if str(_value(row, "uuid", "")) == uuid:
            return row
    return None


def _redact_authority_verdicts(verdicts: list[dict[str, Any]], *, reveal: bool) -> list[dict[str, Any]]:
    """Mask the free-text fields ScalarAuthorityVerdict/EventAuthorityVerdict carry.

    ``value`` and each contributor's ``stated_span`` (a quoted evidence excerpt) are memory
    content, not structural metadata -- neither is in privacy.REDACTED_FIELDS (that vocabulary
    predates this authority-layer shape), so redact_mapping's flat field-name match can't cover
    them. Everything else (kind/status/subject_uuid/attribute/scope/valid_at/view_uuid/
    has_foundation/predicate/object_key/...) is structural and passes through unchanged.

    ``value`` is masked unconditionally (not via redact_text) because it is often non-string
    (a spend amount, a duration, a count) -- redact_text only masks strings, so routing a
    numeric authority value through it would silently leak the exact figure in redacted mode.
    """
    if reveal:
        return verdicts
    out: list[dict[str, Any]] = []
    for verdict in verdicts:
        v = dict(verdict)
        if "value" in v and v["value"] not in (None, ""):
            v["value"] = MASK
        if "object_display" in v:
            v["object_display"] = redact_text(v["object_display"], reveal=False)
        # EventAuthorityVerdict carries its own top-level stated_span (no contributors list);
        # ScalarAuthorityVerdict's is nested per-contributor below. Cover both shapes.
        if "stated_span" in v:
            v["stated_span"] = redact_text(v["stated_span"], reveal=False)
        contributors = v.get("contributors")
        # asdict() preserves tuple-typed fields as tuples, not lists (ScalarAuthorityVerdict
        # declares contributors: tuple[...]) -- check both, and always emit a list back out.
        if isinstance(contributors, (list, tuple)):
            v["contributors"] = [
                {**c, "stated_span": redact_text(c.get("stated_span"), reveal=False)}
                if isinstance(c, dict) else c
                for c in contributors
            ]
        out.append(v)
    return out


def _redact_temporal_facts(facts: list[dict[str, Any]], *, reveal: bool) -> list[dict[str, Any]]:
    """Mask ``fact`` (the free-text bi-temporal fact string) on each TemporalFact dict.

    Same gap as authority_layer: ``fact`` is memory content, not in privacy.REDACTED_FIELDS
    (that vocabulary predates this shape), so redact_mapping's flat field-name match can't
    reach it. valid_at/invalid_at/created_at/expired_at/is_current_belief/temporal_role are
    structural (dates and an enum-like role label) and pass through unchanged.
    """
    if reveal:
        return facts
    out: list[dict[str, Any]] = []
    for fact in facts:
        f = dict(fact)
        if "fact" in f:
            f["fact"] = redact_text(f["fact"], reveal=False)
        out.append(f)
    return out


def _serialize_result(result: object, *, reveal: bool) -> dict[str, Any]:
    trace = _value(result, "trace")
    memories: list[dict[str, Any]] = []
    for rank, memory in enumerate(_value(result, "results", []) or [], start=1):
        uuid = str(_value(memory, "uuid", ""))
        candidate = _trace_row(trace, uuid)
        source = _enum_value(_value(candidate, "source"))
        contributors = [
            str(_enum_value(item))
            for item in sorted(
                _value(candidate, "contributing_sources", frozenset()) or [],
                key=lambda item: str(_enum_value(item)),
            )
        ]
        row = {
            "rank": rank,
            "uuid": uuid,
            "name": str(_value(memory, "name", "")),
            "content": _value(memory, "content"),
            "scope": str(_value(memory, "scope", "")),
            "memory_type": str(_value(memory, "memory_type", "")),
            "final_score": float(_value(memory, "final_score", 0.0) or 0.0),
            "retrieval_score": _value(memory, "retrieval_score"),
            "retrieval_score_kind": str(
                _enum_value(_value(memory, "retrieval_score_kind", "graphiti_rrf"))
            ),
            "source": source,
            "contributing_sources": contributors,
            "similarity": _value(candidate, "similarity"),
            "bm25_rank": _value(candidate, "bm25_rank"),
            "cosine_rank": _value(candidate, "cosine_rank"),
            "content_rank": _value(candidate, "content_rank"),
            "content_cosine": _value(candidate, "content_cosine"),
            "warden_label": _enum_value(_value(memory, "warden_label")),
            "score_parts": _mapping(_value(candidate, "score_parts")),
            # Recall Lab arms bypass the /api/recall route's authority formatting; surface these
            # two flags directly off ScoredMemory so a caller can reconstruct the same
            # [AUTHORITATIVE CURRENT MEMORY] / [SUPERSEDED ... MEMORY] labeling the production
            # endpoint provides (needed for apples-to-apples benchmark comparisons).
            "is_superseded_view": bool(_value(memory, "is_superseded_view", False)),
            "is_scalar_authority": bool(_value(memory, "is_scalar_authority", False)),
            # Also dropped like authority_layer: /api/recall (routes.py) includes each
            # memory's temporal_facts (world-time/belief-time/supersession evidence);
            # Recall Lab silently omitted it, so any "previously vs now" / supersession
            # reasoning the answer prompt depends on this for was uniformly unavailable
            # across every Recall Lab arm regardless of tuning.
            "temporal_facts": _redact_temporal_facts(
                [asdict(t) for t in _value(memory, "temporal_facts", ()) or ()],
                reveal=reveal,
            ),
        }
        memories.append(redact_mapping(row, reveal=reveal))

    # Mirrors api/routes.py's RecallResponse.authority_layer/event_authority_layer: dataclass
    # field names already match ScalarAuthorityVerdictResponse/EventAuthorityVerdictResponse,
    # so asdict() round-trips cleanly through the same JSON shape /api/recall emits.
    authority_layer = _value(result, "authority_layer")
    event_authority_layer = _value(result, "event_authority_layer")

    return {
        "query": str(_value(result, "query", "")),
        "preset": str(_enum_value(_value(result, "preset", ""))),
        "results": memories,
        "candidates_evaluated": int(_value(result, "candidates_evaluated", 0) or 0),
        "nodes_touched": int(_value(result, "nodes_touched", 0) or 0),
        "note": _value(result, "note"),
        "search_error": _value(result, "search_error"),
        "service_ms": _value(trace, "total_ms"),
        "phases": dict(_value(trace, "phases", {}) or {}),
        "rank_shadow_warning": _value(trace, "rank_shadow_warning"),
        "authority_layer": (
            _redact_authority_verdicts([asdict(v) for v in authority_layer], reveal=reveal)
            if authority_layer else None
        ),
        "event_authority_layer": (
            _redact_authority_verdicts([asdict(v) for v in event_authority_layer], reveal=reveal)
            if event_authority_layer else None
        ),
    }


async def _run_arm(
    recall_service: object,
    request: RecallLabRequest,
    arm: RecallLabArm,
    *,
    reveal: bool,
) -> dict[str, Any]:
    started = perf_counter()
    try:
        result = await recall_service.recall(
            request.query,
            preset=request.preset,
            limit=request.limit,
            candidate_k=request.candidate_k,
            include_session=request.include_session,
            include_superseded=request.include_superseded,
            include_invalidated=request.include_invalidated,
            namespace=request.namespace,
            tuning=arm.tuning.to_domain(),
            trace=True,
            update_access=False,
        )
    except Exception as exc:
        return {
            "id": arm.id,
            "label": arm.label,
            "ok": False,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "tuning": arm.tuning.model_dump(mode="json"),
            "error": f"{type(exc).__name__}: {exc}",
        }

    payload = _serialize_result(result, reveal=reveal)
    judge_payload = _serialize_result(result, reveal=True)
    return {
        "id": arm.id,
        "label": arm.label,
        "ok": True,
        "degraded": payload["search_error"] is not None,
        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        "tuning": arm.tuning.model_dump(mode="json"),
        "_judge_results": judge_payload["results"],
        **payload,
    }


async def run_recall_lab(
    recall_service: object,
    request: RecallLabRequest,
    *,
    reveal: bool,
    judge_llm: object | None = None,
) -> dict[str, Any]:
    """Run all enabled arms concurrently without mutating recall/access state."""

    enabled = [arm for arm in request.arms if arm.enabled]
    arms = await asyncio.gather(
        *(_run_arm(recall_service, request, arm, reveal=reveal) for arm in enabled)
    )
    judgment = None
    if request.judge:
        healthy_arms = [
            arm for arm in arms
            if arm.get("ok") and not arm.get("degraded")
        ]
        if len(healthy_arms) < 2:
            failures = [
                {
                    "id": arm.get("id"),
                    "ok": arm.get("ok"),
                    "degraded": arm.get("degraded", False),
                    "error": arm.get("error") or arm.get("search_error"),
                }
                for arm in arms
                if arm not in healthy_arms
            ]
            logger.warning(
                "Recall Lab judge skipped for query=%r: fewer than two healthy arms; failures=%s",
                request.query,
                failures,
            )
            judgment = {
                "ok": False,
                "skipped": True,
                "error": "Recall Lab judge skipped: fewer than two healthy retrieval arms",
                "passes_requested": request.judge_passes,
            }
        elif judge_llm is None:
            judgment = {
                "ok": False,
                "error": "Recall Lab judge requires an available runtime LLM",
                "passes_requested": request.judge_passes,
            }
        else:
            judgment = await judge_recall_lab(
                judge_llm,
                request.query,
                healthy_arms,
                passes=request.judge_passes,
            )
    for arm in arms:
        arm.pop("_judge_results", None)
    payload = {
        "query": request.query,
        "preset": request.preset.value,
        "namespace": request.namespace,
        "limit": request.limit,
        "candidate_k": request.candidate_k,
        "arms": arms,
    }
    if judgment is not None:
        payload["judgment"] = judgment
    return payload
