"""Read-only, concurrent recall experiments for the Explorer Recall Lab."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
from dataclasses import asdict, is_dataclass
from enum import Enum
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from menhir.domain.models import MemoryType, NodeScope
from menhir.domain.recall import QueryPreset
from menhir.domain.retrieval_tuning import CandidateSource, RetrievalScoreKind, RetrievalTuningConfig
from menhir.domain.truth.labels import WardenLabel
from menhir.domain.typed_assertion import ASSERTION_OPERATIONS, EVIDENCE_TIERS, VALUE_KINDS
from menhir.privacy import MASK, redact_text

logger = logging.getLogger(__name__)


class RecallLabTuning(BaseModel):
    """The tuning controls that have an implemented effect in RecallService."""

    model_config = ConfigDict(extra="forbid")

    enable_assertion_shadow: bool = True
    enable_facet_shadow: bool = False
    enable_facet_candidates: bool = False
    facet_weight: float = Field(default=0.5, ge=0.0, le=10.0)
    enable_oracle_ranking: bool = True
    oracle_rank_weight: float = Field(default=0.25, ge=0.0, le=2.0)
    enable_intent_lens: bool = True
    enable_warden_gate: bool = True
    enable_contradiction_interrupt: bool = True
    enable_source_memories: bool = True
    source_memory_limit: int = Field(default=10, ge=1, le=50)
    source_memory_max_chars: int = Field(default=2000, ge=100, le=4000)
    source_memory_spread: bool = True
    scalar_verdict_min_similarity: float = Field(default=0.0, ge=0.0, le=1.0)

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
    arms: list[RecallLabArm] = Field(min_length=1, max_length=12)

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
    # The judge echoes memory text; keep pydantic from copying it into validation errors.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

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
        "id": "e",
        "label": "E · production base + frontier (soft evidence)",
        "enabled": True,
        "tuning": _default_tuning(
            enable_facet_candidates=True,
            facet_weight=0.5,
            enable_oracle_ranking=True,
            enable_intent_lens=True,
            enable_warden_gate=True,
            enable_contradiction_interrupt=False,
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
            enable_oracle_ranking=False,
            enable_intent_lens=False,
            enable_warden_gate=False,
            enable_contradiction_interrupt=False,
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
            enable_warden_gate=False,
            enable_contradiction_interrupt=False,
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
            enable_contradiction_interrupt=False,
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


# Keys known to carry NO memory content: ids, kinds/enums, counts, timestamps, scores, booleans.
# Redaction is an ALLOWLIST over this set: any other key -- including one added to a domain
# dataclass tomorrow -- is masked by default. Free-form slot names (attribute, relation, unit),
# values, quoted spans, sentences, names and fact text are deliberately absent.
#
# A key on the list is not enough on its own: the VALUE must also fit the key's shape, because
# several of these labels are authored by an LLM or a caller rather than by the system. Ids must
# be opaque tokens (synthetic ids that embed slot text are replaced by a salted hash); enum-like
# keys must carry a value from a closed vocabulary (anything else is masked); scores, counts and
# timestamps must have the matching primitive type.
_ID_KEYS: frozenset[str] = frozenset({
    "uuid", "id", "subject_uuid", "view_uuid", "episode_uuid", "assertion_id",
})
_TIME_KEYS: frozenset[str] = frozenset({
    "valid_at", "invalid_at", "created_at", "expired_at", "said_at", "event_at",
    "mentioned_at", "reference_time",
})
_NUMBER_KEYS: frozenset[str] = frozenset({
    "rank", "contributors_total", "contributors_truncated", "next_offset", "has_foundation",
    "lower_bound", "is_current_belief", "corroborated", "is_superseded_view",
    "is_scalar_authority",
    "final_score", "retrieval_score", "similarity", "bm25_rank", "cosine_rank", "content_rank",
    "content_cosine", "cosine", "semantic_similarity", "adjacency_bonus", "recency_bonus",
    "prominence_bonus", "conflict_bonus", "type_boost", "alpha", "beta", "gamma", "delta",
})
# Children of these are walked by the same allowlist; a bare string under one is masked.
_CONTAINER_KEYS: frozenset[str] = frozenset({"contributors", "temporal_facts", "score_parts"})
_VOCAB: dict[str, frozenset[str]] = {
    "scope": frozenset({s.value for s in NodeScope} | {f"NodeScope.{s.name}" for s in NodeScope}),
    "kind": frozenset({"current", "expired", "as_of", "history"}),
    "status": frozenset({"leads", "advisory"}),
    "value_kind": frozenset(VALUE_KINDS),
    "operation": frozenset(ASSERTION_OPERATIONS),
    "evidence_tier": frozenset(EVIDENCE_TIERS),
    "basis": frozenset({"counted"}),
    "event_basis": frozenset({"explicit"}),
    "memory_type": frozenset(
        {m.value for m in MemoryType}
        | {"SCALAR_STATE", "SCALAR_HISTORY", "OBSERVATION", "EPISODIC_PENDING", "Entity"}
    ),
    "source": frozenset({s.value for s in CandidateSource}),
    "contributing_sources": frozenset({s.value for s in CandidateSource}),
    "temporal_role": frozenset({"current_belief", "superseded_belief", "ended"}),
    "retrieval_score_kind": frozenset({k.value for k in RetrievalScoreKind}),
    "warden_label": frozenset({w.value for w in WardenLabel}),
    "preset": frozenset({p.value for p in QueryPreset}),
}
_REDACTION_SAFE_KEYS: frozenset[str] = (
    _ID_KEYS | _TIME_KEYS | _NUMBER_KEYS | _CONTAINER_KEYS | frozenset(_VOCAB)
)
_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HASHED_ID_RE = re.compile(r"h~[0-9a-f]{16}")
_TIME_RE = re.compile(r"[0-9][0-9T:+\-.Z ]{3,39}")
# Per-process secret: ids built from low-entropy slot text (attribute/scope/unit) could be
# recovered from a plain hash by guessing, so the hash is keyed. Stable within a process (the
# page matches baseline rows by id), different across restarts.
_HASH_SALT: bytes = secrets.token_bytes(32)


def _stable_hash(text: str) -> str:
    digest = hmac.new(_HASH_SALT, text.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"h~{digest[:16]}"


def _mask_value(value: Any) -> Any:
    """Mask everything under a non-allowlisted key, recursively; numbers and bools too.

    Dict keys inside masked data are data too, so they are replaced by a stable hash.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            (key if _HASHED_ID_RE.fullmatch(str(key)) else _stable_hash(str(key))): _mask_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mask_value(item) for item in value]
    if isinstance(value, str):
        return value if not value.strip() else MASK
    return MASK


