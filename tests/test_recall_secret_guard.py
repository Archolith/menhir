from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from menhir.domain import recall as recall_mod
from menhir.domain.recall import RecallResult, ScoredMemory, SourceMemory, TemporalFact
from menhir.domain.recall_secret_guard import WITHHELD_NOTE, scrub_recall_result
from menhir.services import recall_pipeline

CANARY = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _memory(content: str, *, name: str = "n", fact: str | None = None) -> ScoredMemory:
    facts = (
        TemporalFact(
            fact=fact, valid_at=None, invalid_at=None, created_at=None, expired_at=None,
            is_current_belief=True, temporal_role="current_belief",
        ),
    ) if fact is not None else ()
    return ScoredMemory(
        uuid="u1", name=name, content=content, scope="SEMANTIC", memory_type="SEMANTIC",
        final_score=0.5, breakdown=None, temporal_facts=facts,  # type: ignore[arg-type]
    )


def _result(**kw) -> RecallResult:
    base = dict(query="q", preset="default", results=[], candidates_evaluated=1, nodes_touched=0)
    base.update(kw)
    return RecallResult(**base)


def test_scrubs_content_name_fact_and_source_memories() -> None:
    res = _result(
        results=[_memory(f"use {CANARY} here", name=f"key {GH}", fact=f"token {GH}")],
        source_memories=(SourceMemory("s1", f"raw {CANARY}", None, 0.9, "x"),),
    )
    out = scrub_recall_result(res)
    blob = repr(out)
    assert CANARY not in blob and GH not in blob
    assert "[REDACTED:" in out.results[0].content
    assert "[REDACTED:" in out.results[0].temporal_facts[0].fact
    assert "[REDACTED:" in out.source_memories[0].content


def test_clean_result_is_unchanged_object_graph() -> None:
    mem = _memory("The user prefers dark mode.")
    out = scrub_recall_result(_result(results=[mem]))
    assert out.results[0] is mem


def test_scan_failure_withholds_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_text: str):
        raise RuntimeError("scanner down")

    monkeypatch.setattr("menhir.domain.recall_secret_guard.redact", boom)
    res = _result(
        results=[_memory("anything")],
        source_memories=(SourceMemory("s1", "raw", None, 0.9, "x"),),
        note="existing",
    )
    out = scrub_recall_result(res)
    assert out.results == [] and out.source_memories is None
    assert WITHHELD_NOTE in (out.note or "") and "existing" in out.note


def test_run_recall_scrubs_regardless_of_inner_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_inner(service, query, *, owned_tasks, **kwargs):
        return _result(results=[_memory(f"leak {CANARY}")])

    monkeypatch.setattr(recall_pipeline, "_run_recall", fake_inner)
    out = asyncio.run(recall_pipeline.run_recall(object(), "q"))
    assert CANARY not in repr(out)
    assert replace(out).results[0].content.startswith("leak [REDACTED:")
    assert recall_mod is not None
