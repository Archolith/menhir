"""Report-only retire category judge: nominate, gather evidence, judge, report (retire plan, step 3).

Safety property: this module writes NOTHING to Neo4j and nothing to pending_actions. Every query
here is a read, none goes through recall (so `last_accessed` is never touched), and the only
outputs are the returned report dict and the usual LLM usage telemetry. Nomination applies the
same guards `retire_memory` refuses on, so a nominated memory is one `retire_memory` would accept.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from menhir.domain.namespace import normalize_namespace, tenant_scope_cypher, tenant_scope_params
from menhir.domain.recall_visibility import memory_retired_cypher
from menhir.domain.retention import destructive_retention_allowed_cypher
from menhir.domain.retire_judge import (
    JUDGE_SYSTEM_PROMPT,
    MUST_NOT_KEEP,
    Verdict,
    build_judge_prompt,
    parse_judge_response,
    vote,
)
from menhir.domain.secret_scrub import detect_secrets
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.infrastructure.cypher import memory_recency_cypher, non_derived_view_cypher
from menhir.infrastructure.erasure_fence import marker_absent_cypher
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.observability import last_provider_quota_exhaustion
from menhir.infrastructure.tool_event_repository import ToolEventRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository

logger = logging.getLogger(__name__)

Execute = Callable[..., list[dict[str, Any]]]
Chat = Callable[[str, str], str]

#: guarded memories read per nomination run, least recently used first.
SCAN_CAP = 5000
EPISODE_CLIP = 500
MAX_EPISODES = 5
MAX_FACTS = 5
PREVIEW_CHARS = 160
_PROVENANCE_CHUNK = 200
_TEXT_FIELDS = ("content", "summary", "name")
WITHHELD = "[withheld: holds a credential]"


def _withhold(text: str) -> str:
    """The text, or WITHHELD when it holds any recognised credential.

    Whole-text withholding, not partial redaction: a detector hit can cover only part of the
    secret (a classic GitHub token reports just its prefix), so replacing the hit can leak the rest.
    """
    return WITHHELD if text and detect_secrets(text) else text


class _ExecuteAdapter:
    """Gives a bare `execute(query, params)` the `.execute(query, params=...)` shape the
    repositories expect."""

    def __init__(self, execute: Execute) -> None:
        self._execute = execute

    def execute(self, query: str, params: dict[str, Any] | None = None, **_: Any) -> list[dict[str, Any]]:
        return self._execute(query, params or {})


def _retire_guards(v: str) -> str:
    """The conditions `retire_memory` refuses on, for one node."""
    return f"""
    ( {non_derived_view_cypher(v)}
      AND {non_structural_memory_cypher(v)}
      AND {destructive_retention_allowed_cypher(v)}
      AND ({v}['erased_at'] IS NULL
           AND coalesce({v}.processing_substage, '') <> 'deleted_by_user'
           AND {marker_absent_cypher(v)})
      AND NOT coalesce({v}.user_flagged, false)
      AND coalesce({v}.scope, 'PERSISTENT') <> 'CANDIDATE'
      AND NOT ({v}:Episodic AND coalesce({v}.processing_state, '') IN ['PENDING', 'ENRICHING'])
    )"""


def _nomination_query() -> str:
    # retire_memory marks and guards both halves of an episode pair, so nomination does too.
    return f"""
    MATCH (n)
    WHERE (n:Entity OR n:Episodic)
      AND {tenant_scope_cypher("n")}
      AND NOT {memory_retired_cypher("n")}
      AND {_retire_guards("n")}
    {MemoryQueryRepository._EPISODE_PAIR_MATCH}
    WITH n, nodes
    WHERE all(x IN nodes WHERE {_retire_guards("x")})
    RETURN n.uuid AS uuid, [x IN nodes | x.uuid] AS pair_uuids, labels(n) AS labels, n.type AS type,
           n.content AS content, n.summary AS summary, n.name AS name,
           toString(n.created_at) AS created_at,
           ({memory_recency_cypher("n")} < datetime() - duration({{days: $idle_days}})
            AND coalesce(toInteger(n.edge_count), 0) < $max_edge_count) AS idle,
           CASE WHEN n.target_date IS NOT NULL AND date(n.target_date) < date()
                THEN true ELSE false END AS target_date_passed
    ORDER BY {memory_recency_cypher("n")} ASC, n.uuid
    LIMIT $scan_cap
    """


NOMINATION_QUERY = _nomination_query()


def _memory_type(row: dict[str, Any]) -> str:
    if row.get("type"):
        return str(row["type"])
    labels = [label for label in (row.get("labels") or []) if label not in ("Entity", "Episodic")]
    return str(labels[0]) if labels else ("Episodic" if "Episodic" in (row.get("labels") or []) else "Entity")


def _text(row: dict[str, Any]) -> str:
    for name in _TEXT_FIELDS:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def nominate(
    execute: Execute, namespace: str | None, *, idle_days: int = 90, max_edge_count: int = 3,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Candidates with at least one signal, most signals first then least recently used.

    One namespace-scoped read for the guarded universe (idle and target_date_passed are computed
    in it); scalar_replaced, stale_anchor and secret_hit are then attached to the rows it returned.
    """
    ns = normalize_namespace(namespace)
    rows = execute(
        NOMINATION_QUERY,
        {**tenant_scope_params(ns), "idle_days": int(idle_days),
         "max_edge_count": int(max_edge_count), "scan_cap": SCAN_CAP},
    )
    candidates: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    seen_pairs: set[str] = set()
    for row in rows:
        uuid = str(row["uuid"])
        pair = {str(u) for u in (row.get("pair_uuids") or [uuid])}
        if pair & seen_pairs:
            continue  # the other half of an episode pair already stands for this memory
        seen_pairs |= pair
        is_entity = "Entity" in (row.get("labels") or [])
        signals: list[str] = []
        if row.get("idle"):
            signals.append("idle")
        if row.get("target_date_passed"):
            signals.append("target_date_passed")
        secret_kinds: set[str] = set()
        for name in _TEXT_FIELDS:
            value = row.get(name)
            if isinstance(value, str) and value:
                secret_kinds.update(hit.kind for hit in detect_secrets(value))
        if secret_kinds:
            signals.append("secret_hit")
        candidates[uuid] = {
            "uuid": uuid,
            "label": "Entity" if is_entity else "Episodic",
            "memory_type": _memory_type(row),
            "created_at": row.get("created_at"),
            "content": _text(row),
            "signals": signals,
            "secret_kinds": sorted(secret_kinds),
            "scalar": None,
            "anchor": None,
        }
        order.append(uuid)

    adapter = _ExecuteAdapter(execute)
    entity_uuids = [u for u in order if candidates[u]["label"] == "Entity"]
    typed = TypedAssertionRepository(adapter)
    for i in range(0, len(entity_uuids), _PROVENANCE_CHUNK):
        chunk = entity_uuids[i:i + _PROVENANCE_CHUNK]
        for prov in typed.suppressible_provenance(namespace=ns, candidate_uuids=chunk):
            cand = candidates.get(str(prov.get("candidate_uuid")))
            if cand is None or cand["scalar"] is not None:
                continue
            if prov.get("stale_value") == prov.get("view_value"):
                continue
            cand["scalar"] = prov
            cand["signals"].append("scalar_replaced")
    for stale in ToolEventRepository(adapter).stale_anchored_memories(namespace=ns, limit=1000):
        cand = candidates.get(str(stale.get("memory_uuid")))
        if cand is None or cand["anchor"] is not None:
            continue
        cand["anchor"] = stale
        cand["signals"].append("stale_anchor")

    position = {u: i for i, u in enumerate(order)}
    nominated = [candidates[u] for u in order if candidates[u]["signals"]]
    nominated.sort(key=lambda c: (-len(c["signals"]), position[c["uuid"]]))
    return nominated[: max(0, int(limit))]


