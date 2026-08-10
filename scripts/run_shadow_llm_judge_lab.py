"""Runner for shadow_llm_judge_lab.py -- makes REAL LLM calls (gpt-4o-mini) testing whether an
LLM judge preserves true positives while rejecting hard negatives that pure embedding similarity
could not separate (the semantic-similarity lab found one wrong_state_family decoy scoring
HIGHER than every true positive).

Tests the judge against all 69 rows from the same fixture set as the similarity lab (21
Phase-4b-validated eligibility scenarios + 36 cross-family controls), so results are directly
comparable row-for-row.

Usage:
    .venv/Scripts/python.exe scripts/run_shadow_llm_judge_lab.py [--trials N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
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
from menhir.infrastructure.providers import build_chat_backend  # noqa: E402
from menhir.explorer.shadow_llm_judge_lab import (  # noqa: E402
    JUDGE_SYSTEM_PROMPT,
    JudgedRow,
    category_breakdown,
    confusion_counts,
    judge_user_prompt,
    parse_judge_response,
    precision_recall_f1,
)
from menhir.explorer.shadow_semantic_similarity_lab import build_similarity_lab_rows  # noqa: E402


async def _judge_one(create, row) -> bool | None:
    try:
        raw = await create(
            system_prompt=JUDGE_SYSTEM_PROMPT,
            user_prompt=judge_user_prompt(row.embed_message_text, row.embed_candidate_text),
            operation="shadow_llm_judge_lab",
            max_tokens=16,
            temperature=0.0,
        )
    except Exception as exc:
        print(f"    CALL FAILED: {exc!r}")
        return None
    return parse_judge_response(raw)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    llm_backend = build_chat_backend(MemorySettings.from_env())
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured LLM backend has no create_chat_completion")

    rows = build_similarity_lab_rows()
    rng = random.Random(args.seed)
    order = list(range(len(rows)))
    rng.shuffle(order)

    print(f"Judging {len(rows)} (message, candidate) rows, shuffled order...")
    judged: list[JudgedRow] = [None] * len(rows)  # type: ignore[list-item]
    for i, idx in enumerate(order, 1):
        row = rows[idx]
        judgment = await _judge_one(create, row)
        judged[idx] = JudgedRow(row=row, judgment=judgment)
        mark = "MATCH" if judgment else ("NO_MATCH" if judgment is False else "UNPARSEABLE")
        correct = "" if judgment is None else ("OK" if judgment == row.is_positive else "WRONG")
        print(f"  [{i}/{len(rows)}] {row.category:22s} gt_positive={row.is_positive!s:5s} "
              f"judge={mark:12s} {correct}  {row.scenario_id}/{row.candidate_id}")

    print("\n=== Confusion matrix (all 69 rows) ===")
    counts = confusion_counts(judged)
    metrics = precision_recall_f1(counts)
    print(f"  TP={counts['tp']} FP={counts['fp']} TN={counts['tn']} FN={counts['fn']} "
          f"unparseable={counts['unparseable']}")
    print(f"  precision={metrics['precision']:.3f} recall={metrics['recall']:.3f} "
          f"f1={metrics['f1']:.3f}")

    print("\n=== Within-family only (excludes cross_family_control) ===")
    within_family = [jr for jr in judged if jr.row.category != "cross_family_control"]
    wf_counts = confusion_counts(within_family)
    wf_metrics = precision_recall_f1(wf_counts)
    print(f"  TP={wf_counts['tp']} FP={wf_counts['fp']} TN={wf_counts['tn']} FN={wf_counts['fn']} "
          f"unparseable={wf_counts['unparseable']}")
    print(f"  precision={wf_metrics['precision']:.3f} recall={wf_metrics['recall']:.3f} "
          f"f1={wf_metrics['f1']:.3f}")

    print("\n=== Category breakdown ===")
    breakdown = category_breakdown(judged)
    for category, stats in sorted(breakdown.items()):
        print(f"  {category:24s} correct={stats['correct']:3d} incorrect={stats['incorrect']:3d} "
              f"unparseable={stats['unparseable']:3d}")

    print("\n=== The 'boundaries' hard case (lme-2698e78f wrong_state_family decoy) ===")
    for jr in judged:
        if jr.row.fixture_qid == "lme-2698e78f" and jr.row.category == "wrong_state_family":
            outcome = "CORRECTLY REJECTED" if jr.judgment is False else (
                "WRONGLY ACCEPTED" if jr.judgment is True else "UNPARSEABLE"
            )
            print(f"  {jr.row.scenario_id}/{jr.row.candidate_id}: judge={jr.judgment} -> {outcome}")

    out_path = MENHIR_ROOT / "results" / "shadow_llm_judge_lab.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({
        "confusion_all": counts,
        "metrics_all": metrics,
        "confusion_within_family": wf_counts,
        "metrics_within_family": wf_metrics,
        "category_breakdown": breakdown,
        "rows": [
            {"scenario_id": jr.row.scenario_id, "fixture_qid": jr.row.fixture_qid,
             "category": jr.row.category, "is_positive": jr.row.is_positive,
             "candidate_id": jr.row.candidate_id, "judgment": jr.judgment}
            for jr in judged
        ],
    }, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
