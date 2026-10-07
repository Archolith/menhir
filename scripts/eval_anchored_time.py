#!/usr/bin/env python3
"""Anchored-time resolver eval (P1 plan section 12).

--offline (free): rescore frozen resolver outputs through the ported Menhir code and print the
O2 table: baseline, P0-parity resolver window, and the P1 written value (midpoint overlay).
--live (operator only, PAID): run held-out turns through AnchoredTimeResolver, i.e. Menhir's
own request path, write checkpoints next to the held-outs, then score them like --offline.

Held-out sets are LME-derived and not committed; point MENHIR_ANCHORED_TIME_EVAL_DIR (or
--eval-dir) at a directory holding heldout*_turns.json, heldout*_gold.json and ckpt_*.jsonl.

    python scripts/eval_anchored_time.py --offline
    python scripts/eval_anchored_time.py --live --sets heldout,heldout2,heldout3 --runs 1 --tag p1path --max-usd 0.05
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from menhir.infrastructure.anchored_time import (
    EdgeTimeInput,
    clause_guard,
    compute,
    parse_items,
    plan_overlay,
)

EVAL_DIR_ENV = "MENHIR_ANCHORED_TIME_EVAL_DIR"
#: (turns, gold) per held-out set.
HELDOUT = {
    "heldout": ("heldout_turns.json", "heldout_gold.json"),
    "heldout2": ("heldout2_turns.json", "heldout2_gold.json"),
    "heldout3": ("heldout3_turns.json", "heldout3_gold.json"),
}
#: Frozen-prompt runs pinned in plan section 12 (O2): label -> (set, checkpoint).
FROZEN_RUNS = {
    "heldout:fix": ("heldout", "ckpt_heldout_fix.jsonl"),
    "heldout2:frozen": ("heldout2", "ckpt_heldout2_frozen.jsonl"),
    "heldout2:frozen_b": ("heldout2", "ckpt_heldout2_frozen_b.jsonl"),
    "heldout3:frozen": ("heldout3", "ckpt_heldout3_frozen.jsonl"),
}
#: gpt-6-luna Flex, USD per 1M tokens (input, output); used only for the live budget cap.
LUNA_RATES = (0.05, 0.25)


# ---------------------------------------------------------------- scoring (P0 definitions)
def _mid(w):
    s, e = w
    if s is None and e is None:
        return None
    if s is None:
        return e
    if e is None:
        return s
    return s + (e - s) / 2


def date_ok(pred_w, gold_w) -> bool:
    if pred_w is None:
        return False
    m = _mid(pred_w)
    gs, ge = gold_w
    return m is not None and (gs is None or m >= gs) and (ge is None or m <= ge)


def false_date(pred_w, ref: date) -> bool:
    """A date was asserted for a fact whose gold has none, and it is not just the speech date."""
    if pred_w is None:
        return False
    s, e = pred_w
    return not ((s is None or s <= ref) and (e is None or e >= ref))


def gold_window(g: dict):
    w = g.get("window")
    if not w:
        return None
    return (date.fromisoformat(w[0]) if w[0] else None, date.fromisoformat(w[1]) if w[1] else None)


@dataclass
class Score:
    dated_ok: int = 0
    dated_n: int = 0
    false_n: int = 0
    nodate_n: int = 0

    def add(self, window, gold_w, ref: date) -> None:
        if gold_w:
            self.dated_ok += date_ok(window, gold_w)
            self.dated_n += 1
        else:
            self.false_n += false_date(window, ref)
            self.nodate_n += 1

    def cell(self) -> str:
        return f"{self.dated_ok}/{self.dated_n}, {self.false_n}/{self.nodate_n}"


@dataclass
class SetScore:
    baseline: Score = field(default_factory=Score)
    resolver: Score = field(default_factory=Score)
    written: Score = field(default_factory=Score)
    turns: int = 0
    parse_errors: int = 0
    overridden: int = 0
    kept_inside: int = 0
    guard_drops: list[str] = field(default_factory=list)


def load_heldout(eval_dir: Path, name: str) -> list[dict]:
    turns_file, gold_file = HELDOUT[name]
    turns = json.loads((eval_dir / turns_file).read_text(encoding="utf-8"))["heldout"]
    gold = json.loads((eval_dir / gold_file).read_text(encoding="utf-8"))
    out = []
    for t in turns:
        g = gold[t["hid"]]
        if len(g) != len(t["facts"]):
            raise ValueError(f"gold/fact count mismatch for {t['hid']}")
        out.append({
            "id": t["hid"], "speech_date": t["speech_date"], "turn": t["turn"],
            "facts": [f["fact"] for f in t["facts"]],
            "uuids": [f.get("uuid") or f"{t['hid']}.{i}" for i, f in enumerate(t["facts"])],
            "baseline": [f.get("baseline_va") for f in t["facts"]],
            "gold": g,
        })
    return out


def load_checkpoint(path: Path) -> dict[str, str | None]:
    rows: dict[str, str | None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["custom_id"]] = None if r.get("error") else r.get("content")
    return rows


def _as_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    d = date.fromisoformat(str(value)[:10])
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def score_run(turns: list[dict], contents: dict[str, str | None]) -> SetScore:
    """Guard on. Resolver = P0 window metric; written = what P1 leaves in ``valid_at``."""
    out = SetScore()
    for t in turns:
        out.turns += 1
        ref = date.fromisoformat(t["speech_date"][:10])
        items: dict[int, dict] = {}
        content = contents.get(t["id"])
        try:
            if not content:
                raise ValueError("no output")
            items, _missing = parse_items(content)
            drops = clause_guard(t["turn"], t["facts"], items)
            if drops:
                out.guard_drops.append(f"{t['id']}:{drops}")
        except (ValueError, TypeError, AttributeError):
            out.parse_errors += 1
            items = {}
        inputs = [EdgeTimeInput(uuid=u, fact=f, valid_at=_as_datetime(b), invalid_at=None)
                  for u, f, b in zip(t["uuids"], t["facts"], t["baseline"])]
        results = plan_overlay(inputs, items, ref)
        out.overridden += sum(r.written for r in results)
        out.kept_inside += sum(r.reason == "graphiti_inside_window" for r in results)
        for i, g in enumerate(t["gold"]):
            if g["basis"] == "skip":
                continue
            gw = gold_window(g)
            base = inputs[i].valid_at
            base_w = (base.date(), base.date()) if base else None
            new = results[i].new_valid_at or base
            new_w = (new.date(), new.date()) if new else None
            out.baseline.add(base_w, gw, ref)
            out.resolver.add(compute(ref, items, i), gw, ref)
            out.written.add(new_w, gw, ref)
    return out


def offline_scores(eval_dir: Path) -> dict[str, SetScore]:
    scores = {}
    for label, (name, ckpt) in FROZEN_RUNS.items():
        scores[label] = score_run(load_heldout(eval_dir, name), load_checkpoint(eval_dir / ckpt))
    return scores


def print_table(scores: dict[str, SetScore]) -> None:
    print("dated in window / false dates on no-date facts (guard on)")
    print(f"{'run':<20} {'baseline':<14} {'resolver (P0)':<15} {'written (P1)':<14} overridden kept_inside parse_err")
    for label, s in scores.items():
        print(f"{label:<20} {s.baseline.cell():<14} {s.resolver.cell():<15} {s.written.cell():<14} "
              f"{s.overridden:<10} {s.kept_inside:<11} {s.parse_errors}")
        if s.guard_drops:
            print(f"{'':<20} guard drops: {', '.join(s.guard_drops)}")


# ---------------------------------------------------------------- live (operator only, paid)
class _UsageTap:
    """Records token usage of every resolver call and refuses calls past the budget."""

    def __init__(self, inner: Any, max_usd: float, rates: tuple[float, float]) -> None:
        self._inner = inner
        self.max_usd = max_usd
        self.rates = rates
        self.spent = 0.0
        self.chat = self
        self.completions = self

    async def create(self, **request: Any) -> Any:
        if self.spent >= self.max_usd:
            raise RuntimeError(f"budget cap reached (${self.spent:.4f} >= ${self.max_usd:.4f})")
        response = await self._inner.chat.completions.create(**request)
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.spent += ((getattr(usage, "prompt_tokens", 0) or 0) * self.rates[0]
                           + (getattr(usage, "completion_tokens", 0) or 0) * self.rates[1]) / 1e6
        return response


async def _live(args: argparse.Namespace, eval_dir: Path) -> int:
    from menhir.config.settings_model import MemorySettings
    from menhir.infrastructure.anchored_time_resolver import AnchoredTimeResolver
    from menhir.infrastructure.observability import build_async_openai_client
    from menhir.infrastructure.providers import ProviderConfig

    settings = MemorySettings.from_env()
    provider = ProviderConfig.for_graphiti_llm(settings)
    model = args.model or settings.anchored_time_resolver_model or provider.chat_model
    raw = build_async_openai_client(base_url=provider.base_url, api_key=provider.api_key, settings=settings)
    # Raw client, as in graphiti_client.py: the resolver adds shaping and the one backoff layer.
    tap = _UsageTap(raw, args.max_usd, LUNA_RATES)
    print(f"LIVE (paid): model={model} base={provider.base_url} cap=${args.max_usd:.2f}")
    sem = asyncio.Semaphore(args.workers)
    for run in range(1, args.runs + 1):
        for name in args.sets.split(","):
            turns = load_heldout(eval_dir, name)
            # Fresh resolver per run: its cache must not turn run 2 into a copy of run 1.
            resolver = AnchoredTimeResolver(tap, base_url=provider.base_url, model=model,
                                            timeout_s=settings.anchored_time_resolver_timeout_s, cache_size=0)
            ckpt = eval_dir / f"ckpt_{name}_{args.tag}_r{run}.jsonl"
            latencies: list[float] = []

            async def one(t: dict) -> dict:
                async with sem:
                    outcome = await resolver.resolve(t["turn"], date.fromisoformat(t["speech_date"][:10]), t["facts"])
                if outcome.latency_s is not None:
                    latencies.append(outcome.latency_s)
                content = None
                if outcome.status == "ok":
                    content = json.dumps({"facts": [dict(v, i=k) for k, v in sorted(outcome.items.items())],
                                          "missing_events": [None] * outcome.missing_events})
                return {"custom_id": t["id"], "content": content, "status": outcome.status,
                        "error": outcome.error_class or (None if content else outcome.status)}

            started = time.monotonic()
            rows = await asyncio.gather(*(one(t) for t in turns))
            ckpt.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            s = score_run(turns, {r["custom_id"]: r["content"] for r in rows})
            lat = sorted(latencies)
            p = (lambda q: f"{lat[min(len(lat) - 1, int(q * len(lat)))]:.1f}s") if lat else (lambda q: "-")
            failed = sum(r["status"] != "ok" for r in rows)
            print(f"{name} r{run}: baseline {s.baseline.cell()} | resolver {s.resolver.cell()} | written "
                  f"{s.written.cell()} | failed {failed}/{len(rows)} | p50 {p(0.5)} p95 {p(0.95)} | "
                  f"{time.monotonic() - started:.0f}s | spent ${tap.spent:.4f} -> {ckpt.name}")
            if tap.spent >= tap.max_usd:
                print("budget cap reached; stopping")
                return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--eval-dir", default=os.environ.get(EVAL_DIR_ENV, ""))
    parser.add_argument("--sets", default="heldout,heldout2,heldout3")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--tag", default="live")
    parser.add_argument("--max-usd", type=float, default=0.0)
    parser.add_argument("--model", default="")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if not args.eval_dir:
        parser.error(f"set --eval-dir or {EVAL_DIR_ENV} (held-out sets are not committed)")
    eval_dir = Path(args.eval_dir)
    if args.offline:
        print_table(offline_scores(eval_dir))
        return 0
    if not 0 < args.max_usd <= 0.10:
        parser.error("--live needs --max-usd in (0, 0.10] (plan section 12 cap)")
    return asyncio.run(_live(args, eval_dir))


if __name__ == "__main__":
    sys.exit(main())
