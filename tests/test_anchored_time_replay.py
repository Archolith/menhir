"""Offline replay: the ported code reproduces the P0 prototype on frozen resolver outputs.

Dev sets always run (committed fixtures). Held-out parity and the pinned O2 scores (plan
section 12) run only when MENHIR_ANCHORED_TIME_EVAL_DIR points at the uncommitted held-outs.
"""
from __future__ import annotations

from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from menhir.infrastructure.anchored_time import clause_guard, compute, parse_items

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "anchored_time"
DEV = {"dev": ("dev_set.json", "ckpt_dev_fix2.jsonl"),
       "dev2": ("dev_set2.json", "ckpt_dev2_it2.jsonl"),
       "dev3": ("dev_set3.json", "ckpt_dev3_it1.jsonl")}
HELDOUT = {"heldout:fix": ("heldout_turns.json", "ckpt_heldout_fix.jsonl"),
           "heldout2:frozen": ("heldout2_turns.json", "ckpt_heldout2_frozen.jsonl"),
           "heldout2:frozen_b": ("heldout2_turns.json", "ckpt_heldout2_frozen_b.jsonl"),
           "heldout3:frozen": ("heldout3_turns.json", "ckpt_heldout3_frozen.jsonl")}
#: label -> (baseline, P0 resolver, P1 written) as "dated ok/n, false/n", overridden, kept inside.
PINNED_O2 = {
    "heldout:fix": ("18/32, 0/73", "31/32, 0/73", "26/32, 0/73", 10, 19),
    "heldout2:frozen": ("12/23, 0/76", "22/23, 4/76", "19/23, 2/76", 11, 13),
    "heldout2:frozen_b": ("12/23, 0/76", "21/23, 2/76", "19/23, 0/76", 9, 14),
    "heldout3:frozen": ("18/28, 0/93", "26/28, 0/93", "24/28, 0/93", 9, 16),
}
EVAL_DIR = os.environ.get("MENHIR_ANCHORED_TIME_EVAL_DIR")
needs_eval_dir = pytest.mark.skipif(not EVAL_DIR, reason="MENHIR_ANCHORED_TIME_EVAL_DIR not set")


def _turns(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [(t["id"], t["speech_date"], t["turn"], t["facts"]) for t in data]
    return [(t["hid"], t["speech_date"], t["turn"], [f["fact"] for f in t["facts"]]) for t in data["heldout"]]


def _replay(turns_path: Path, ckpt_path: Path) -> dict:
    rows = {}
    for line in ckpt_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["custom_id"]] = r
    out = {}
    for tid, speech_date, turn, facts in _turns(turns_path):
        r = rows.get(tid)
        rec = {"parse": "ok", "guard_drops": [], "windows": []}
        items: dict[int, dict] = {}
        if not r or r.get("error") or not r.get("content"):
            rec["parse"] = "no_output"
        else:
            try:
                items, _missing = parse_items(r["content"])
                rec["guard_drops"] = clause_guard(turn, facts, items)
            except ValueError:
                rec["parse"] = "error"
                items = {}
        ref = date.fromisoformat(speech_date[:10])
        for i in range(len(facts)):
            w = compute(ref, items, i)
            rec["windows"].append([d.isoformat() if d else None for d in w] if w else None)
        rec["bases"] = [(items.get(i) or {}).get("basis") for i in range(len(facts))]
        out[tid] = rec
    return out


def _assert_parity(actual: dict, expected: dict) -> None:
    assert sorted(actual) == sorted(expected)
    for tid, exp in expected.items():
        got = actual[tid]
        for key in ("parse", "guard_drops", "windows", "bases"):
            assert got[key] == exp[key], (tid, key)


def _expected_sets(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["sets"]


@pytest.mark.parametrize("name", sorted(DEV))
def test_dev_set_replay_matches_the_prototype(name) -> None:
    turns_file, ckpt = DEV[name]
    expected = _expected_sets(FIXTURES / "prototype_expected.json")[name]
    assert expected
    _assert_parity(_replay(FIXTURES / turns_file, FIXTURES / ckpt), expected)


def test_dev_replay_exercises_dated_windows() -> None:
    # The frozen dev outputs trigger no guard drop; tests/test_anchored_time_guard.py covers the guard.
    sets = _expected_sets(FIXTURES / "prototype_expected.json")
    windows = [w for s in sets.values() for t in s.values() for w in t["windows"]]
    assert sum(w is not None for w in windows) >= 10


@needs_eval_dir
@pytest.mark.parametrize("label", sorted(HELDOUT))
def test_heldout_replay_matches_the_prototype(label) -> None:
    eval_dir = Path(EVAL_DIR)
    turns_file, ckpt = HELDOUT[label]
    expected = _expected_sets(eval_dir / "prototype_expected_heldout.json")[label]
    _assert_parity(_replay(eval_dir / turns_file, eval_dir / ckpt), expected)


@needs_eval_dir
def test_offline_eval_reproduces_the_pinned_o2_table(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("eval_anchored_time", ROOT / "scripts" / "eval_anchored_time.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    scores = module.offline_scores(Path(EVAL_DIR))
    assert sorted(scores) == sorted(PINNED_O2)
    for label, (baseline, resolver, written, overridden, kept) in PINNED_O2.items():
        s = scores[label]
        assert (s.baseline.cell(), s.resolver.cell(), s.written.cell(), s.overridden, s.kept_inside) == (
            baseline, resolver, written, overridden, kept), label
