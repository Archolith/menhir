#!/usr/bin/env python3
"""Build labeled set 1 for the anchored-time attach check, offline (plan section 5, set 1).

Replays the 22 frozen resolver checkpoints. A candidate is a fact that plan_overlay would write
once the attachment guards (clause_guard, state_start_guard) are off. Each is labeled from gold
("right"/"wrong") and split into "tune" (heldout, b11R, b11X) and "blind" (heldout2/3/4).
No network or API calls.

    python scripts/build_attach_set.py --eval-dir <dir> --out <file.json>
"""
from __future__ import annotations

import argparse
import copy
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
import eval_anchored_time as e  # noqa: E402

e.HELDOUT.update({
    "heldout4": ("heldout4_turns.json", "heldout4_gold.json"),
    "b11R": ("b11R_turns.json", "b11R_gold.json"),
    "b11X": ("b11X_turns.json", "b11X_gold.json"),
})
TUNE = {"heldout", "b11R", "b11X"}


def run_list() -> dict[str, tuple[str, str]]:
    runs = {}
    for name in ("heldout", "heldout2", "heldout3"):
        for tag in ("p1path", "p1path120", "p1l2", "p1helper"):
            runs[f"{name}:{tag}"] = (name, f"ckpt_{name}_{tag}_r1.jsonl")
    runs.update(e.FROZEN_RUNS)
    runs["heldout4:l3_r1"] = ("heldout4", "ckpt_heldout4_l3_r1.jsonl")
    runs["heldout4:l3_r2"] = ("heldout4", "ckpt_heldout4_l3_r2.jsonl")
    for s in ("R", "X"):
        for g in ("g1a", "g1b"):
            runs[f"b11{s}:{g}"] = (f"b11{s}", f"ckpt_b11{s}_{g}_r1.jsonl")
    return runs


def turn_items(t: dict, content: str | None):
    """(raw, guarded) items; both empty when the turn has no usable output."""
    try:
        if not content:
            raise ValueError("no output")
        items, _ = e.parse_items(content)
        raw = {k: dict(v) for k, v in items.items()}
        guarded = copy.deepcopy(raw)
        e.clause_guard(t["turn"], t["facts"], guarded)
        e.state_start_guard(t["turn"], t["facts"], guarded)
        return raw, guarded
    except (ValueError, TypeError, AttributeError):
        return {}, {}


def label_of(value: date, g: dict, ref: date) -> str:
    gw = e.gold_window(g)
    w = (value, value)
    if gw:
        return "right" if e.date_ok(w, gw) else "wrong"
    return "wrong" if e.false_date(w, ref) else "right"


def build(eval_dir: Path) -> tuple[dict, list[dict]]:
    runs = run_list()
    cands: list[dict] = []
    for label, (name, ck) in runs.items():
        contents = e.load_checkpoint(eval_dir / ck)
        for t in e.load_heldout(eval_dir, name):
            ref = date.fromisoformat(t["speech_date"][:10])
            raw, guarded = turn_items(t, contents.get(t["id"]))
            inputs = [e.EdgeTimeInput(uuid=u, fact=f, valid_at=e._as_datetime(b), invalid_at=None)
                      for u, f, b in zip(t["uuids"], t["facts"], t["baseline"])]
            with_g = e.plan_overlay(inputs, guarded, ref)
            no_g = e.plan_overlay(inputs, raw, ref)
            for i, g in enumerate(t["gold"]):
                if g["basis"] == "skip" or no_g[i].reason != "written":
                    continue
                v = no_g[i].new_valid_at.date()
                base = inputs[i].valid_at
                gw = e.gold_window(g)
                bw = (base.date(), base.date()) if base else None
                base_ok = e.date_ok(bw, gw) if gw else not e.false_date(bw, ref)
                cands.append({
                    "run": label, "dataset": name, "split": "tune" if name in TUNE else "blind",
                    "turn_id": t["id"], "fact_index": i, "key": f"{t['id']}.{i}", "turn": t["turn"],
                    "speech_date": t["speech_date"], "facts": t["facts"], "fact": t["facts"][i],
                    "resolver_item": raw.get(i), "written_at_a0a9814": with_g[i].reason == "written",
                    "guard": guarded.get(i, {}).get("guard"), "guarded_reason": with_g[i].reason,
                    "value": v.isoformat(), "gold": g, "label": label_of(v, g, ref), "base_ok": base_ok,
                })
    runs_turns = {(c["run"], c["turn_id"]) for c in cands}
    meta = {
        "runs": len(runs), "turns_with_candidates": len(runs_turns), "candidates": len(cands),
        "by_split": {s: sum(c["split"] == s for c in cands) for s in ("tune", "blind")},
        "by_label": {s: sum(c["label"] == s for c in cands) for s in ("right", "wrong")},
        "guard_blocked": sum(not c["written_at_a0a9814"] for c in cands),
        "wrong_with_guards_off": sorted({c["key"] for c in cands if c["label"] == "wrong"}),
        "attach_calls": len(runs_turns),
        "distinct_turns": len({(c["dataset"], c["turn_id"]) for c in cands}),
    }
    return meta, cands


def ascii_safe(text: str) -> str:
    return str(text).encode("ascii", "replace").decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    meta, cands = build(Path(args.eval_dir))
    Path(args.out).write_text(json.dumps({"meta": meta, "candidates": cands}, indent=1), encoding="utf-8")
    print(f"runs {meta['runs']} turns_with_candidates {meta['turns_with_candidates']} "
          f"candidates {meta['candidates']} {meta['by_split']} {meta['by_label']} "
          f"guard_blocked {meta['guard_blocked']} attach_calls {meta['attach_calls']} "
          f"distinct_turns {meta['distinct_turns']}")
    for c in cands:
        if c["label"] == "wrong":
            item = c["resolver_item"] or {}
            print(f"WRONG {c['run']} {c['key']} guard={c['guard']} reason={c['guarded_reason']} "
                  f"fact={ascii_safe(c['fact'])!r} expr={ascii_safe(item.get('expression'))!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
