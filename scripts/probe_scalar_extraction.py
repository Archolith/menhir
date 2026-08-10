"""READ-ONLY probe: does typed-scalar EXTRACTION ever propose a given value?

Isolates one question, with no writes and no gate involvement:

    Does `extract_typed_scalars_once` emit a proposal for the buried update clause at all?

If it does NOT, the k-sample consistency gate is irrelevant to the miss and the target is
extraction/segmentation. If it DOES, extraction is fine and the gate is the whole story.

Faithfulness: episodes are loaded and formatted exactly as the scheduler does
(`personal_memory_queries.load_user_episodes` + `scheduler_tasks._build_episodes`):
user-turn episodes only, oldest first, `user:` prefix stripped, body truncated to 2000 chars,
bodies shorter than 8 chars skipped, content rendered as `[YYYY-MM-DD] <body>`.

Writes nothing to the graph and calls no menhir persistence path.

Usage:
    python scripts/probe_scalar_extraction.py --ns 031748ae --expect 5 --attr team_size
    python scripts/probe_scalar_extraction.py --ns a2f3aa27 --expect 1300 --attr followers -k 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass

from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

from menhir.services.typed_scalar_perception import (
    TYPED_SCALAR_SYSTEM_PROMPT,
    extract_typed_scalars_once,
    gate_typed_scalars,
)

_PREFIX = "lme-scalar-ku-20260722-"


@dataclass
class _Ep:
    """Minimal stand-in for perception.Episode -- the extractor only reads .uuid/.content."""

    uuid: str
    content: str


# Topic-shift markers: phrases that introduce an ASIDE carrying new durable state, as opposed to
# the CORRECTION markers ("actually", "instead", "no longer") and state-change verbs the plan's
# Stage A already lists. Mined from the LME corpus (1690 user episodes) rather than guessed.
#
# Corpus-attested, with occurrence count and the share whose clause carries a digit:
#     by the way   376  28%      speaking of  113  19%
#     also,         59  22%      oh, and       17  53%
# Deliberately EXCLUDED despite occurring: "i also" (31, 0% digit) and "anyway," (18, 0% digit) --
# they never introduce a scalar here, so they would add fragmentation for no possible gain.
#
# The remainder do not occur in this synthetic corpus at all. They are kept for robustness on real
# user prose, which has a much wider marker vocabulary; under the value gate an unfired marker costs
# exactly nothing, so breadth here is free while precision is enforced downstream.
_TOPIC_SHIFT = re.compile(
    r"(?i)(?<![A-Za-z])(?:"
    # --- corpus-attested
    r"by the way|speaking of|oh,? and|also,"
    # --- plausible in real prose, unattested in LME (free under the value gate)
    r"|btw|incidentally|on another note|on a different note|as an aside|side note"
    r"|forgot to mention|almost forgot|meant to mention|meant to ask|should mention"
    r"|worth mentioning|come to think of it|just remembered|while i'?m at it"
    r"|one more thing|another thing|quick update|p\.s\.|fyi"
    r")(?![A-Za-z])"
)


_HAS_VALUE = re.compile(r"\d")


def _segment(eps: list[_Ep], *, require_value: bool = False,
             sentences: bool = False) -> tuple[list[_Ep], int]:
    """Split each episode at topic-shift markers so a trailing aside becomes its OWN episode.

    This is the treatment arm for `menhir-adaptive-claim-segmentation-plan.md`: a narrow,
    marker-triggered boundary, NOT the blind sentence splitting that plan already rejected
    (24 turns -> ~190 episodes with negligible gain).

    Pieces get distinct synthetic uuids (`<uuid>#<i>`), matching that plan's "child episodes"
    design and keeping `source_key` (episode_uuid + span offsets) unique per piece. A marker at
    offset 0 is ignored -- the whole turn is already the aside, so splitting would only produce an
    empty head. Returns (episodes, number_of_splits_applied).
    """
    out: list[_Ep] = []
    splits = 0
    for e in eps:
        m = re.match(r"^(\[\d{4}-\d{2}-\d{2}\]\s*)(.*)$", e.content, re.S)
        prefix, body = (m.group(1), m.group(2)) if m else ("", e.content)
        if sentences:
            # MARKER-FREE arm. Measured ceiling of the marker approach: only 38% of non-initial
            # value-bearing clauses in this corpus are introduced by ANY topic-shift marker, so a
            # marker-triggered split can never reach the other 62% ("I lead a team of 4 engineers").
            # Cut at every non-initial sentence boundary instead; the value gate below keeps this far
            # cheaper than the blind sentence splitting this plan already rejected.
            cuts = [mm.end() for mm in re.finditer(r"(?<=[.!?])\s+", body) if mm.end() > 0]
        else:
            cuts = [mm.start() for mm in _TOPIC_SHIFT.finditer(body) if mm.start() > 0]
        if not cuts:
            out.append(e)
            continue
        pieces, prev = [], 0
        for c in cuts:
            pieces.append(body[prev:c].strip())
            prev = c
        pieces.append(body[prev:].strip())
        pieces = [p for p in pieces if len(p) >= 8]   # same floor as _build_episodes
        if len(pieces) < 2:
            out.append(e)
            continue
        # Targeted mode: a topic-shift marker is common in ordinary prose ("by the way" fires 8x in
        # one 19-episode namespace), so splitting on EVERY occurrence over-fragments and measurably
        # HURTS perception. Only pay the split when the aside actually carries a scalar payload.
        if require_value and not any(_HAS_VALUE.search(p) for p in pieces[1:]):
            out.append(e)
            continue
        splits += 1
        for j, p in enumerate(pieces):
            out.append(_Ep(uuid=f"{e.uuid}#{j}", content=f"{prefix}{p}"))
    return out, splits


def _load_episodes(driver, ns: str) -> list[_Ep]:
    """Mirror load_user_episodes + _build_episodes exactly."""
    with driver.session() as s:
        rows = list(s.run(
            """
            MATCH (e:Episodic {group_id: $ns})
            WHERE e.content STARTS WITH 'user:'
            RETURN e.uuid AS uuid, toString(e.valid_at) AS valid_at, e.content AS content
            ORDER BY e.valid_at
            LIMIT 500
            """,
            ns=ns,
        ))
    eps: list[_Ep] = []
    for r in rows:
        raw = str(r["content"] or "")
        body = (raw[len("user:"):] if raw[:5].lower() == "user:" else raw).strip()[:2000]
        if len(body) < 8:
            continue
        when = str(r["valid_at"] or "")[:10]
        eps.append(_Ep(uuid=str(r["uuid"]), content=f"[{when}] {body}"))
    return eps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ns", required=True, help="namespace suffix, e.g. a2f3aa27")
    ap.add_argument("--expect", required=True, help="the value that SHOULD be proposed, e.g. 1300")
    ap.add_argument("--attr", default="", help="expected attribute, e.g. followers (reporting only)")
    ap.add_argument("-k", type=int, default=1, help="extraction samples (default 1; >1 also gates)")
    # Keep the probe's defaults aligned with the current production consolidation settings.
    ap.add_argument("--temp", type=float, default=0.7, help="LLM temperature (production default 0.7)")
    ap.add_argument("--max-tokens", type=int, default=2048,
                    help="max completion tokens (production default 2048)")
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="lmedata123")
    ap.add_argument("--model", default="", help="override OPENAI_CHAT_MODEL")
    ap.add_argument(
        "--show-raw",
        action="store_true",
        help="print each raw model response for parser diagnostics (may include source text)",
    )
    ap.add_argument(
        "--production-gate",
        action="store_true",
        help=(
            "report decisions with the candidate run's 2/3 threshold and attribute/scope/subject "
            "reconciliation; span grounding remains the service's always-on internal invariant"
        ),
    )
    ap.add_argument(
        "--only-episode",
        type=int,
        default=None,
        help="probe one zero-based episode after optional dedupe/segmentation (read-only isolation)",
    )
    ap.add_argument("--segment", action="store_true",
                    help="TREATMENT ARM: split episodes at topic-shift markers (by the way, oh and, "
                         "incidentally, ...) so a trailing aside becomes its own episode")
    ap.add_argument("--segment-valued", action="store_true", dest="segment_valued",
                    help="TREATMENT ARM (targeted): like --segment, but only split when the aside "
                         "actually contains a scalar payload (a digit)")
    ap.add_argument("--dedupe", action="store_true",
                    help="Collapse exact-duplicate episode bodies (keeping the earliest) before "
                         "extraction. 43%% of LME user episodes are exact duplicates; each copy is a "
                         "distinct source_key, so duplicates scatter k-sample votes and can make a "
                         "perfectly-perceived claim unprovable at threshold=1.0")
    ap.add_argument("--segment-sentences", action="store_true", dest="segment_sentences",
                    help="TREATMENT ARM (marker-free): split at every non-initial sentence boundary "
                         "whose tail carries a digit -- reaches the 62%% of value-bearing asides that "
                         "no topic-shift marker introduces")
    args = ap.parse_args()

    load_dotenv(override=True)  # inherited env must not beat the project's .env
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set (checked environment and .env)", file=sys.stderr)
        return 2
    model = args.model or os.environ.get("OPENAI_CHAT_MODEL") or "gpt-4o-mini"

    # Accept a full namespace from any run generation. The historical default prefix remains useful
    # for short fixture ids, but newer LongMemEval runs use names such as ``lme-<question-id>``.
    ns = args.ns if args.ns.startswith("lme-") else _PREFIX + args.ns
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        episodes = _load_episodes(driver, ns)
    finally:
        driver.close()

    if not episodes:
        print(f"no user episodes for {ns}", file=sys.stderr)
        return 2

    n_before = len(episodes)
    if args.dedupe:
        seen: set[str] = set()
        deduped: list[_Ep] = []
        for e in episodes:
            body = e.content.split("] ", 1)[-1]
            if body in seen:
                continue
            seen.add(body)
            deduped.append(e)
        print(f"dedupe          {len(episodes)} -> {len(deduped)} episodes "
              f"({len(episodes) - len(deduped)} duplicate copies removed)")
        episodes = deduped
    splits = 0
    if args.segment or args.segment_valued or args.segment_sentences:
        episodes, splits = _segment(
            episodes,
            require_value=args.segment_valued or args.segment_sentences,
            sentences=args.segment_sentences,
        )
    if args.only_episode is not None:
        if not 0 <= args.only_episode < len(episodes):
            print(
                f"--only-episode {args.only_episode} outside 0..{len(episodes) - 1}",
                file=sys.stderr,
            )
            return 2
        episodes = [episodes[args.only_episode]]

    total_chars = sum(len(e.content) for e in episodes)
    print(f"namespace       {ns}")
    _arm = ("SENTENCE-VALUED (treatment)" if args.segment_sentences
            else "SEGMENTED-VALUED (treatment)" if args.segment_valued
            else "SEGMENTED (treatment)" if args.segment else "CONTROL")
    print(f"arm             {_arm}")
    if splits or args.segment or args.segment_valued or args.segment_sentences:
        print(f"segmentation    {splits} episode(s) split -> {n_before} -> {len(episodes)} episodes "
              f"({(len(episodes) / n_before - 1) * 100:+.0f}% inflation)")
    print(f"episodes        {len(episodes)}  ({total_chars} chars, ~{total_chars // 4} tokens)")
    print(f"model           {model}  temp={args.temp}  max_tokens={args.max_tokens}  k={args.k}")
    print(f"looking for     value={args.expect!r}" + (f" attribute={args.attr!r}" if args.attr else ""))

    hits = [(i, e) for i, e in enumerate(episodes) if args.expect in e.content]
    print(f"episodes literally containing {args.expect!r}: "
          f"{[i for i, _ in hits] if hits else 'NONE'}")
    for i, e in hits:
        j = e.content.find(args.expect)
        print(f"    [{i}] ...{e.content[max(0, j - 100):j + 60]}...")
    print()

    client = OpenAI(api_key=api_key)
    calls = {"n": 0}

    truncated = {"n": 0}
    completion_tokens: list[int] = []
    raw_completions: list[str] = []

    def llm_complete(system: str, user: str) -> str:
        calls["n"] += 1
        resp = client.chat.completions.create(
            model=model,
            temperature=args.temp,
            max_tokens=args.max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        choice = resp.choices[0]
        # Record the ACTUAL completion size, not just whether it hit the cap. Sizing a budget needs
        # the distribution of what responses really cost; "did it truncate at 512" only tells you
        # about 512. Run with a generous --max-tokens to observe the true demand.
        used = int(getattr(resp.usage, "completion_tokens", 0) or 0)
        completion_tokens.append(used)
        # `length` means the JSON array was cut off mid-emission; _parse_json_array then silently
        # drops the tail. Surfacing it separates "model did not propose X" from "X was truncated".
        if choice.finish_reason == "length":
            truncated["n"] += 1
            print(f"    !! completion hit max_tokens={args.max_tokens} "
                  f"(finish_reason=length) -- JSON array truncated, tail proposals lost")
        else:
            print(f"    completion_tokens={used}  (headroom {args.max_tokens - used})")
        raw = choice.message.content or ""
        raw_completions.append(raw)
        return raw

    assert TYPED_SCALAR_SYSTEM_PROMPT  # the extractor supplies it; referenced for clarity

    samples = []
    sample_drops: list[Counter[str]] = []
    for _ in range(args.k):
        drops: Counter[str] = Counter()
        samples.append(
            extract_typed_scalars_once(
                episodes,
                llm_complete,
                on_drop=lambda reason, counts=drops: counts.update((reason,)),
            )
        )
        sample_drops.append(drops)

    found_any = False
    per_sample_hit: list[bool] = []
    for si, props in enumerate(samples):
        print(f"--- sample {si + 1}/{args.k}: {len(props)} proposal(s)")
        if sample_drops[si]:
            print(f"    parser_drops={dict(sample_drops[si])}")
        else:
            print("    parser_drops=none")
        if args.show_raw:
            print("    raw_response:")
            for line in raw_completions[si].splitlines() or [""]:
                print(f"        {line}")
        hit = any((p.normalized_value or "").strip() == args.expect for p in props)
        per_sample_hit.append(hit)
        for p in props:
            mark = ""
            if args.expect in (p.normalized_value or "") or args.expect in (p.stated_span or ""):
                mark = "   <== MATCHES EXPECTED VALUE"
                found_any = True
            print(f"    {p.subject_text!r} {p.attribute}={p.normalized_value!r} "
                  f"op={p.operation} kind={p.value_kind} unit={p.unit!r} when={p.when!r}{mark}")
            print(f"        span={p.stated_span!r}")

    print()
    if args.k > 1:
        gate_kwargs = (
            {
                "threshold": 2 / 3,
                "reconcile_attribute": True,
                "reconcile_scope": True,
                "reconcile_subject": True,
                "align_spans": True,
            }
            if args.production_gate
            else {"threshold": 1.0}
        )
        gate_label = "candidate 2/3 + identity reconciliation" if args.production_gate else "unanimous"
        print(f"--- gate decisions (k>1; {gate_label})")
        for dcn in gate_typed_scalars(samples, **gate_kwargs):
            star = "" if not (args.expect in (getattr(dcn.proposal, "normalized_value", "") or "")) else "  <== EXPECTED"
            print(f"    committed={dcn.committed} veto={dcn.veto} agreement={dcn.agreement:.2f} "
                  f"dist={dcn.distribution}{star}")
            print(f"        reason: {dcn.reason}")
        print()

    # PRIMARY METRIC. Per-sample perception rate is what an arm comparison must be judged on:
    # commit/no-commit at k=3 is far too noisy to separate a real effect from sampling variance
    # (measured: the same namespace gave 3/3, then 0/3, then 11/12 across runs).
    n_hit = sum(per_sample_hit)
    mean_props = sum(len(s) for s in samples) / max(1, len(samples))
    print("=" * 72)
    print(f"PER-SAMPLE PERCEPTION RATE for value {args.expect!r}: {n_hit}/{args.k} "
          f"({100 * n_hit / max(1, args.k):.0f}%)   [arm: "
          f"{_arm.split()[0]}]")
    print(f"mean proposals/sample: {mean_props:.1f}   (noise/inflation proxy)")
    print("=" * 72)
    if found_any:
        print(f"VERDICT: extraction DID propose {args.expect!r}. Extraction is not the miss; "
              "look at the gate / binding / fold.")
    else:
        print(f"VERDICT: extraction NEVER proposed {args.expect!r} in {args.k} sample(s). "
              "The gate is irrelevant to this miss -- the target is extraction (segmentation/prompt).")
    print(f"llm calls: {calls['n']}   truncated completions: {truncated['n']}/{calls['n']}")
    if completion_tokens:
        _ct = sorted(completion_tokens)
        print(f"COMPLETION TOKENS: min={_ct[0]} p50={_ct[len(_ct) // 2]} max={_ct[-1]} "
              f"  (configured probe cap is {args.max_tokens})")
    if truncated["n"]:
        print("WARNING: at least one sample was truncated at max_tokens, so absent proposals may be "
              "a token-budget artifact rather than a perception miss.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
