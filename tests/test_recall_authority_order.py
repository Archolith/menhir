"""Authority/history lanes iterate slots by relevance, not by hash-random set order."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from menhir.services.recall_pipeline import _relevance_ordered_slots
from tests.test_counting_recall_authority_only import R_ON, _occ_hit
from tests.test_scalar_history_recall import _setup_observation_lane, _setup_search_and_metadata
from tests.test_scalar_view_collapse_drilldown import QUERY, _state_view, _svc

A = ("s", "a", "", "count", "")
B = ("s", "b", "", "count", "")
C = ("s", "c", "", "count", "")


@pytest.mark.unit
def test_orders_by_cosine_descending():
    cos = {A: 0.6, B: 0.9, C: 0.75}
    assert _relevance_ordered_slots({A, B, C}, cos) == [B, C, A]


@pytest.mark.unit
def test_missing_cosine_sorts_last():
    assert _relevance_ordered_slots({A, B, C}, {A: 0.1, C: 0.2}) == [C, A, B]


@pytest.mark.unit
def test_ties_break_by_slot_tuple_and_none_is_safe():
    n = ("s", None, "", "count", "")
    cos = {A: 0.5, B: 0.5, C: 0.5, n: 0.5}
    expected = _relevance_ordered_slots([A, B, C, n], cos)
    assert expected == [n, A, B, C]  # "" sorts before "a"


@pytest.mark.unit
def test_input_permutation_and_container_independent():
    cos = {A: 0.6, B: 0.9, C: 0.6}
    expected = [B, A, C]
    for perm in itertools.permutations([A, B, C]):
        assert _relevance_ordered_slots(list(perm), cos) == expected
        assert _relevance_ordered_slots(set(perm), cos) == expected
        assert _relevance_ordered_slots(tuple(perm), cos) == expected


@pytest.mark.unit
def test_hash_seed_independence():
    root = Path(__file__).resolve().parents[1]
    code = (
        "from menhir.services.recall_pipeline import _relevance_ordered_slots as f\n"
        "slots = {('subj', 'attr%d' % i, 'sc', 'count', 'u') for i in range(8)}\n"
        "cos = {s: (i * 7 % 8) / 10 for i, s in enumerate(sorted(slots))}\n"
        "print([s[1] for s in f(slots, cos)])\n"
    )
    outs = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root / "src")}
        res = subprocess.run([sys.executable, "-c", code], env=env, cwd=root,
                             capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, res.stderr
        outs.append(res.stdout.strip())
    assert outs[0] == outs[1]
    # cos for attr i = (i*7 % 8)/10 -> i=1:7, 2:6, 3:5, 4:4, 5:3, 6:2, 7:1, 0:0
    assert outs[0] == str([f"attr{i}" for i in (1, 2, 3, 4, 5, 6, 7, 0)])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_layer_verdicts_in_descending_cosine_order(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    client, adapter = stub_graphiti_client, stub_memory_graph_adapter
    # Deliberately unsorted cosines; six slots so set order matching by luck is improbable.
    cosines = {"attr_a": 0.71, "attr_b": 0.93, "attr_c": 0.78, "attr_d": 0.88,
               "attr_e": 0.74, "attr_f": 0.83}
    hits = [_occ_hit(f"occ-{a}", c, f"counted {a}", attribute=a, unit=a)
            for a, c in cosines.items()]
    views = {a: {**_state_view("counted"), "uuid": f"sv-{a}", "attribute": a,
                 "view_key": f"vk-{a}", "unit": a} for a in cosines}
    _setup_search_and_metadata(client, adapter)
    for row in adapter.candidate_metadata:
        row["namespace"] = "proj"
    _setup_observation_lane(adapter, obs_hit=hits)
    adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: views.get(attribute))
    res = await _svc(client, adapter, **R_ON).recall(QUERY, namespace="proj", limit=10)
    got = [v.attribute for v in res.authority_layer if v.kind == "current"]
    assert got == sorted(cosines, key=lambda a: -cosines[a])
