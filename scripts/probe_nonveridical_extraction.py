"""Probe for #283: does real ingest store facts from negated, asked, hypothetical or quoted turns?

Each corpus case is one user turn, ingested through the production composition root
(`build_memory_services` + `prepare_memory_runtime`, the path the server uses) into its own
namespace with `occurred_at` = the case's speech date. Once the episodes are READY or FAILED, the
case's stored RELATES_TO facts (with valid/invalid/expired times) and entity summaries are dumped,
and an LLM judge decides, per proposition, whether any stored fact asserts it. Each proposition
expects absent (a leak if asserted) or present (a miss if not).

Stages (each writes JSONL under --out; reruns skip finished cases):

    ingest  python scripts/probe_nonveridical_extraction.py ingest --out DIR
    judge   python scripts/probe_nonveridical_extraction.py judge  --out DIR
    report  python scripts/probe_nonveridical_extraction.py report --out DIR

`ingest` WRITES to the graph in NEO4J_URI and refuses the shared ports (7687, 7474, 7688): point it
at a throwaway container. It pins the telemetry DB under --out. LLM calls go to OpenAI Flex.
Scalar Views (the typed-scalar lane, POST /api/phase3/run) are not covered here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/nonveridical/extraction_probe_283.json"
RESERVED_PORTS = {7687, 7474, 7688}
JUDGE_MODEL = "gpt-6-luna"

JUDGE_SYSTEM = (
    "You check what a memory system stored from one user turn. You get the turn and its speech "
    "date, one proposition, and the stored facts with their time fields. Answer ASSERTED if any "
    "stored fact states the proposition as true, even in different words. Answer NOT_ASSERTED if "
    "no fact does. Rules: (1) a fact that keeps the turn's modality (the user plans to, wants to, "
    "wishes, did not, asked whether, was told that, joked that) does not assert an unhedged "
    "proposition, but it does assert a proposition that itself states the plan, wish or report; "
    "(2) a fact whose invalid_at or expired_at is on or before the speech date describes an ended "
    "state and does not assert that state still holds; (3) a future-tense fact (the user will, is "
    "going to) does not assert that the thing has already happened. "
    "Reply with one line: ASSERTED or NOT_ASSERTED, then ' | ' and the uuid of the fact that "
    "asserts it, or 'none'."
)
CATEGORIES = {"negated", "question", "counterfactual", "roleplay", "plan_modal", "reported",
              "sarcasm", "mixed", "third_party", "control"}


# ---------------------------------------------------------------- pure helpers (offline-tested)

def load_corpus(path: Path = CORPUS) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    ids = [c["id"] for c in cases]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate case ids: {dupes}")
    known = set(ids)
    for c in cases:
        missing = {"id", "category", "speech_date", "turn", "propositions"} - c.keys()
        if missing:
            raise ValueError(f"{c.get('id')}: missing {sorted(missing)}")
        if c["category"] not in CATEGORIES:
            raise ValueError(f"{c['id']}: unknown category {c['category']}")
        props = c["propositions"]
        if not props or any(p.get("expect") not in ("absent", "present") or not p.get("text")
                            for p in props):
            raise ValueError(f"{c['id']}: each proposition needs text and expect absent|present")
        if c["category"] == "control" and any(p["expect"] != "present" for p in props):
            raise ValueError(f"{c['id']}: a control's propositions must all be present")
        if c.get("pair") and c["pair"] not in known:
            raise ValueError(f"{c['id']}: pair {c['pair']} is not a case")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["speech_date"]):
            raise ValueError(f"{c['id']}: speech_date must be YYYY-MM-DD")
    return cases


def verdict_key(case_id: str, index: int) -> str:
    return f"{case_id}#{index}"


def namespace_for(run_id: str, case_id: str) -> str:
    return f"p283-{run_id}-{case_id}".lower()


def check_neo4j_uri(uri: str) -> None:
    port = urlparse(uri).port or 7687
    if port in RESERVED_PORTS:
        raise SystemExit(f"refusing NEO4J_URI {uri}: port {port} is a shared instance")


def fact_lines(row: dict[str, Any]) -> list[str]:
    lines = []
    for f in row.get("facts", []):
        times = [f"{k}={f[k]}" for k in ("valid_at", "invalid_at", "expired_at") if f.get(k)]
        lines.append(f"{f['uuid']}: {f['fact']}" + (f" [{', '.join(times)}]" if times else ""))
    lines += [f"{e['uuid']}: (entity {e['name']}) {e['summary']}"
              for e in row.get("entities", []) if e.get("summary")]
    return lines


def judge_messages(case: dict[str, Any], proposition: str,
                   row: dict[str, Any]) -> list[dict[str, str]]:
    stored = "\n".join(fact_lines(row)) or "(nothing stored)"
    user = (f"Turn (said {case['speech_date']}): {case['turn']}\n"
            f"Proposition: {proposition}\n\nStored facts:\n{stored}")
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_verdict(text: str) -> tuple[bool, str | None]:
    head, _, tail = text.strip().splitlines()[0].partition("|") if text.strip() else ("", "", "")
    head = head.strip().upper()
    if head not in ("ASSERTED", "NOT_ASSERTED"):
        raise ValueError(f"unparseable verdict: {text!r}")
    ref = tail.strip() or None
    return head == "ASSERTED", (None if ref in (None, "none") else ref)


def summarize(cases: list[dict[str, Any]], verdicts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per category: `leak` = an absent proposition was asserted; `miss` = a present one was not."""
    by_cat: dict[str, Counter] = defaultdict(Counter)
    wrong = []
    for c in cases:
        cat = by_cat[c["category"]]
        for i, p in enumerate(c["propositions"]):
            v = verdicts.get(verdict_key(c["id"], i))
            if v is None:
                cat["ungraded"] += 1
                continue
            cat[f"{p['expect']}_n"] += 1
            if v["asserted"] == (p["expect"] == "present"):
                continue
            kind = "leak" if p["expect"] == "absent" else "miss"
            cat[kind] += 1
            wrong.append({"id": c["id"], "kind": kind, "category": c["category"],
                          "turn": c["turn"], "proposition": p["text"], "fact": v.get("fact_ref")})
    return {"by_category": {k: dict(v) for k, v in sorted(by_cat.items())}, "wrong": wrong}


