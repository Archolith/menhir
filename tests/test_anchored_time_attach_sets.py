"""Integrity of the attach-check labeled sets (plan menhir-anchored-time-attachment-plan.md P1).

Set 2 (the adversarial fixture and its fresh blind third) is checked here; set 1 is built offline by
``scripts/build_attach_set.py`` from the workspace eval data and is not in the repo.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from menhir.infrastructure.anchored_time import _fold

pytestmark = pytest.mark.unit

FIXTURES = [Path(__file__).parent / "fixtures" / "anchored_time" / name
            for name in ("attach_adversarial.json", "attach_adversarial_fresh.json")]
CASES = [c for path in FIXTURES for c in json.loads(path.read_text(encoding="utf-8"))["cases"]]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_set2_ids_are_unique_and_the_file_is_ascii(path) -> None:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    assert len({c["id"] for c in cases}) == len(cases) > 0
    path.read_bytes().decode("ascii")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_set2_case_is_well_formed(case) -> None:
    facts, target = case["facts"], case["target"]
    date.fromisoformat(case["speech_date"])
    assert isinstance(target, int) and 0 <= target < len(facts)
    assert str(target) in case["items"]
    assert case["label"] in ("write", "no_write")
    if case["label"] == "write":
        start, end = map(date.fromisoformat, case["correct_window"])
        assert start <= end
    else:
        assert case["correct_window"] is None
    for key, item in case["items"].items():
        i = int(key)
        assert 0 <= i < len(facts) and item.get("i") == i
        expression = item.get("expression")
        if expression:  # verbatim from the turn, or from the fact text it dates
            assert _fold(expression) in _fold(case["turn"]) or _fold(expression) in _fold(facts[i])
