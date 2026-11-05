#!/usr/bin/env python
"""Batch recall with no answer model and no judge: every candidate of every query, as JSONL.

Plan: menhir-recall-real-similarity-test-plan-2026-11-05. Each output row holds the shipped
results with their labels, and every trace candidate including floor-dropped ones.

    MENHIR_NEO4J_URI=bolt://127.0.0.1:18341 ... python scripts/recall_lab_batch.py \
        --input queries.jsonl --arms arms.json --output out.jsonl

* ``--input``: JSONL rows of ``{"id", "namespace", "query"}``.
* ``--arms``: JSON list of ``{"id", "tuning": {...}}``. Tuning overrides apply on top of the
  configured production tuning, so ``{"id": "base", "tuning": {}}`` is the production baseline.

Recall runs as ``recall_memories`` calls it, except that it never waits on pending episodes and
never updates access state. Rerunning skips (id, arm) pairs already in ``--output``, and query
vectors are pinned in ``<output>.vectors.json`` so arms differ only by their tuning. It refuses
any graph that is not on loopback, and the known shared ports, and it blocks the LLM client and
reranker. A row with ``error`` or ``name_cosine_warning`` means the arm did not run as asked, so
the exit code is 1 and that row is not reusable as a result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from menhir.config import MemorySettings
from menhir.domain.recall import QueryPreset
from menhir.domain.retrieval_tuning import RetrievalTuningConfig

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Live instance, shared test container and the persistent LME graph.
SHARED_PORTS = frozenset({7687, 7688, 7689})


def check_target(uri: str) -> None:
    """Refuse anything but a throwaway graph reached over loopback."""
    parsed = urlparse(uri)
    if parsed.hostname not in LOOPBACK_HOSTS or (parsed.port or 7687) in SHARED_PORTS:
        raise SystemExit(f"refusing graph {parsed.hostname}:{parsed.port}: use a throwaway copy")


def load_rows(path: Path) -> list[dict[str, str]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        missing = {"id", "namespace", "query"} - set(row)
        if missing:
            raise SystemExit(f"input row {row.get('id')!r} is missing {sorted(missing)}")
    return rows


def load_arms(path: Path, base: RetrievalTuningConfig) -> dict[str, RetrievalTuningConfig]:
    """Arm id to tuning. An unknown tuning key raises here, before any recall runs."""
    arms = {
        arm["id"]: replace(base, record_name_cosine=True, **arm.get("tuning", {}))
        for arm in json.loads(path.read_text(encoding="utf-8"))
    }
    if not arms:
        raise SystemExit("no arms")
    return arms


def finished_pairs(path: Path) -> set[tuple[str, str]]:
    """(id, arm) pairs with a clean row. Failed rows are retried."""
    if not path.exists():
        return set()
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return {(r["id"], r["arm"]) for r in rows if not _failed(r)}


def _failed(row: dict[str, Any]) -> bool:
    return bool(row.get("error") or row.get("name_cosine_warning"))


def block_llm(graphiti_client: Any) -> list[str]:
    """Make any LLM or reranker call fail, and return the list that records attempts."""
    calls: list[str] = []

    def blocked(*_args: object, **_kwargs: object) -> None:
        calls.append("llm")
        raise RuntimeError("recall_lab_batch runs with no LLM")

    for ref, method in (
        (getattr(graphiti_client, "llm_client_ref", None), "generate_response"),
        (getattr(graphiti_client, "reranker_ref", None), "rank"),
    ):
        if ref is not None:
            setattr(ref, method, blocked)
    return calls


def pin_query_vectors(graphiti_client: Any, cache_path: Path) -> dict[str, Any]:
    """Serve one stored vector per input, across arms and reruns.

    The embedding API is not bit-stable: on 50 questions a rerun flipped about one result order,
    which an arm comparison would misread as the floor's effect. Returns the live cache for
    ``save_vectors``.
    """
    vectors: dict[str, Any] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    embedder = graphiti_client.embedder_ref
    real_create = embedder.create

    async def pinned_create(input_data: Any, *args: Any, **kwargs: Any) -> Any:
        key = json.dumps(input_data, sort_keys=True, default=str)
        if key not in vectors:
            vectors[key] = await real_create(input_data, *args, **kwargs)
        return vectors[key]

    embedder.create = pinned_create
    return vectors


def save_vectors(vectors: dict[str, Any], cache_path: Path) -> None:
    cache_path.write_text(json.dumps(vectors), encoding="utf-8")


def serialize_candidate(candidate: Any, cosines: dict[str, float | None]) -> dict[str, Any]:
    row = asdict(candidate)
    row["source"] = candidate.source.value
    row["contributing_sources"] = sorted(s.value for s in candidate.contributing_sources)
    row["name_cosine"] = cosines.get(candidate.uuid)
    return row


def serialize_result(result: Any) -> dict[str, Any]:
    from menhir.mcp.formatters import _compact_scored_item
    from menhir.services.context_builder import estimate_tokens

    # Both are None when recall found no candidates and returned early.
    trace = result.trace
    record = result.name_cosine
    cosines = record.cosines if record else {}
    return {
        "search_error": result.search_error,
        "note": result.note,
        "candidates_evaluated": result.candidates_evaluated,
        "rank_shadow_warning": getattr(trace, "rank_shadow_warning", None),
        "name_cosine_warning": record.warning if record else None,
        "results": [
            {
                "rank": rank,
                "uuid": memory.uuid,
                "memory_type": memory.memory_type,
                "view_kind": memory.view_kind,
                "final_score": memory.final_score,
                "retrieval_score": memory.retrieval_score,
                "relevance": _compact_scored_item(memory)["relevance"],
                "name_cosine": cosines.get(memory.uuid),
                "tokens": estimate_tokens(f"{memory.name}\n{memory.content or ''}")[0],
            }
            for rank, memory in enumerate(result.results, start=1)
        ],
        "candidates": [
            serialize_candidate(c, cosines) for c in getattr(trace, "candidates", ())
        ],
        "source_memories": [
            {key: value for key, value in asdict(sm).items() if key != "content"}
            for sm in result.source_memories or ()
        ],
    }


async def run_one(
    recall_service: Any,
    row: dict[str, str],
    arm_id: str,
    tuning: RetrievalTuningConfig,
    *,
    limit: int,
    candidate_k: int,
    preset: QueryPreset,
    source_memory_limit: int | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"id": row["id"], "arm": arm_id, "namespace": row["namespace"]}
    started = perf_counter()
    try:
        result = await recall_service.recall(
            row["query"],
            preset=preset,
            limit=limit,
            candidate_k=candidate_k,
            include_session=True,
            namespace=row["namespace"],
            tuning=tuning,
            trace=True,
            update_access=False,
            source_memory_limit=source_memory_limit,
        )
        out.update(serialize_result(result))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
    return out


async def run_batch(
    recall_service: Any,
    rows: list[dict[str, str]],
    arms: dict[str, RetrievalTuningConfig],
    output: Path,
    *,
    concurrency: int,
    **recall_args: Any,
) -> int:
    """Run every unfinished (row, arm) pair and return how many failed."""
    done = finished_pairs(output)
    gate = asyncio.Semaphore(concurrency)
    failed = 0

    with output.open("a", encoding="utf-8") as sink:
        async def work(row: dict[str, str], arm_id: str) -> None:
            nonlocal failed
            async with gate:
                record = await run_one(recall_service, row, arm_id, arms[arm_id], **recall_args)
            failed += _failed(record)
            sink.write(json.dumps(record, default=str) + "\n")
            sink.flush()

        await asyncio.gather(*(
            work(row, arm_id)
            for row in rows for arm_id in arms if (row["id"], arm_id) not in done
        ))
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--arms", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--preset", default="knowledge")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    settings = MemorySettings.from_env()
    check_target(settings.neo4j_uri)
    rows = load_rows(args.input)
    arms = load_arms(args.arms, settings.retrieval_tuning())

    from menhir.cli.bootstrap import build_hook_services

    builder = build_hook_services(settings).context_builder
    recall_service = getattr(builder, "recall_service", None)
    if recall_service is None:
        raise SystemExit("direct recall is unavailable; check the Graphiti and embedder settings")
    llm_calls = block_llm(recall_service.graphiti_client)
    vector_cache = args.output.with_suffix(".vectors.json")
    vectors = pin_query_vectors(recall_service.graphiti_client, vector_cache)

    try:
        failed = asyncio.run(run_batch(
            recall_service, rows, arms, args.output,
            concurrency=args.concurrency,
            limit=args.limit,
            candidate_k=args.candidate_k,
            preset=QueryPreset[args.preset.upper()],
            source_memory_limit=getattr(settings, "frontier_source_memory_limit", None),
        ))
    finally:
        save_vectors(vectors, vector_cache)
    print(f"rows={len(rows)} arms={len(arms)} failed={failed} llm_calls={len(llm_calls)}")
    return 1 if failed or llm_calls else 0


if __name__ == "__main__":
    sys.exit(main())
