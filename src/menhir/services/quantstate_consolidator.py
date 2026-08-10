"""QuantState consolidation pass — LLM perception -> deterministic fold -> in-graph counters.

The write-time step behind the QuantState primitive. Reads a namespace's episodes, has an LLM
emit COUNTABLE EVENTS (crisp for agent/code memory: failures, retries, recurring errors, fixes),
folds them deterministically (no LLM arithmetic), and writes one supersedable counter node per
(subject, counter) via graph_adapter.record_counter — provenance-linked to the contributing
episodes.

Decoupled from any specific LLM: the caller injects `llm_complete(system, user) -> str`. In
benchmark mode the scheduler is off, so this is invoked explicitly (like promote / backfill).
The design law: LLM does perception (language -> events), we do the arithmetic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol

LlmComplete = Callable[[str, str], str]
Embed = Callable[[str], list[float]]

SYSTEM_PROMPT = (
 "You maintain a coding agent's EXPERIENTIAL COUNTERS from its work log. Read the numbered log "
 "episodes and emit counter events for recurring, actionable agent/code events: failed attempts, "
 "neutral/negative measured results, repeated errors, retries, regressions, and repeated SUCCESSFUL "
 "fixes. Each event is an object: {\"episode\": <the number>, \"subject\": <who/what, usually 'self' "
 "or the project>, \"counter\": <stable snake_case key>, \"op\": \"delta\"|\"set\", \"value\": "
 "<number, usually +1>, \"evidence\": <short quote>}. "
 "CRITICAL: canonicalize semantically-equivalent outcomes to the SAME counter key. E.g. every "
 "read-time ranking/reranking/brief experiment that measured neutral-or-negative vs baseline shares "
 "ONE counter like 'read_time_intervention_failed' (do NOT invent a new key per experiment). Group "
 "correctness bug-fixes that worked under one key like 'correctness_fix_succeeded'. "
 "Output ONLY a JSON array of events.")


class GraphAdapter(Protocol):
    def record_counter(self, **kwargs: Any) -> dict[str, Any]: ...


def _retrieval_text(subject: str, counter: str, value: float) -> str:
    human = counter.strip().replace("_", " ")
    v = str(int(value)) if float(value) == int(value) else str(value)
    return f"how many times {subject.strip()} {human}: {v} ({human} count is {v})"


@dataclass(frozen=True)
class Episode:
    uuid: str
    content: str


def _parse_json_array(text: str) -> list[dict]:
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.M).strip()
    # LLMs emit explicit-positive numbers ("value": +1), which are invalid JSON (no leading +).
    # Strip a leading + on numeric literals so the whole array doesn't fail to parse.
    text = re.sub(r'(:\s*)\+(\d)', r'\1\2', text)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def extract_events(episodes: list[Episode], llm_complete: LlmComplete) -> list[dict]:
    """One batched LLM call; each event is attributed to its source episode by number."""
    if not episodes:
        return []
    log = "\n".join(f"[{i}] {e.content}" for i, e in enumerate(episodes))
    raw = _parse_json_array(llm_complete(SYSTEM_PROMPT, log))
    out = []
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        try:
            idx = int(ev.get("episode"))
        except (TypeError, ValueError):
            idx = None
        ev["_episode_uuid"] = episodes[idx].uuid if idx is not None and 0 <= idx < len(episodes) else None
        out.append(ev)
    return out


def fold(events: list[dict]) -> dict[tuple[str, str], dict]:
    """Deterministic fold per (subject, counter): 'set' resets, 'delta' accumulates. Collects the
    contributing episode uuids for provenance. Returns {(subject,counter): {value, episodes}}."""
    state: dict[tuple[str, str], dict] = {}
    for ev in events:
        subject = str(ev.get("subject") or "self").strip().lower()
        counter = str(ev.get("counter") or "").strip().lower()
        if not counter:
            continue
        try:
            value = float(ev.get("value"))
        except (TypeError, ValueError):
            continue
        key = (subject, counter)
        cell = state.setdefault(key, {"value": 0.0, "episodes": []})
        cell["value"] = value if ev.get("op") == "set" else cell["value"] + value
        ep = ev.get("_episode_uuid")
        if ep and ep not in cell["episodes"]:
            cell["episodes"].append(ep)
    return state


def consolidate(
    *,
    namespace: str,
    episodes: list[Episode],
    graph_adapter: GraphAdapter,
    llm_complete: LlmComplete,
    embed: Embed | None = None,
    source: str = "consolidation",
) -> list[dict[str, Any]]:
    """End-to-end: extract -> fold -> write one supersedable counter node per (subject, counter).

    If `embed` is given, each counter gets a name_embedding on its retrieval text so recall's
    hybrid (bm25 + cosine) search surfaces it for a "how often did X" query. Without it, the
    counter still carries a retrieval-friendly `name` (bm25 surface) — surfacing degrades to
    lexical-only, never breaks."""
    folded = fold(extract_events(episodes, llm_complete))
    results = []
    for (subject, counter), cell in sorted(folded.items(), key=lambda kv: -kv[1]["value"]):
        emb = None
        if embed is not None:
            try:
                emb = embed(_retrieval_text(subject, counter, cell["value"]))
            except Exception:
                emb = None  # surfacing degrades to bm25-only; never block the write
        res = graph_adapter.record_counter(
            subject=subject, counter=counter, value=cell["value"],
            namespace=namespace, episode_uuids=cell["episodes"], source=source,
            name_embedding=emb,
        )
        res.update({"subject": subject, "counter": counter, "value": cell["value"],
                    "n_episodes": len(cell["episodes"])})
        results.append(res)
    return results
