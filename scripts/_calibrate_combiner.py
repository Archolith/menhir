"""Throwaway: calibrate LogSpaceOracleCombiner weights against the nDCG harness.

Diagnosis from _eval_frontier: FAMILY_ALPHA['semantic']=0.8 under-weights semantic relevance
(lowest of all families) + the MAX_FAMILY_CONTRIBUTION=3.0 cap, so oracle_ranking demotes the
best semantic hit on plain queries. Sweep semantic alpha + cap; judge a generous pooled set
ONCE per query (blind, gpt-4.1-mini), score each config by mean nDCG@5 vs shipped. Not committed.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
os.environ["NEO4J_URI"] = "bolt://localhost:7687"
os.environ["NEO4J_USER"] = "neo4j"
os.environ["NEO4J_PASSWORD"] = "menhirdummy123"
os.environ["MENHIR_BENCHMARK_MODE"] = "1"
for _k in list(os.environ):
    if _k.startswith("MENHIR_FRONTIER_"):
        del os.environ[_k]

from openai import OpenAI  # noqa: E402
import menhir.domain.oracle_combiner as OC  # noqa: E402
from menhir.config import MemorySettings  # noqa: E402
from menhir.core import build_memory_services, prepare_memory_runtime  # noqa: E402
from menhir.domain.retrieval_tuning import RetrievalTuningConfig  # noqa: E402

JUDGE_MODEL = "gpt-4.1-mini"
K = 5
LIMIT = 8
QUERIES = [
    "how does the oracle combiner rank candidates",
    "why is recall returning stale memories",
    "why did we choose project as the scope axis",
    "have we already tried wiring evidence kinds",
    "what changed in the warden gate",
    "menhir intent oracle classification",
    "graphiti hybrid search alpha tuning",
    "what should I do next on the frontier wiring",
    "namespace isolation group_id",
    "structural anchoring ANCHORED_TO files",
    "temporal supersession historical lens",
    "how does the assertion pipeline bucket results",
]

# Evidence-weight sweep: keep semantic at baseline 0.8 / cap 3.0; lower the SECONDARY
# families (evidence 1.2 is currently the highest, overriding relevance on topical queries).
# (label, semantic_alpha, max_family_contribution, extra_alpha_overrides)
CONFIGS = [
    ("baseline(ev1.2,str1.0)", 0.8, 3.0, {}),
    ("ev0.8", 0.8, 3.0, {"evidence": 0.8}),
    ("ev0.6", 0.8, 3.0, {"evidence": 0.6}),
    ("ev0.4", 0.8, 3.0, {"evidence": 0.4}),
    ("ev0.6,str0.8", 0.8, 3.0, {"evidence": 0.6, "structure": 0.8}),
    ("ev0.4,str0.7", 0.8, 3.0, {"evidence": 0.4, "structure": 0.7}),
    ("ev0.4,str0.7,scope0.4", 0.8, 3.0, {"evidence": 0.4, "structure": 0.7, "scope": 0.4}),
]

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
_BASE_ALPHA = dict(OC.FAMILY_ALPHA)


def set_weights(sem: float, cap: float, extra: dict) -> None:
    a = dict(_BASE_ALPHA); a["semantic"] = sem; a.update(extra)
    OC.FAMILY_ALPHA = a
    OC.MAX_FAMILY_CONTRIBUTION = cap


def judge(query: str, memos: list[dict]) -> dict[str, int]:
    lines = [f"[{i}] {m['text'][:600]}" for i, m in enumerate(memos)]
    prompt = (f"Query: {query}\n\nMemories:\n" + "\n".join(lines) +
              "\n\nRate each memory's relevance: 3=highly relevant,2=relevant,1=marginal,0=irrelevant. "
              'Return ONLY JSON mapping bracket index (string) to integer grade.')
    resp = client.chat.completions.create(
        model=JUDGE_MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "You are a strict IR relevance judge."},
                  {"role": "user", "content": prompt}])
    raw = json.loads(resp.choices[0].message.content)
    return {memos[int(i)]["id"]: int(g) for i, g in raw.items() if int(i) < len(memos)}


def ndcg(order, grades, k):
    dcg = sum((2 ** grades.get(c, 0) - 1) / math.log2(r + 2) for r, c in enumerate(order[:k]))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(r + 2) for r, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


async def main():
    settings = MemorySettings.from_env()
    built = build_memory_services(settings)
    await prepare_memory_runtime(built)
    cfg = RetrievalTuningConfig(enable_oracle_ranking=True, enable_intent_lens=True)
    rng = random.Random(42)

    # Phase A: per query, run shipped + every config, pool union of top-K, judge ONCE.
    per_query = {}  # q -> {"shipped": order, "orders": {label: order}, "grades": {}}
    try:
        for q in QUERIES:
            shipped = await built.recall_service.recall(q, limit=LIMIT, tuning=RetrievalTuningConfig())
            s_order = [r.uuid for r in shipped.results][:K]
            by_id = {r.uuid: r for r in shipped.results}
            orders = {}
            for label, sem, cap, extra in CONFIGS:
                set_weights(sem, cap, extra)
                fr = await built.recall_service.recall(q, limit=LIMIT, tuning=cfg)
                orders[label] = [r.uuid for r in fr.results][:K]
                for r in fr.results:
                    by_id.setdefault(r.uuid, r)
            pool = list(dict.fromkeys(s_order + [c for o in orders.values() for c in o]))
            memos = [{"id": c, "text": f"{by_id[c].name or ''}. {by_id[c].content or ''}"} for c in pool]
            shuf = memos[:]; rng.shuffle(shuf)
            grades = judge(q, shuf)
            per_query[q] = {"shipped": s_order, "orders": orders, "grades": grades}
            print(f"judged {q!r} (pool={len(pool)})")
    finally:
        await built.graphiti_client.close(); built.neo4j.close()

    # Phase B: score (pure CPU).
    n = len(per_query)
    ship_mean = sum(ndcg(d["shipped"], d["grades"], K) for d in per_query.values()) / n
    print(f"\nshipped nDCG@{K} mean = {ship_mean:.4f}  (n={n})\n")
    results = []
    for label, *_ in CONFIGS:
        m = sum(ndcg(d["orders"][label], d["grades"], K) for d in per_query.values()) / n
        results.append((label, m))
    results.sort(key=lambda x: -x[1])
    for label, m in results:
        flag = "  <-- beats shipped" if m > ship_mean else ""
        print(f"  {label:32s} nDCG@{K}={m:.4f}  d={m-ship_mean:+.4f}{flag}")
    best = results[0]
    print(f"\nBEST: {best[0]}  nDCG={best[1]:.4f}  d={best[1]-ship_mean:+.4f}")


if __name__ == "__main__":
    asyncio.run(main())
