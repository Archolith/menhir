"""Runner for shadow_contrastive_judge_lab.py -- makes REAL LLM calls (gpt-4o-mini) testing
whether a CONTRASTIVE judge (message + ALL real candidates shown together, one call) succeeds
where the pairwise judge failed 3 times: preserve the true positive while rejecting the
"boundaries" wrong_state_family decoy specifically, and every other real negative category.

Usage:
    .venv/Scripts/python.exe scripts/run_shadow_contrastive_judge_lab.py
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
from menhir.infrastructure.providers import build_chat_backend  # noqa: E402
from menhir.explorer.shadow_contrastive_judge_lab import (  # noqa: E402
    CONTRASTIVE_JUDGE_SYSTEM_PROMPT,
    build_contrastive_test_cases,
    contrastive_user_prompt,
    parse_contrastive_response,
    score_contrastive_result,
)


async def main() -> None:
    llm_backend = build_chat_backend(MemorySettings.from_env())
    backend = getattr(llm_backend, "backend", llm_backend)
    create = getattr(backend, "create_chat_completion", None)
    if create is None:
        raise RuntimeError("configured LLM backend has no create_chat_completion")

    test_cases = build_contrastive_test_cases()
    print(f"Running {len(test_cases)} contrastive test cases (7 candidates each)...")

    all_results = []
    for tc in test_cases:
        print(f"\n=== {tc.fixture_qid} ===")
        candidate_pairs = [(c.candidate_id, c.text) for c in tc.candidates]
        try:
            raw = await create(
                system_prompt=CONTRASTIVE_JUDGE_SYSTEM_PROMPT,
                user_prompt=contrastive_user_prompt(tc.message, candidate_pairs),
                operation="shadow_contrastive_judge_lab",
                max_tokens=800,
                temperature=0.0,
            )
        except Exception as exc:
            print(f"  CALL FAILED: {exc!r}")
            raw = None

        result = parse_contrastive_response(tc, raw)
        score = score_contrastive_result(result)
        all_results.append((tc, result, score))

        if score is None:
            print(f"  UNPARSEABLE / CALL FAILED. Raw: {raw!r}")
            continue

        by_id = {c.candidate_id: c for c in tc.candidates}
        print(f"  selected_ids={sorted(score.selected_ids)}")
        for cid in sorted(score.selected_ids):
            cand = by_id.get(cid)
            print(f"    -> {cid}: category={cand.category if cand else '???'!r} "
                  f"text={cand.text if cand else '???'!r}")
        print(f"  true_positive_preserved={score.true_positive_preserved}  "
              f"any_negative_selected={score.any_negative_selected}  "
              f"boundaries_selected={score.boundaries_selected}")
        # Full audit dump -- what the model extracted per candidate (same_slot etc.), for
        # inspection only, never used for scoring above.
        if result.parsed_audit and isinstance(result.parsed_audit.get("candidates"), list):
            print("  --- audit (extracted fields, NOT used for scoring) ---")
            print(f"  message: {result.parsed_audit.get('message')}")
            for entry in result.parsed_audit["candidates"]:
                print(f"    {entry}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    n = len(all_results)
    scored = [(tc, s) for tc, r, s in all_results if s is not None]
    preserved = sum(1 for _, s in scored if s.true_positive_preserved)
    clean = sum(1 for _, s in scored if s.true_positive_preserved and not s.any_negative_selected)
    boundaries_rejected = sum(
        1 for tc, s in scored
        if any(c.category == "wrong_state_family" for c in tc.candidates) and not s.boundaries_selected
    )
    boundaries_cases = sum(
        1 for tc, s in scored if any(c.category == "wrong_state_family" for c in tc.candidates)
    )
    print(f"Test cases: {n}, scored (parseable): {len(scored)}")
    print(f"True positive preserved: {preserved}/{len(scored)}")
    print(f"Clean (positive preserved, zero negatives selected): {clean}/{len(scored)}")
    print(f"Boundaries decoy correctly rejected: {boundaries_rejected}/{boundaries_cases}")

    out_path = MENHIR_ROOT / "results" / "shadow_contrastive_judge_lab.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({
        "summary": {
            "test_cases": n, "scored": len(scored), "true_positive_preserved": preserved,
            "clean": clean, "boundaries_rejected": boundaries_rejected,
            "boundaries_cases": boundaries_cases,
        },
        "results": [
            {
                "fixture_qid": tc.fixture_qid,
                "raw_response": r.raw_response,
                "selected_ids": sorted(s.selected_ids) if s else None,
                "true_positive_preserved": s.true_positive_preserved if s else None,
                "any_negative_selected": s.any_negative_selected if s else None,
                "boundaries_selected": s.boundaries_selected if s else None,
                "parsed_audit": r.parsed_audit,
            }
            for tc, r, s in all_results
        ],
    }, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