def _clip(text: object, limit: int = EPISODE_CLIP) -> str:
    return str(text or "")[:limit]


def gather_evidence(candidates: list[dict[str, Any]], queries: Any) -> None:
    """Fill each LLM-bound candidate's `evidence` with handle items (E1..En) and `refs`.

    `queries` is a MemoryQueryRepository (fetch_node_receipts, fetch_temporal_facts); both are
    plain reads. Secret-hit candidates are skipped: they are never sent to a model.
    """
    for cand in candidates:
        cand["evidence"] = []
        cand["refs"] = {}
        if "secret_hit" in cand["signals"]:
            continue

        def add(label: str, text: str, ref: str, cand: dict[str, Any] = cand) -> None:
            handle = f"E{len(cand['evidence']) + 1}"
            cand["evidence"].append({"handle": handle, "label": label, "text": text})
            cand["refs"][handle] = ref

        scalar = cand.get("scalar")
        if scalar:
            add(
                "replacing_value",
                f"Later value for {scalar.get('attribute')}: {scalar.get('view_value')} "
                f"(valid {scalar.get('view_valid_at')}). This memory's source recorded "
                f"{scalar.get('stale_value')} (valid {scalar.get('stale_valid_at')}).",
                f"scalar:{scalar.get('subject_uuid')}/{scalar.get('attribute')}",
            )
        anchor = cand.get("anchor")
        if anchor:
            add(
                "anchor",
                f"Anchored file {anchor.get('path')} changed ({anchor.get('operation') or 'unknown'}) "
                f"at {anchor.get('dirty_at')}, after the memory was anchored at "
                f"{anchor.get('anchored_at')}.",
                f"anchor:{anchor.get('project') or ''}:{anchor.get('path')}",
            )
        if cand["label"] != "Entity":
            continue
        receipts = queries.fetch_node_receipts(cand["uuid"]) or {}
        for ep in list(receipts.get("episodes") or [])[:MAX_EPISODES]:
            add("source_episode", _clip(ep.get("content")), str(ep.get("uuid") or ""))
        facts = queries.fetch_temporal_facts([cand["uuid"]]) or []
        for fact in facts[:MAX_FACTS]:
            add(
                "temporal_fact",
                f"{_clip(fact.get('fact'))} (valid_at={fact.get('valid_at')}, "
                f"invalid_at={fact.get('invalid_at')}, expired_at={fact.get('expired_at')})",
                f"fact:{cand['uuid']}",
            )