def _safe_id(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return _mask_value(value)
    if not value.strip():
        return value
    if _HASHED_ID_RE.fullmatch(value) or _OPAQUE_ID_RE.fullmatch(value):
        return value
    return _stable_hash(value)


def _safe_vocab(key: str, value: Any) -> Any:
    vocab = _VOCAB[key]
    if value is None:
        return None
    if isinstance(value, str):
        return value if value in vocab else _mask_value(value)
    if isinstance(value, (list, tuple)):
        return [item if isinstance(item, str) and item in vocab else _mask_value(item) for item in value]
    return _mask_value(value)


def _redact_allowlist(obj: Any, *, reveal: bool) -> Any:
    """Allowlist redaction for any structure Recall Lab emits (rows, verdicts, facts, nested).

    Only keys in ``_REDACTION_SAFE_KEYS`` pass through, and only with a value of the right shape
    (opaque id, closed-vocabulary label, number, timestamp); every other value is masked with
    ``MASK`` (strings, numbers, booleans, and everything nested under it). Dicts and lists
    are walked recursively. With ``reveal`` the input is returned untouched.
    """
    if reveal:
        return obj
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if key in _ID_KEYS:
                out[key] = _safe_id(value)
            elif key in _VOCAB:
                out[key] = _safe_vocab(key, value)
            elif key in _NUMBER_KEYS:
                out[key] = value if value is None or isinstance(value, (int, float, bool)) else _mask_value(value)
            elif key in _TIME_KEYS:
                ok = value is None or (isinstance(value, str) and _TIME_RE.fullmatch(value))
                out[key] = value if ok else _mask_value(value)
            elif key in _CONTAINER_KEYS:
                if isinstance(value, dict):
                    out[key] = _redact_allowlist(value, reveal=False)
                elif isinstance(value, (list, tuple)):
                    out[key] = [
                        _redact_allowlist(item, reveal=False) if isinstance(item, dict) else _mask_value(item)
                        for item in value
                    ]
                else:
                    out[key] = _mask_value(value)
            else:
                out[key] = _mask_value(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact_allowlist(item, reveal=False) for item in obj]
    return _mask_value(obj)


_ERROR_CLASS_RE = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?::.*)?", re.DOTALL
)
_ERROR_CLASS_SUFFIXES = ("Error", "Exception", "Exit", "Timeout", "Warning", "Interrupt")
_STATIC_JUDGE_ERRORS = frozenset({
    "Recall Lab judge skipped: fewer than two healthy retrieval arms",
    "Recall Lab judge requires an available runtime LLM",
})

