"""Runner for shadow_semantic_similarity_lab.py -- makes REAL embedding-API calls against
production's own embedder (GraphitiClient.embed_query, text-embedding-3-small).

Validation experiment for the direct-message<->fact-embedding-similarity proposal in response
to .agent/reviews/menhir-shadow-context-composition-facet-instability-2026-07-16.md. Must
demonstrate real discrimination between positive matches and each negative category BEFORE any
change is made to production shadow_context_composition.py.

Usage:
    .venv/Scripts/python.exe scripts/run_shadow_semantic_similarity_lab.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

MENHIR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))


def _load_openai_key_from_env_file() -> str:
    env_path = MENHIR_ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"OPENAI_API_KEY not found in {env_path}")


def _configure_environment() -> None:
    os.environ.setdefault("OPENAI_API_KEY", _load_openai_key_from_env_file())
    os.environ["GRAPHITI_LLM_PROVIDER"] = "openai"
    os.environ["MEMORY_GRAPHITI_PROVIDER"] = "openai"
    os.environ["LLM_CHAT_PROVIDER"] = "openai"
    os.environ["MEMORY_CHAT_PROVIDER"] = "openai"
    os.environ["OPENAI_CHAT_MODEL"] = "gpt-4o-mini"
    os.environ.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    os.environ.setdefault("NEO4J_URI", "bolt://localhost:7689")
    os.environ.setdefault("NEO4J_PASSWORD", "lmedata123")


_configure_environment()

from menhir.config import MemorySettings  # noqa: E402
from menhir.core.bootstrap import build_memory_services  # noqa: E402
from menhir.explorer.shadow_semantic_similarity_lab import (  # noqa: E402
    best_f1_point,
    build_similarity_lab_rows,
    category_summary,
    precision_recall_curve,
    score_rows,
    unique_embed_texts,
)


async def main() -> None:
    settings = MemorySettings.from_env()
    artifacts = build_memory_services(settings)
    graphiti_client = artifacts.graphiti_client

    rows = build_similarity_lab_rows()
    texts = unique_embed_texts(rows)
    print(f"Built {len(rows)} (message, candidate) rows over {len(texts)} unique texts to embed.")

    embeddings: dict[str, list[float]] = {}
    for i, text in enumerate(texts, 1):
        try:
            vector = await graphiti_client.embed_query(text)
        except Exception as exc:
            print(f"  [{i}/{len(texts)}] EMBED FAILED: {text[:60]!r}: {exc!r}")
            continue
        embeddings[text] = vector
        print(f"  [{i}/{len(texts)}] embedded ({len(vector)}d): {text[:60]!r}")

    scored = score_rows(rows, embeddings)

    print("\n=== Category similarity summary ===")
    summary = category_summary(scored)
    for category, stats in sorted(summary.items(), key=lambda kv: -kv[1]["mean"]):
        print(f"  {category:24s} n={stats['count']:3.0f}  mean={stats['mean']:.4f}  "
              f"min={stats['min']:.4f}  max={stats['max']:.4f}")

    print("\n=== Precision/recall curve (threshold sweep) ===")
    curve = precision_recall_curve(scored)
    for point in curve:
        if point.threshold * 100 % 10 == 0:  # print every 0.1 step to keep output readable
            print(f"  t={point.threshold:.2f}  P={point.precision:.3f}  R={point.recall:.3f}  "
                  f"F1={point.f1:.3f}  TP={point.true_positives} FP={point.false_positives} "
                  f"FN={point.false_negatives}")

    best = best_f1_point(curve)
    print(f"\nBest-F1 threshold: t={best.threshold:.3f}  P={best.precision:.3f}  "
          f"R={best.recall:.3f}  F1={best.f1:.3f}  "
          f"TP={best.true_positives} FP={best.false_positives} FN={best.false_negatives}")

    out_path = MENHIR_ROOT / "results" / "shadow_semantic_similarity_lab.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({
        "category_summary": summary,
        "curve": [
            {"threshold": p.threshold, "precision": p.precision, "recall": p.recall,
             "f1": p.f1, "tp": p.true_positives, "fp": p.false_positives, "fn": p.false_negatives}
            for p in curve
        ],
        "best_f1_point": {
            "threshold": best.threshold, "precision": best.precision, "recall": best.recall,
            "f1": best.f1, "tp": best.true_positives, "fp": best.false_positives,
            "fn": best.false_negatives,
        },
        "rows": [
            {"scenario_id": sr.row.scenario_id, "fixture_qid": sr.row.fixture_qid,
             "category": sr.row.category, "is_positive": sr.row.is_positive,
             "candidate_id": sr.row.candidate_id, "similarity": sr.similarity}
            for sr in scored
        ],
    }, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
