"""Shared data models and the API contract constant for the bench-run explorer.

Extracted from ``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "bench-inspection/v1"


@dataclass
class BenchScore:
    arm: str = ""
    correct: bool = False
    score: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    response_text: str = ""
    recalled: str = ""
    gold: str = ""
    question: str = ""


@dataclass
class BenchTask:
    namespace: str = ""
    question_id: str = ""
    question: str = ""
    answer: str = ""
    question_type: str = ""
    turns: int = 0
    typed_assertions: int = 0
    scalar_views: int = 0
    turn_evidence: int = 0
    scalar_llm_calls: int = 0
    scalar_consolidated: bool = False
    ready: bool = False
    failed_remaining: int = 0
    graph_available: bool | None = None
    scores: list[BenchScore] = field(default_factory=list)
    source_warning: str | None = None
    primary_outcome: str | None = None  # "PASSED", "FAILED", or "NOT SCORED"


@dataclass
class BenchRun:
    run_id: str = ""
    label: str = ""
    source: str = ""
    variant: str = ""
    model: str = ""
    total_items: int = 0
    completed_items: int = 0
    tasks: list[BenchTask] = field(default_factory=list)
    arms: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: str = ""
    menhir_commit: str = ""
    bench_commit: str = ""
    arm: str = ""
    is_active: bool = False
    source_warning: str | None = None
    attempts: int = 0
    noncanonical: bool = False
    resumed: bool = False
    phases: list[dict[str, Any]] = field(default_factory=list)
    harness_exit: int | None = None
    graph_source_run_id: str | None = None
    live_graph_source_run_id: str | None = None
    source_graph_reused: bool = False
    reingested: bool = False
    graph_container: str = ""
    graph_volume: str = ""
    fixture_sha256: str = ""
    fixture_count: int = 0
    namespace_prefix: str = ""


def _primary_outcome(scores: list[BenchScore]) -> str | None:
    """Determine the primary outcome pill for a task.

    Selection rule: prefer the score whose arm is ``menhir_recall``;
    otherwise the first scored arm that is not ``no_memory``; if none
    exist return ``NOT SCORED``.
    """
    if not scores:
        return "NOT SCORED"
    candidate: BenchScore | None = None
    for s in scores:
        if s.arm == "menhir_recall":
            candidate = s
            break
    if candidate is None:
        for s in scores:
            if s.arm != "no_memory":
                candidate = s
                break
    if candidate is None:
        return "NOT SCORED"
    return "PASSED" if candidate.correct else "FAILED"