# Keys known to carry no memory content, by level. Everything else is masked (fail closed),
# including keys written by older code (e.g. a stored ``event_authority_layer``).
_WALKED_ARM_KEYS: frozenset[str] = frozenset({"results", "authority_layer", "source_memories"})
_SAFE_ARM_KEYS: frozenset[str] = frozenset({
    "id", "label", "enabled", "ok", "degraded", "elapsed_ms", "tuning", "candidates_evaluated",
    "nodes_touched", "service_ms", "preset", "phases", "warden_status", "warden_notice", "note",
    "search_error", "rank_shadow_warning",
})
_SAFE_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "preset", "namespace", "limit", "candidate_k", "include_session", "include_superseded",
    "include_invalidated", "judge", "judge_passes", "saved", "saved_run_id",
})
_SAFE_JUDGMENT_KEYS: frozenset[str] = frozenset({
    "ok", "skipped", "passes_requested", "passes_completed", "stable", "winner_id",
    "winner_label", "tied_ids", "tie_votes", "winner_votes", "average_scores", "model",
    "elapsed_ms",
})
_SAFE_PASS_KEYS: frozenset[str] = frozenset({"winner_id", "ranking_ids", "scores"})


def _redact_error_text(text: Any) -> Any:
    """Reduce an exception string to its class name; anything else is masked."""
    if not isinstance(text, str) or not text.strip():
        return text
    if text in _STATIC_JUDGE_ERRORS:
        return text
    match = _ERROR_CLASS_RE.fullmatch(text)
    if match and match.group(1).rsplit(".", 1)[-1].endswith(_ERROR_CLASS_SUFFIXES):
        return match.group(1)
    return MASK


def _redact_keys(obj: dict[str, Any], safe: frozenset[str]) -> dict[str, Any]:
    return {k: (v if k in safe else _mask_value(v)) for k, v in obj.items()}


def _redact_judgment(judgment: Any) -> Any:
    if not isinstance(judgment, dict):
        return _mask_value(judgment)
    out = _redact_keys(judgment, _SAFE_JUDGMENT_KEYS | {"error", "passes"})
    if "error" in judgment:
        out["error"] = _redact_error_text(judgment["error"])
    if isinstance(judgment.get("passes"), (list, tuple)):
        out["passes"] = [
            _redact_keys(p, _SAFE_PASS_KEYS) if isinstance(p, dict) else _mask_value(p)
            for p in judgment["passes"]
        ]
    elif "passes" in judgment:
        out["passes"] = _mask_value(judgment["passes"])
    return out


