#!/usr/bin/env python3
"""Anchored-time attach check eval (plan menhir-anchored-time-attachment-plan.md P3).

--call (PAID, operator only): one blind attach call per distinct turn of each named set, through
AnchoredTimeAttacher (Menhir's request path). Writes attach_<set>_<tag>.jsonl to the eval dir.
--score (free): replays the 22 frozen resolver runs and set 2 through plan_overlay, paired arms on
the SAME resolver output: A = regex guards (a0a9814), D = no guards, B = no guards + attach
(the P5 target), C = guards + attach (what PR #275 ships). --fake agree|reject scores without any
attach checkpoint, to check the scorer: agree must give B == D and C == A, reject no writes.

Sets: heldout, b11R, b11X (tune); heldout2, heldout3, heldout4 (blind); set2 (the adversarial
fixture, tune); set2fresh (its blind third, --set2fresh path).

    python scripts/eval_attach.py --score --fake agree --eval-dir <dir>
    python scripts/eval_attach.py --call --sets heldout,b11R,b11X,set2 --tag t1 --max-usd 0.10 --eval-dir <dir>
    python scripts/eval_attach.py --score --tag t1 --eval-dir <dir>
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
import build_attach_set as bas  # noqa: E402
import eval_anchored_time as e  # noqa: E402

from menhir.infrastructure.anchored_time import DATED_BASES  # noqa: E402
from menhir.infrastructure.anchored_time_attach import agrees, decide_all  # noqa: E402

DATASETS = ("heldout", "b11R", "b11X", "heldout2", "heldout3", "heldout4")
SET2 = ROOT / "tests" / "fixtures" / "anchored_time" / "attach_adversarial.json"
SET2_FRESH = ROOT / "tests" / "fixtures" / "anchored_time" / "attach_adversarial_fresh.json"
FAKE_RELATION = {"state": "start", "point_event": "occurrence", "plan": "plan"}


def set2_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"] if path.exists() else []


def requests_for(name: str, eval_dir: Path, set2fresh: Path) -> list[dict]:
    """(custom_id, turn, speech_date, facts) per distinct attach call of a set."""
    if name in DATASETS:
        return [{"id": t["id"], "turn": t["turn"], "speech_date": t["speech_date"][:10], "facts": t["facts"]}
                for t in e.load_heldout(eval_dir, name)]
    path = SET2 if name == "set2" else set2fresh
    return [{"id": c["id"], "turn": c["turn"], "speech_date": c["speech_date"], "facts": c["facts"]}
            for c in set2_cases(path)]


# ---------------------------------------------------------------- paid calls
async def _call(args: argparse.Namespace, eval_dir: Path) -> int:
    from menhir.config.settings_model import MemorySettings
    from menhir.infrastructure.anchored_time_attach_client import AnchoredTimeAttacher
    from menhir.infrastructure.observability import build_async_openai_client
    from menhir.infrastructure.providers import ProviderConfig

    settings = MemorySettings.from_env()
    provider = ProviderConfig.for_graphiti_llm(settings)
    model = args.model or settings.anchored_time_attach_model or settings.anchored_time_resolver_model \
        or provider.chat_model
    raw = build_async_openai_client(base_url=provider.base_url, api_key=provider.api_key, settings=settings)
    tap = e._UsageTap(raw, args.max_usd, e.LUNA_RATES)
    attacher = AnchoredTimeAttacher(tap, base_url=provider.base_url, model=model,
                                    timeout_s=settings.anchored_time_resolver_timeout_s, cache_size=0)
    print(f"ATTACH LIVE (paid): model={model} base={provider.base_url} prompt={attacher.prompt_version} "
          f"cap=${args.max_usd:.2f}")
    sem = asyncio.Semaphore(args.workers)
    for name in args.sets.split(","):
        reqs = requests_for(name, eval_dir, Path(args.set2fresh))
        out = eval_dir / f"attach_{name}_{args.tag}.jsonl"
        if out.exists():
            print(f"{out.name} exists; refusing to overwrite (pick a new --tag)")
            return 1

        async def one(r: dict) -> dict:
            async with sem:
                o = await attacher.attach(r["turn"], date.fromisoformat(r["speech_date"]), r["facts"])
            return {"custom_id": r["id"], "status": o.status, "error": o.error_class or None,
                    "latency_s": o.latency_s, "prompt": attacher.prompt_version, "model": model,
                    "answers": {str(k): v for k, v in (o.answers or {}).items()} if o.status == "ok" else None}

        started = time.monotonic()
        rows = await asyncio.gather(*(one(r) for r in reqs))
        out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        bad = [r for r in rows if r["status"] != "ok"]
        print(f"{name}: calls {len(rows)} failed {len(bad)} {sorted({r['status'] for r in bad})} "
              f"{time.monotonic() - started:.0f}s spent ${tap.spent:.4f} -> {out.name}")
        if any(r["error"] in ("RateLimitError", "insufficient_quota") for r in bad):
            print("rate limit or quota error; stopping")
            return 1
        if tap.spent >= tap.max_usd:
            print("budget cap reached; stopping")
            return 1
    return 0


# ---------------------------------------------------------------- scoring
def load_answers(eval_dir: Path, name: str, tag: str) -> dict[str, dict | None] | None:
    path = eval_dir / f"attach_{name}_{tag}.jsonl"
    if not path.exists():
        return None
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["custom_id"]] = ({int(k): v for k, v in r["answers"].items()}
                                   if r["status"] == "ok" and r["answers"] is not None else None)
    return out


def fake_answers(items: dict[int, dict], mode: str) -> dict[int, dict]:
    if mode == "reject":
        return {}
    return {i: {"relation": FAKE_RELATION.get(str(it.get("kind")), "none"), "expression": it.get("expression")}
            for i, it in items.items() if it.get("expression")}


def arms(turn: str, inputs, raw, guarded, ref: date, answers):
    """A, D, B, C results (see module docstring). ``answers`` None = failed call (V3)."""
    a = e.plan_overlay(inputs, guarded, ref)
    d = e.plan_overlay(inputs, raw, ref)
    b = e.plan_overlay(inputs, raw, ref, attach=decide_all(turn, [r for r in d if r.written], answers))
    c = e.plan_overlay(inputs, guarded, ref, attach=decide_all(turn, [r for r in a if r.written], answers))
    return {"A": a, "D": d, "B": b, "C": c}


def sign_test(x: int, y: int) -> float:
    """Exact two-sided binomial test on discordant pairs x vs y (p = 0.5)."""
    n = x + y
    if n == 0:
        return 1.0
    k = min(x, y)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def score_runs(eval_dir: Path, tag: str, fake: str) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    calls = {"turns_with_candidates": 0, "failed": 0, "missing_checkpoint": set()}
    answers_by_set = {n: load_answers(eval_dir, n, tag) for n in DATASETS} if not fake else {}
    for label, (name, ck) in bas.run_list().items():
        answers = answers_by_set.get(name)
        if not fake and answers is None:
            calls["missing_checkpoint"].add(name)
            continue
        contents = e.load_checkpoint(eval_dir / ck)
        for t in e.load_heldout(eval_dir, name):
            ref = date.fromisoformat(t["speech_date"][:10])
            raw, guarded = bas.turn_items(t, contents.get(t["id"]))
            inputs = [e.EdgeTimeInput(uuid=u, fact=f, valid_at=e._as_datetime(b), invalid_at=None)
                      for u, f, b in zip(t["uuids"], t["facts"], t["baseline"])]
            ans = fake_answers(raw, fake) if fake else answers.get(t["id"])
            res = arms(t["turn"], inputs, raw, guarded, ref, ans)
            if any(r.written for r in res["D"]):
                calls["turns_with_candidates"] += 1
                calls["failed"] += ans is None
            for i, g in enumerate(t["gold"]):
                if g["basis"] == "skip":
                    continue
                gw = e.gold_window(g)
                base = inputs[i].valid_at
                row = {"run": label, "set": name, "split": "tune" if name in bas.TUNE else "blind",
                       "key": f"{t['id']}.{i}", "fact": t["facts"][i], "turn": t["turn"], "gold": g,
                       "item": raw.get(i), "answer": (ans or {}).get(i), "failed": ans is None}
                for arm, results in res.items():
                    r = results[i]
                    new = r.new_valid_at or base
                    w = (new.date(), new.date()) if new else None
                    ok = e.date_ok(w, gw) if gw else not e.false_date(w, ref)
                    row[arm] = {"ok": ok, "written": r.written, "reason": r.reason,
                                "value": r.new_valid_at.date().isoformat() if r.written else None}
                bw = (base.date(), base.date()) if base else None
                row["base_ok"] = e.date_ok(bw, gw) if gw else not e.false_date(bw, ref)
                rows.append(row)
    return rows, calls


def score_set2(path: Path, answers: dict | None, fake: str) -> list[dict]:
    out = []
    for c in set2_cases(path):
        ref = date.fromisoformat(c["speech_date"])
        raw = {int(k): dict(v) for k, v in c["items"].items()}
        guarded = copy.deepcopy(raw)
        e.clause_guard(c["turn"], c["facts"], guarded)
        e.state_start_guard(c["turn"], c["facts"], guarded)
        said = datetime(ref.year, ref.month, ref.day, 12, tzinfo=timezone.utc)  # Graphiti's default
        inputs = [e.EdgeTimeInput(uuid=f"e{i}", fact=f, valid_at=said, invalid_at=None)
                  for i, f in enumerate(c["facts"])]
        ans = fake_answers(raw, fake) if fake else (answers or {}).get(c["id"])
        res = arms(c["turn"], inputs, raw, guarded, ref, ans)
        row = {"id": c["id"], "label": c["label"], "unsure": c["why"].startswith("[UNSURE"),
               "turn": c["turn"], "fact": c["facts"][c["target"]], "item": raw.get(c["target"]),
               "answer": (ans or {}).get(c["target"]), "failed": ans is None}
        for arm, results in res.items():
            r = results[c["target"]]
            if not r.written:
                verdict = "miss" if c["label"] == "write" else "ok"
            elif c["label"] == "no_write":
                verdict = "WRONG"
            else:
                lo, hi = map(date.fromisoformat, c["correct_window"])
                verdict = "ok" if lo <= r.new_valid_at.date() <= hi else "WRONG"
            row[arm] = {"verdict": verdict, "reason": r.reason}
        out.append(row)
    return out


def independence(rows: list[dict]) -> dict:
    """How often the blind check agrees with the resolver, over facts with an answer."""
    n = both = agree_dated = same_phrase = 0
    blocked = blocked_agree = 0
    for r in rows:
        if r["failed"]:
            continue
        item, ans = r["item"] or {}, r["answer"] or {}
        res_dated = str(item.get("basis")) in DATED_BASES and bool(item.get("expression"))
        chk_dated = ans.get("relation") in ("start", "occurrence", "plan") and bool(ans.get("expression"))
        n += 1
        agree_dated += res_dated == chk_dated
        if res_dated and chk_dated:
            both += 1
            same_phrase += agrees(r["turn"], item.get("expression"), ans.get("expression"))
        if r["D"]["written"] and not r["A"]["written"]:
            blocked += 1
            blocked_agree += r["B"]["written"]
    return {"facts": n, "dated_agree": agree_dated, "both_dated": both, "same_phrase": same_phrase,
            "regex_blocked": blocked, "regex_blocked_but_attach_agrees": blocked_agree}


def report(rows: list[dict], calls: dict, s2: dict[str, list[dict]], out: Path | None) -> None:
    print(f"attach calls: turns with candidates {calls['turns_with_candidates']} (run-turns), "
          f"failed {calls['failed']} (V3, counted in every denominator below)")
    if calls["missing_checkpoint"]:
        print(f"MISSING attach checkpoints for {sorted(calls['missing_checkpoint'])}: their runs are not scored")
    for split in ("tune", "blind", "all"):
        sel = [r for r in rows if split == "all" or r["split"] == split]
        if not sel:
            continue
        cells = []
        for arm in ("A", "D", "B", "C"):
            w = [r for r in sel if r[arm]["written"]]
            cells.append(f"{arm}: ok {sum(r[arm]['ok'] for r in sel)}/{len(sel)} writes {len(w)} "
                         f"wrong {sum(not r[arm]['ok'] for r in w)} "
                         f"harmful {sum(r['base_ok'] and not r[arm]['ok'] for r in w)}")
        print(f"[{split}] " + " | ".join(cells))
        for x, y in (("B", "A"), ("C", "A")):
            xo = sum(r[x]["ok"] and not r[y]["ok"] for r in sel)
            yo = sum(r[y]["ok"] and not r[x]["ok"] for r in sel)
            print(f"[{split}] paired {x} vs {y}: {x}-only ok {xo}, {y}-only ok {yo}, exact p={sign_test(xo, yo):.3g}")
    print("independence (run-facts with an answer):", independence(rows))
    for name, cases in s2.items():
        for subset in ("all", "sure", "unsure"):
            sel = [c for c in cases if subset == "all" or c["unsure"] == (subset == "unsure")]
            if not sel:
                continue
            parts = []
            for arm in ("A", "D", "B", "C"):
                v = [c[arm]["verdict"] for c in sel]
                parts.append(f"{arm}: WRONG {v.count('WRONG')} miss {v.count('miss')} ok {v.count('ok')}")
            print(f"{name} [{subset} n={len(sel)} failed={sum(c['failed'] for c in sel)}] " + " | ".join(parts))
    if out:
        out.write_text(json.dumps({"rows": rows, "set2": s2}, indent=0), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--call", action="store_true")
    mode.add_argument("--score", action="store_true")
    parser.add_argument("--eval-dir", default=os.environ.get(e.EVAL_DIR_ENV, ""))
    parser.add_argument("--sets", default="heldout,b11R,b11X,set2")
    parser.add_argument("--tag", default="")
    parser.add_argument("--fake", choices=("", "agree", "reject"), default="")
    parser.add_argument("--set2fresh", default=str(SET2_FRESH))
    parser.add_argument("--max-usd", type=float, default=0.0)
    parser.add_argument("--model", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if not args.eval_dir:
        parser.error(f"set --eval-dir or {e.EVAL_DIR_ENV}")
    eval_dir = Path(args.eval_dir)
    if args.call:
        if not args.tag:
            parser.error("--call needs --tag")
        if not 0 < args.max_usd <= 0.10:
            parser.error("--call needs --max-usd in (0, 0.10] (plan section 5 cap)")
        return asyncio.run(_call(args, eval_dir))
    if not (args.tag or args.fake):
        parser.error("--score needs --tag or --fake")
    rows, calls = score_runs(eval_dir, args.tag, args.fake)
    s2 = {}
    for name, path in (("set2", SET2), ("set2fresh", Path(args.set2fresh))):
        answers = None if args.fake else load_answers(eval_dir, name, args.tag)
        if path.exists() and (args.fake or answers is not None):
            s2[name] = score_set2(path, answers, args.fake)
    report(rows, calls, s2, Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
