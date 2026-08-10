#!/usr/bin/env python3
"""Phase 3 real-data validation: hook triage -> :TurnEvidence -> consolidation -> Views -> recall.

FREEZE THE PRODUCER AND MEASURE. This exercises the SELECTIVE-capture pipeline end to end on the
REAL Neo4j graph and the REAL personal-memory LLM (mirroring the prod scheduler wiring:
`make_sync_chat(model=personal_memory_chat_model)` + `make_view_embedder`, k=3, all bias guards
pinned on), isolated to a dedicated namespace so it never touches real user data.

It answers the validation questions directly:
  * how many prompts pass the producer's deterministic triage (and which junk is dropped)
  * how many are stored as :TurnEvidence
  * whether Phase 3 selects the namespace (dirty detection)
  * how many Views are written / how many measures abstain
  * re-run duplicate writes (watermark debounce + forced-refold write-idempotency)
  * whether new evidence re-dirties the namespace
  * whether recall gained a durable answer it did not have before
  * supersession / currentness behaviour on a correction ("actually it is 20, not 25")

The four minimum cases (verbatim) are the user prompts a UserPromptSubmit hook would observe:
  1. "I have 25 movies on my watch list."           -> stated-measure View
  2. "I bought one bike for $50 and another for $75." -> base events -> fold-derived SUM View
  3. "Actually it is 20, not 25."                     -> supersession / currentness
  4. "write the handoff"                              -> junk: not stored, not processed

Isolation: everything lives in namespace 'phase3-validation'. The script purges that namespace on
start (re-runnable) and, unless --keep is passed, on exit. It NEVER processes any other namespace
(consolidation is always called with an explicit namespaces=['phase3-validation']).

Run:  .venv/Scripts/python.exe scripts/validate_phase3_realdata.py [--keep] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NS = "phase3-validation"
SESSION = f"phase3-val-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
SOURCE = "perception-phase3-validation"
K = 3

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "scripts" / "hooks" / "menhir_turn_evidence.py"


# --------------------------------------------------------------------------- hook (producer) import


def _load_hook_module() -> Any:
    """Load the real, stdlib-only producer hook so triage/payload logic is exercised, not re-implemented."""
    spec = importlib.util.spec_from_file_location("menhir_turn_evidence_hook", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- namespace isolation


def purge_namespace(neo4j: Any) -> dict[str, int]:
    """Delete all validation artifacts in NS so the script is re-runnable and leaves no residue."""
    te = neo4j.execute(
        "MATCH (t:TurnEvidence {namespace:$ns}) DETACH DELETE t RETURN count(t) AS c",
        params={"ns": NS},
    )
    wm = neo4j.execute(
        "MATCH (w:ConsolidationWatermark {group_id:$ns}) DETACH DELETE w RETURN count(w) AS c",
        params={"ns": NS},
    )
    views = neo4j.execute(
        "MATCH (n:Entity {group_id:$ns}) WHERE n.view_kind IS NOT NULL OR n.is_view = true "
        "DETACH DELETE n RETURN count(n) AS c",
        params={"ns": NS},
    )
    return {
        "turn_evidence": int(te[0]["c"]) if te else 0,
        "watermarks": int(wm[0]["c"]) if wm else 0,
        "views": int(views[0]["c"]) if views else 0,
    }


def count_evidence(neo4j: Any) -> int:
    rows = neo4j.execute("MATCH (t:TurnEvidence {namespace:$ns}) RETURN count(t) AS c", params={"ns": NS})
    return int(rows[0]["c"]) if rows else 0


# --------------------------------------------------------------------------- capture via live endpoint


def post_evidence(payload: dict, *, port: int, agent_key: str) -> dict[str, Any]:
    """POST one candidate through the LIVE /api/turn-evidence route (routes.py + auth + repo + Neo4j)."""
    url = f"http://127.0.0.1:{port}/api/turn-evidence"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if agent_key:
        headers["Authorization"] = f"Bearer {agent_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            return {"status": resp.status, "body": json.loads(resp.read() or b"{}")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "error": exc.read().decode("utf-8", "replace")[:300]}


# --------------------------------------------------------------------------- consolidation


async def run_consolidation(graph_adapter: Any, chat: Any, embed: Any) -> dict[str, object]:
    """Invoke the REAL prod task, isolated to NS (explicit namespaces = never touches other data)."""
    from menhir.services import scheduler_tasks

    return await scheduler_tasks.consolidate_personal_memory(
        graph_adapter,
        llm_complete=chat,
        embed=embed,
        namespaces=[NS],
        k=K,
        source=SOURCE,
    )


# --------------------------------------------------------------------------- helpers


def list_ns_counters(graph_adapter: Any) -> list[dict[str, Any]]:
    """USER-facing counter Views only. Excludes `subject='perception'` abstention RECEIPTS (run-level
    observability, name_embedding=None, out of recall) — those carry wall-clock valid_at so they must
    not enter the idempotency comparison or the recall-improvement count."""
    return sorted(
        (r for r in graph_adapter.list_counters(namespace=NS, limit=100)
         if str(r.get("subject")) != "perception"),
        key=lambda r: str(r.get("counter")),
    )


def list_ns_receipts(graph_adapter: Any) -> list[dict[str, Any]]:
    return sorted(
        (r for r in graph_adapter.list_counters(namespace=NS, limit=100)
         if str(r.get("subject")) == "perception"),
        key=lambda r: str(r.get("counter")),
    )


def ns_is_dirty(graph_adapter: Any) -> bool:
    return NS in graph_adapter.list_dirty_namespaces(limit=500)


def find_watchlist_counter(counters: list[dict[str, Any]]) -> dict[str, Any] | None:
    for r in counters:
        name = str(r.get("counter") or "").lower()
        if "watch" in name or "movie" in name:
            return r
    # fallback: a stated count equal to one of the two asserted totals
    for r in counters:
        try:
            if float(r.get("value")) in (25.0, 20.0):
                return r
        except (TypeError, ValueError):
            continue
    return None


def _p(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="retain validation data in NS on exit")
    ap.add_argument("--json", default="", help="write the machine report to this path")
    args = ap.parse_args()

    os.environ["MENHIR_TURN_NAMESPACE"] = NS  # force the hook to key evidence to the isolated namespace

    from menhir.cli.bootstrap import build_hook_services
    from menhir.config import MemorySettings
    from menhir.infrastructure.sync_llm import make_sync_chat
    from menhir.infrastructure.view_embedder import make_view_embedder

    settings = MemorySettings.from_env()
    hs = build_hook_services(settings)
    neo4j, graph_adapter = hs.neo4j, hs.graph_adapter

    pm_model = getattr(settings, "personal_memory_consolidation_chat_model", "") or None
    chat = make_sync_chat(settings, model=pm_model)
    if chat is None:
        print("ABORT: no sync chat provider configured (personal-memory consolidation cannot run).")
        return 2
    embed = make_view_embedder(settings)

    hook = _load_hook_module()
    report: dict[str, Any] = {
        "namespace": NS,
        "session": SESSION,
        "chat_model": pm_model or "(provider default)",
        "k": K,
        "view_embedder": embed is not None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    _p("0. ISOLATION — purge validation namespace (re-runnable)")
    purged = purge_namespace(neo4j)
    print(f"purged in {NS}: {purged}")
    report["purged_on_start"] = purged

    # ---- 1. producer triage (frozen) ------------------------------------------------------------
    cases = [
        {"n": 1, "prompt": "I have 25 movies on my watch list.", "phase": "A",
         "expect": "candidate -> stated measure"},
        {"n": 2, "prompt": "I bought one bike for $50 and another for $75.", "phase": "A",
         "expect": "candidate -> fold-derived SUM"},
        {"n": 4, "prompt": "write the handoff", "phase": "A",
         "expect": "junk -> dropped by triage"},
        {"n": 3, "prompt": "Actually it is 20, not 25.", "phase": "B",
         "expect": "candidate -> supersession"},
    ]
    _p("1. PRODUCER TRIAGE (deterministic, LLM-free) — observe every prompt, store only candidates")
    triage_rows = []
    for c in cases:
        hook_input = {
            "prompt": c["prompt"], "session_id": SESSION, "cwd": str(_REPO_ROOT),
            "hook_event_name": "UserPromptSubmit", "permission_mode": "default",
        }
        payload = hook.build_evidence_payload(hook_input)
        is_candidate = payload is not None
        reasons = payload["triage_reason"] if payload else []
        c["payload"] = payload
        triage_rows.append({"case": c["n"], "prompt": c["prompt"], "candidate": is_candidate,
                            "reasons": reasons, "expect": c["expect"]})
        mark = "STORE " if is_candidate else "DROP  "
        print(f"  [{mark}] case {c['n']}: {c['prompt']!r}")
        print(f"           reasons={reasons}  ({c['expect']})")
    passed = sum(1 for r in triage_rows if r["candidate"])
    report["triage"] = {"prompts_seen": len(cases), "passed_triage": passed,
                        "dropped": len(cases) - passed, "detail": triage_rows}
    print(f"\n  prompts passing triage: {passed}/{len(cases)}  (dropped junk: {len(cases) - passed})")

    port = int(getattr(settings, "api_port", 8090))
    agent_key = getattr(settings, "agent_key", "") or ""

    # ---- 2. capture phase A (cases 1 & 2) via live endpoint -------------------------------------
    _p("2. CAPTURE phase A -> POST candidates through live /api/turn-evidence")
    stored_a = 0
    for c in cases:
        if c["phase"] != "A" or c["payload"] is None:
            if c["phase"] == "A" and c["payload"] is None:
                print(f"  case {c['n']}: not a candidate -> NOT sent (junk stays on the machine)")
            continue
        res = post_evidence(c["payload"], port=port, agent_key=agent_key)
        created = bool(res.get("body", {}).get("created"))
        stored_a += 1 if res.get("status") == 200 else 0
        print(f"  case {c['n']}: POST -> {res.get('status')} created={created} "
              f"turn_id={res.get('body', {}).get('turn_id', '')[:8]}")
        if res.get("status") != 200:
            print(f"     ERROR: {res.get('error')}")
    captured_a = count_evidence(neo4j)
    report["capture_phase_a"] = {"posted_ok": stored_a, "evidence_in_ns": captured_a}
    print(f"\n  :TurnEvidence captured in {NS}: {captured_a}")

    # ---- 3. Phase 3 selection (dirty detection over evidence) -----------------------------------
    _p("3. PHASE 3 SELECTION — does the dirty query pick up the namespace?")
    selected = ns_is_dirty(graph_adapter)
    report["phase3_selected_before_run1"] = selected
    print(f"  {NS} dirty (Phase 3 would consolidate it): {selected}")

    counters_before = list_ns_counters(graph_adapter)
    print(f"  counter Views in {NS} BEFORE consolidation: {counters_before}")
    report["counters_before"] = counters_before

    # ---- 4. consolidation run 1 -----------------------------------------------------------------
    _p("4. CONSOLIDATION run #1 (real LLM, all bias guards pinned on, batch re-fold)")
    run1 = asyncio.run(run_consolidation(graph_adapter, chat, embed))
    print(f"  result: {run1}")
    counters_after1 = list_ns_counters(graph_adapter)
    print(f"  counter Views in {NS} AFTER run #1: {counters_after1}")
    report["run1"] = run1
    report["counters_after_run1"] = counters_after1

    # ---- 5. debounce: watermark should drop NS from the dirty set -------------------------------
    _p("5. RE-RUN / IDEMPOTENCY")
    dirty_after_run1 = ns_is_dirty(graph_adapter)
    print(f"  a) watermark debounce: {NS} still dirty after run #1? {dirty_after_run1} "
          f"(False => scheduler skips it, 0 re-LLM)")
    run2 = asyncio.run(run_consolidation(graph_adapter, chat, embed))  # forced re-fold (explicit NS)
    counters_after2 = list_ns_counters(graph_adapter)
    unchanged = counters_after2 == counters_after1
    print(f"  b) forced re-fold run #2: {run2}")
    print(f"     current counter Views identical to run #1? {unchanged}")
    print(f"     -> re-run duplicate/divergent writes: {'0' if unchanged else 'NONZERO — see values'}")
    report["debounce_dirty_after_run1"] = dirty_after_run1
    report["run2_forced_refold"] = run2
    report["counters_after_run2"] = counters_after2
    report["rerun_writes_idempotent"] = unchanged

    # ---- 6. capture phase B (case 3 correction) -> re-dirty -------------------------------------
    _p("6. NEW EVIDENCE (case 3 correction) -> re-dirty the namespace")
    case3 = next(c for c in cases if c["n"] == 3)
    if case3["payload"] is None:
        print("  case 3 unexpectedly failed triage; cannot test supersession.")
        report["case3_captured"] = False
    else:
        res = post_evidence(case3["payload"], port=port, agent_key=agent_key)
        print(f"  case 3 POST -> {res.get('status')} created={res.get('body', {}).get('created')}")
        report["case3_captured"] = res.get("status") == 200
    redirty = ns_is_dirty(graph_adapter)
    print(f"  {NS} dirty again after new evidence? {redirty}")
    report["re_dirtied_after_new_evidence"] = redirty

    # ---- 7. consolidation run 3 -> supersession -------------------------------------------------
    _p("7. CONSOLIDATION run #3 -> supersession / currentness")
    run3 = asyncio.run(run_consolidation(graph_adapter, chat, embed))
    print(f"  result: {run3}")
    counters_after3 = list_ns_counters(graph_adapter)
    receipts = list_ns_receipts(graph_adapter)
    print(f"  USER counter Views in {NS} AFTER run #3: {counters_after3}")
    print(f"  abstention RECEIPTS (F1 observability, out of recall): "
          f"{[(r['counter'], r['value']) for r in receipts]}")
    report["run3"] = run3
    report["counters_after_run3"] = counters_after3
    report["abstention_receipts"] = [(r["counter"], r["value"]) for r in receipts]

    wl = find_watchlist_counter(counters_after3)
    if wl:
        hist = graph_adapter.counter_history(
            subject=str(wl["subject"]), counter=str(wl["counter"]), namespace=NS)
        print(f"  watch-list counter now: {wl['counter']}={wl['value']}")
        print(f"  history (newest first): {hist}")
        report["watchlist_current"] = wl
        report["watchlist_history"] = hist
        report["currentness_correct"] = (float(wl["value"]) == 20.0)
        print(f"  currentness: correction to 20 is current? {float(wl['value']) == 20.0} "
              f"(25 would mean the correction did not supersede)")
    else:
        print("  no watch-list counter found (see abstained measures in run output).")
        report["watchlist_current"] = None

    # ---- 8. recall improvement ------------------------------------------------------------------
    _p("8. RECALL IMPROVEMENT — did a durable answer appear that was absent before?")
    before_had = len(counters_before) > 0
    after_had = len(counters_after3) > 0
    print(f"  durable counter Views  before: {len(counters_before)}   after: {len(counters_after3)}")
    print(f"  recall improved (an aggregate answer exists now that did not before): "
          f"{after_had and not before_had}")
    report["recall_improvement"] = {
        "counters_before": len(counters_before),
        "counters_after": len(counters_after3),
        "improved": bool(after_had and not before_had),
    }

    # ---- summary --------------------------------------------------------------------------------
    _p("SUMMARY")
    views_written = len(counters_after3)
    abstained_run1 = int(run1.get("abstained", 0))
    summary = {
        "TurnEvidence captured": count_evidence(neo4j),
        "prompts passing triage": f"{passed}/{len(cases)}",
        "Phase 3 selected": report["phase3_selected_before_run1"],
        "Views written (current)": views_written,
        "abstained (run1)": abstained_run1,
        "re-run duplicate writes": "0" if report["rerun_writes_idempotent"] else "NONZERO",
        "new evidence re-dirties namespace": redirty,
        "recall improvement": report["recall_improvement"]["improved"],
        "supersession/currentness correct": report.get("currentness_correct"),
    }
    for k, v in summary.items():
        print(f"  {k:38s}: {v}")
    report["summary"] = summary
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\n  machine report -> {args.json}")

    if not args.keep:
        _p("CLEANUP — purge validation namespace")
        print(f"purged on exit: {purge_namespace(neo4j)}")
    else:
        print(f"\n  --keep: validation data retained in namespace {NS}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