def read_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {r["id"]: r for r in rows}


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- live stages

_STATES = ("MATCH (e:Episodic) WHERE e.namespace = $ns AND e.processing_state IS NOT NULL "
           "RETURN e.uuid AS uuid, e.processing_state AS state")
_FACTS = ("MATCH (s:Entity)-[r:RELATES_TO {group_id:$ns}]->(t:Entity) "
          "RETURN r.uuid AS uuid, r.fact AS fact, s.name AS source, t.name AS target, "
          "toString(r.valid_at) AS valid_at, toString(r.invalid_at) AS invalid_at, "
          "toString(r.expired_at) AS expired_at ORDER BY uuid")
_ENTITIES = ("MATCH (n:Entity {group_id:$ns}) "
             "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary ORDER BY uuid")


async def _ingest_case(stack, case, ns, timeout_s):
    from menhir.domain.session import new_session
    from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

    session = f"p283-{case['id']}"
    said = f"{case['speech_date']}T12:00:00Z"
    turn_id = TurnEvidenceRepository(stack.neo4j).record_turn_evidence(
        text=case["turn"], role="user", declarant="user", namespace=ns, occurred_at=said,
        source_kind="claude_code_hook", session_id=session, prompt_id=f"p-{case['id']}",
    )["turn_id"]
    result = await stack.ingest_service.queue_episode_for_enrichment(
        case["turn"], new_session(user_id="dev", session_id=session), "user",
        namespace=ns, occurred_at=said, turn_evidence_uuid=turn_id,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    states: dict[str, str] = {}
    while loop.time() < deadline:
        rows = await asyncio.to_thread(stack.neo4j.execute, _STATES, params={"ns": ns})
        states = {r["uuid"]: str(r["state"]) for r in rows}
        if states and all(s in ("READY", "FAILED") for s in states.values()):
            break
        await asyncio.sleep(2.0)
    facts = await asyncio.to_thread(stack.neo4j.execute, _FACTS, params={"ns": ns})
    ents = await asyncio.to_thread(stack.neo4j.execute, _ENTITIES, params={"ns": ns})
    return {"id": case["id"], "namespace": ns, "episode_id": result.episode_id, "states": states,
            "facts": [dict(f) for f in facts], "entities": [dict(e) for e in ents]}


async def stage_ingest(args, cases):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ["MENHIR_MCP_TELEMETRY_DB"] = str(out / "telemetry.db")
    from menhir.config import MemorySettings
    from menhir.core import build_memory_services, prepare_memory_runtime

    settings = MemorySettings.from_env()
    check_neo4j_uri(settings.neo4j_uri)
    stack = build_memory_services(settings)
    await prepare_memory_runtime(stack)
    dump = out / "stored.jsonl"
    done = read_jsonl(dump)
    todo = [c for c in cases if c["id"] not in done]
    gate = asyncio.Semaphore(args.concurrency)

    async def one(case):
        async with gate:
            row = await _ingest_case(stack, case, namespace_for(args.run_id, case["id"]),
                                     args.timeout)
            append_jsonl(dump, row)
            print(f"{case['id']}: {Counter(row['states'].values())} facts={len(row['facts'])}",
                  flush=True)

    await asyncio.gather(*(one(c) for c in todo))


def stage_judge(args, cases):
    from openai import OpenAI

    out = Path(args.out)
    stored = read_jsonl(out / "stored.jsonl")
    path = out / "verdicts.jsonl"
    done = read_jsonl(path)
    client = OpenAI()
    for c in cases:
        if c["id"] not in stored:
            continue
        for i, p in enumerate(c["propositions"]):
            key = verdict_key(c["id"], i)
            if key in done:
                continue
            resp = client.chat.completions.create(
                model=JUDGE_MODEL, service_tier="flex",
                messages=judge_messages(c, p["text"], stored[c["id"]]))
            text = resp.choices[0].message.content or ""
            asserted, ref = parse_verdict(text)
            append_jsonl(path, {"id": key, "asserted": asserted, "fact_ref": ref, "raw": text,
                                "usage": resp.usage.model_dump() if resp.usage else None})


def stage_report(args, cases):
    out = Path(args.out)
    report = summarize(cases, read_jsonl(out / "verdicts.jsonl"))
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    print(json.dumps(report["by_category"], indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stage", choices=("ingest", "judge", "report"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-id", default="r1")
    ap.add_argument("--only", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    cases = load_corpus()
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    if args.stage == "ingest":
        asyncio.run(stage_ingest(args, cases))
    elif args.stage == "judge":
        stage_judge(args, cases)
    else:
        stage_report(args, cases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
