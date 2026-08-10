from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "analyze_recall_lab_scores.py"
SPEC = importlib.util.spec_from_file_location("analyze_recall_lab_scores", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _scores(value: float) -> dict[str, float]:
    return {dimension: value for dimension in MODULE.SCORE_DIMENSIONS}


def _detail(
    run_id: int,
    query: str,
    *,
    production: dict[str, float] | None = None,
    e: dict[str, float] | None = None,
    judge_ok: bool = True,
    degraded: bool = False,
) -> dict[str, Any]:
    production = production or _scores(4.0)
    e = e or _scores(3.0)
    return {
        "id": run_id,
        "request": {"query": query},
        "result": {
            "query": query,
            "arms": [
                {
                    "id": "production",
                    "label": "Production",
                    "ok": True,
                    "degraded": False,
                    "results": [{"uuid": "p1"}, {"uuid": "p2"}],
                },
                {
                    "id": "e",
                    "label": "E",
                    "ok": True,
                    "degraded": degraded,
                    "results": [{"uuid": "e1"}],
                },
            ],
            "judgment": {
                "ok": judge_ok,
                "average_scores": {"production": production, "e": e},
            },
        },
    }


@pytest.mark.unit
def test_observation_rejects_failed_judge_and_degraded_arm() -> None:
    observation, reason = MODULE.observation_from_detail(_detail(1, "q", judge_ok=False))
    assert observation is None
    assert reason == "judge_not_ok"

    observation, reason = MODULE.observation_from_detail(_detail(2, "q", degraded=True))
    assert observation is None
    assert reason == "unhealthy_arm"


@pytest.mark.unit
def test_latest_observation_per_query_collapses_retries() -> None:
    first, _ = MODULE.observation_from_detail(_detail(10, "same query"))
    retry, _ = MODULE.observation_from_detail(_detail(12, "same query", e=_scores(4.0)))
    other, _ = MODULE.observation_from_detail(_detail(11, "other query"))
    assert first is not None and retry is not None and other is not None

    selected = MODULE.latest_observation_per_query([retry, other, first])

    assert [row.run_id for row in selected] == [11, 12]


@pytest.mark.unit
def test_build_report_averages_each_dimension_and_ranks_e_gaps() -> None:
    first, _ = MODULE.observation_from_detail(
        _detail(
            20,
            "first",
            production=_scores(4.0),
            e={
                "relevance": 4.0,
                "completeness": 2.0,
                "ranking": 3.0,
                "noise_control": 4.0,
                "omission_safety": 1.0,
            },
        )
    )
    second, _ = MODULE.observation_from_detail(
        _detail(
            21,
            "second",
            production=_scores(3.0),
            e={
                "relevance": 4.0,
                "completeness": 4.0,
                "ranking": 3.0,
                "noise_control": 4.0,
                "omission_safety": 3.0,
            },
        )
    )
    assert first is not None and second is not None

    report = MODULE.build_report([first, second], focus="e", baseline="production", worst=1)

    assert report["arms"]["e"]["scores"] == {
        "relevance": 4.0,
        "completeness": 3.0,
        "ranking": 3.0,
        "noise_control": 4.0,
        "omission_safety": 2.0,
    }
    assert report["arms"]["e"]["composite"] == 3.2
    assert report["arms"]["e"]["average_hits"] == 1.0
    assert report["comparison"]["priority_order"][0] == "omission_safety"
    assert report["comparison"]["worst_queries"][0]["run_id"] == 20
    assert "warden" in report["comparison"]["lever_hints"]["omission_safety"].lower()