def redact_recall_lab_payload(payload: Any, *, reveal: bool) -> Any:
    """Payload-level redactor for a Recall Lab result (live response or a stored run).

    One code path for every surface: live ``run``, history detail, and stored rows written
    before any fix. Idempotent, so running it over an already-redacted payload changes nothing.
    A viewer without ``reveal`` never receives memory content or query text; with ``reveal``
    the payload is returned untouched.
    """
    if reveal or not isinstance(payload, dict):
        return payload
    out = _redact_keys(payload, _SAFE_PAYLOAD_KEYS | {"query", "arms", "judgment"})
    if "query" in payload:
        out["query"] = redact_text(payload["query"], reveal=False)
    if isinstance(payload.get("arms"), (list, tuple)):
        arms = []
        for arm in payload["arms"]:
            if not isinstance(arm, dict):
                arms.append(_mask_value(arm))
                continue
            a = _redact_keys(
                {k: v for k, v in arm.items() if k != "_judge_results"},
                _SAFE_ARM_KEYS | _WALKED_ARM_KEYS | {"error", "query"},
            )
            for key in _WALKED_ARM_KEYS:
                if key in a:
                    a[key] = _redact_allowlist(a[key], reveal=False)
            if "query" in a:
                a["query"] = redact_text(a["query"], reveal=False)
            if "error" in a:
                a["error"] = _redact_error_text(a["error"])
            arms.append(a)
        out["arms"] = arms
    elif "arms" in payload:
        out["arms"] = _mask_value(payload["arms"])
    if "judgment" in payload:
        out["judgment"] = _redact_judgment(payload["judgment"])
    return out


def redact_recall_lab_request(request_payload: dict[str, Any], *, reveal: bool) -> dict[str, Any]:
    """Request payload as it should be stored: a run written under redaction keeps no raw query."""
    return redact_recall_lab_payload(request_payload, reveal=reveal)


def redact_recall_lab_history_row(row: Any, *, reveal: bool) -> Any:
    """Read-time redaction for a saved-run list row or a history detail row."""
    if reveal or not isinstance(row, dict):
        return row
    out = dict(row)
    if "query" in out:
        out["query"] = redact_text(out["query"], reveal=False)
    if isinstance(out.get("request"), dict):
        out["request"] = redact_recall_lab_payload(out["request"], reveal=False)
    if isinstance(out.get("result"), dict):
        out["result"] = redact_recall_lab_payload(out["result"], reveal=False)
    return out


def _redact_authority_verdicts(verdicts: list[dict[str, Any]], *, reveal: bool) -> list[dict[str, Any]]:
    """Allowlist-redact authority verdicts (see ``_redact_allowlist``)."""
    return _redact_allowlist(verdicts, reveal=reveal)


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
            "temporal_facts": [asdict(t) for t in _value(memory, "temporal_facts", ()) or ()],
        }
        memories.append(_redact_allowlist(row, reveal=reveal))

    # Mirrors api/routes.py's RecallResponse.authority_layer: dataclass
    # field names already match ScalarAuthorityVerdictResponse,
    # so asdict() round-trips cleanly through the same JSON shape /api/recall emits.
    authority_layer = _value(result, "authority_layer")
    # Source-memory lane (default-off): the additive section, serialized next to the
    # ranked results exactly as /api/recall emits it (field names already match
    # SourceMemoryResponse, so asdict round-trips cleanly).
    source_memories = _value(result, "source_memories")

    warden_status = _value(result, "warden_status")
    return {
        "query": str(_value(result, "query", "")),
        "preset": str(_enum_value(_value(result, "preset", ""))),
        "results": memories,
        "candidates_evaluated": int(_value(result, "candidates_evaluated", 0) or 0),
        "nodes_touched": int(_value(result, "nodes_touched", 0) or 0),
        "note": _value(result, "note"),
        "warden_status": asdict(warden_status) if is_dataclass(warden_status) else warden_status,
        "warden_notice": _value(result, "warden_notice"),
        "search_error": _value(result, "search_error"),
        "service_ms": _value(trace, "total_ms"),
        "phases": dict(_value(trace, "phases", {}) or {}),
        "rank_shadow_warning": _value(trace, "rank_shadow_warning"),
        "authority_layer": (
            _redact_authority_verdicts([asdict(v) for v in authority_layer], reveal=reveal)
            if authority_layer else None
        ),
        "source_memories": (
            [
                _redact_allowlist(asdict(sm), reveal=reveal)
                for sm in source_memories
            ]
            if source_memories else None
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
            include_warden_status=True,
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
    return redact_recall_lab_payload(payload, reveal=reveal)
