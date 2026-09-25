"""Read-only, concurrent recall experiments for the Explorer Recall Lab."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from time import perf_counter
from typing import Any

from menhir.explorer.recall_lab_models import (
    RecallLabArm,
    RecallLabJudgeResponse,
    RecallLabJudgeScore,
    RecallLabRequest,
    RecallLabTuning,
)
from menhir.explorer.recall_lab_serialize import _serialize_result

logger = logging.getLogger(__name__)


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