def _verdict_keep(reason: str) -> Verdict:
    return Verdict(category="keep", reason=reason)


def _quota_exhausted() -> bool:
    quota = last_provider_quota_exhaustion()
    return quota is not None and quota.kind == "chat"


def judge(
    candidates: list[dict[str, Any]], chat: Chat | None, *, k: int = 3, call_budget: int = 30,
) -> dict[str, Any]:
    """Judge each candidate; returns {"verdicts": {uuid: Verdict}, "llm_calls", "stop_reason"}.

    A memory is judged only when all k calls fit the budget (never a partial k). A call that
    raises is an abstaining sample; if the provider then reports chat quota exhaustion the run
    stops and the rest are left `keep`.
    """
    verdicts: dict[str, Verdict] = {}
    calls_used = 0
    stop_reason = ""
    for cand in candidates:
        uuid = cand["uuid"]
        if "secret_hit" in cand["signals"]:
            kinds = ",".join(cand.get("secret_kinds") or [])
            verdicts[uuid] = Verdict(category=MUST_NOT_KEEP, reason=f"secret_hit:{kinds}")
            continue
        if stop_reason:
            verdicts[uuid] = _verdict_keep("not_judged")
            continue
        if chat is None:
            verdicts[uuid] = _verdict_keep("no_llm")
            continue
        if calls_used + k > call_budget:
            verdicts[uuid] = _verdict_keep("budget")
            continue
        redacted = {
            **cand,
            "content": _withhold(cand["content"]),
            "evidence": [{**ev, "text": _withhold(ev["text"])} for ev in cand.get("evidence", [])],
        }
        prompt = build_judge_prompt(redacted)
        valid = {ev["handle"] for ev in cand.get("evidence", [])}
        samples: list[dict[str, Any] | None] = []
        for _ in range(k):
            calls_used += 1
            try:
                samples.append(parse_judge_response(chat(JUDGE_SYSTEM_PROMPT, prompt), valid))
            except Exception as exc:
                logger.warning("retire judge call failed: %s", type(exc).__name__)
                samples.append(None)
                if _quota_exhausted():
                    stop_reason = "provider_quota_exhausted"
                    break
        if stop_reason:
            verdicts[uuid] = _verdict_keep("provider_quota_exhausted")
            continue
        verdicts[uuid] = vote(samples, k=k, memory_type=cand["memory_type"])
    return {"verdicts": verdicts, "llm_calls": calls_used, "stop_reason": stop_reason}


def build_report(
    namespace: str | None, candidates: list[dict[str, Any]], judged: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON-safe report. Never carries a secret value: previews and reasons holding one are withheld."""
    ns = normalize_namespace(namespace)
    verdicts: dict[str, Verdict] = (judged or {}).get("verdicts", {})
    items: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    for cand in candidates:
        verdict = verdicts.get(cand["uuid"])
        refs = cand.get("refs", {})
        item: dict[str, Any] = {
            "uuid": cand["uuid"],
            "type": cand["memory_type"],
            "created_at": cand.get("created_at"),
            "signals": list(cand["signals"]),
            "preview": _withhold(cand["content"])[:PREVIEW_CHARS],
        }
        if verdict is not None:
            item.update({
                "verdict": verdict.category,
                "vote": verdict.vote,
                "evidence": {h: refs.get(h, "") for h in verdict.evidence},
                "kind": verdict.kind,
                "reason": _withhold(verdict.reason),
            })
            if verdict.veto_reason:
                item["veto_reason"] = verdict.veto_reason
            totals[verdict.category] += 1
        items.append(item)
    return {
        "namespace": ns,
        "items": items,
        "totals": dict(sorted(totals.items())),
        "llm_calls": int((judged or {}).get("llm_calls", 0)),
        "stop_reason": str((judged or {}).get("stop_reason", "")),
    }
