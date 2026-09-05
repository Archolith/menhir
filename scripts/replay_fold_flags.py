#!/usr/bin/env python3
"""Score gate configurations on the FINAL ScalarStateView, not on what the gate committed.

The gate-level metric (`replay_gate_flags.py`) asks only whether a proposal carrying the right
number was committed. It is structurally blind to `operation`: committing `delta 25` where the truth
is `absolute 25` scores as a success there, because both carry the value 25. Those two readings fold
into completely different Views -- one adds 25 to a running total, the other sets the level to 25 --
so operation correctness is ONLY observable after the fold. That makes this script, not a stricter
gate metric, the honest test.

Everything upstream of the fold is replayed from the frozen capture: no LLM calls. Source episodes
are read from the read-only LME graph; every arm/config is materialized into its OWN throwaway
namespace on the ephemeral test Neo4j (docker-compose.test.yml, tmpfs, no volume). Refuses to run if
the write target resolves to the same host:port as NEO4J_URI or as the LME source.

    docker compose -f docker-compose.test.yml up -d
    PYTHONPATH=src python scripts/replay_fold_flags.py --jsonl %TEMP%/pr_final_KEEP.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone

from neo4j import GraphDatabase

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.typed_scalar_perception import (
    _parse_json_array,
    bind_and_persist_typed_scalars,
    ensure_scalar_state_activated,
    gate_typed_scalars,
    parse_scalar_row,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402
from _replay_entities import fetch_linked_entities, materialize_linked_entities  # noqa: E402
from measure_absence_band import EPISODES_Q, build_episodes  # noqa: E402

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

BASELINE_PROMPT_PREFIX = "You convert"

#: After scripts/repair_lme_valid_at.py, episode `valid_at` holds the real conversation time and
#: `valid_at_ingest_original` holds what was there when the completions were frozen. Both are needed,
#: for DIFFERENT purposes, and conflating them silently invalidates the replay:
#:
#:   valid_at (as captured) -> builds the episode LIST. `build_episodes` collapses exact
#:       (body, valid_at) duplicates, so repairing timestamps made duplicate turns collapse and
#:       changed the episode count in 39 of 78 namespaces. The frozen completions address episodes by
#:       INDEX, so the list must be reconstructed exactly as the model saw it.
#:   valid_at (repaired)    -> feeds `episode_reference_time`, hence each assertion's valid_at, hence
#:       supersession. This is the thing under test.
#:
#: The result is an ABLATION -- same extraction, corrected chronology -- not a simulation of the
#: repaired system end to end. Measuring that needs a fresh capture.
EPISODES_WITH_BOTH_TIMES_Q = """
MATCH (e:Episodic)
WHERE e.content STARTS WITH 'user:' AND e.group_id = $ns
RETURN e.uuid AS uuid,
       toString(coalesce(e.valid_at_ingest_original, e.valid_at, e.created_at)) AS valid_at,
       toString(coalesce(e.valid_at, e.created_at)) AS repaired_valid_at,
       e.content AS content
ORDER BY coalesce(e.created_at, e.valid_at), e.uuid
"""

#: name, threshold, reconcile_attribute, align_spans, reconcile_scope, reconcile_subject,
#: canonical_self. The last three were added after the residual-loss dump showed the surviving
#: scatter is the SAME identity-smearing defect spread into `scope` and `subject`.
CONFIGS = [
    ("today", 1.0, False, False, False, False, False),
    ("reconcile", 1.0, True, False, False, False, False),
    ("both-fixes", 1.0, True, True, False, False, False),
    ("t2of3", 2.0 / 3.0, False, False, False, False, False),
    ("t2of3+both", 2.0 / 3.0, True, True, False, False, False),
    ("+scope", 2.0 / 3.0, True, True, True, False, False),
    ("+scope+subject", 2.0 / 3.0, True, True, True, True, False),
    ("+scope+subj+self", 2.0 / 3.0, True, True, True, True, True),
]


def _hostport(uri: str) -> str:
    value = str(uri or "").strip().lower()
    for scheme in ("bolt+s://", "bolt+ssc://", "bolt://", "neo4j+s://", "neo4j+ssc://", "neo4j://"):
        if value.startswith(scheme):
            value = value[len(scheme):]
            break
    return value.replace("127.0.0.1", "localhost").rstrip("/")


def _numbers(value: object) -> list[float]:
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--source-uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--source-password", default="lmedata123")
    ap.add_argument("--target-uri",
                    default=os.environ.get("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"))
    ap.add_argument("--target-password",
                    default=os.environ.get("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"))
    ap.add_argument("--run-id", default="foldflags")
    ap.add_argument("--bind-scope", choices=("episode", "namespace"), default="episode",
                    help="binding candidates: the claim's own episode (production behaviour) "
                         "or every entity in the namespace (measurement only)")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    target = _hostport(args.target_uri)
    if target == _hostport(os.environ.get("NEO4J_URI", "")):
        raise SystemExit("refusing to write: target resolves to the production graph")
    if target == _hostport(args.source_uri):
        raise SystemExit("refusing to write: target resolves to the read-only LME source")

    completions: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    with open(args.jsonl, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "call" and str(row.get("system", "")).startswith(
                    BASELINE_PROMPT_PREFIX):
                completions[(row["ns"], int(row["trial"]))].append(row["completion"])

    suffixes = sorted({ns for ns, _ in completions})
    source = GraphDatabase.driver(args.source_uri, auth=("neo4j", args.source_password))
    episodes_by_ns, content_by_uuid, entities_by_uuid = {}, {}, {}
    with source.session() as session:
        for suffix in suffixes:
            found = session.run(
                "MATCH (e:Episodic) WHERE e.group_id ENDS WITH $s "
                "RETURN DISTINCT e.group_id AS ns", {"s": suffix}).data()
            rows = session.run(EPISODES_WITH_BOTH_TIMES_Q, {"ns": found[0]["ns"]}).data()
            # `valid_at` here is the AS-CAPTURED time, so the list matches what the model saw.
            episodes_by_ns[suffix] = build_episodes(rows)
            for r in rows:
                # ...while the fold is timed by the REPAIRED conversation time.
                content_by_uuid[str(r["uuid"])] = (
                    r.get("content"), str(r.get("repaired_valid_at") or "") or None)
                # Binding candidates for non-self subjects. Without these the harness can only
                # materialize Views for the SELF and silently scores every other subject as a fold
                # failure -- see scripts/_replay_entities.py.
                entities_by_uuid[str(r["uuid"])] = fetch_linked_entities(session, str(r["uuid"]))

            # --bind-scope=namespace: pool every episode's candidates and offer the SAME pool to
            # every episode. Extraction reads the whole episode list and can name a subject that was
            # mentioned in a DIFFERENT turn ("I've been reading National Geographic ... I've
            # finished five issues so far"), but binding only sees candidates on the claim's own
            # episode, so that subject can never bind and the fact never materializes a View.
            # `_bind_subject` still requires an exact, UNIQUE normalized-name match, so widening the
            # pool can only add a binding where exactly one candidate in the namespace matches --
            # two same-named entities still abstain.
            # Scoped to THIS namespace's episodes only -- `entities_by_uuid` is keyed across every
            # namespace, so pooling over all of it would let one question's entities bind another's.
            if args.bind_scope == "namespace":
                episode_uuids = [str(r["uuid"]) for r in rows]
                pooled: dict[str, dict[str, str]] = {}
                for key in episode_uuids:
                    for candidate in entities_by_uuid.get(key, []):
                        pooled[candidate["uuid"]] = candidate
                shared = list(pooled.values())
                for key in episode_uuids:
                    entities_by_uuid[key] = shared
    source.close()

    # Parse ONCE per cell; every config then gates the same proposals, so differences are the gate's.
    samples_by_cell: dict[tuple[str, int], list[list]] = {}
    for (suffix, trial), texts in sorted(completions.items()):
        episodes = episodes_by_ns[suffix]
        parsed = []
        for text in texts:
            raw_rows, failure = _parse_json_array(text)
            parsed.append([] if failure else [
                p for p in (parse_scalar_row(r, episodes, lambda _r: None) for r in raw_rows)
                if p is not None
            ])
        samples_by_cell[(suffix, trial)] = parsed

    repo = Neo4jRepository(uri=args.target_uri, database="neo4j",
                           user="neo4j", password=args.target_password)
    adapter = MemoryGraphAdapter(neo4j=repo)
    ensure_scalar_state_activated(adapter)
    service = ScalarStateService(adapter._typed_assertions, adapter._views)

    results: dict[str, dict[str, int]] = {}
    #: config -> the cells it got right, so two runs can be diffed for REGRESSIONS. A widened
    #: binding pool is a superset, which can only add bindings -- except where a second same-named
    #: entity enters the pool and breaks `_bind_subject`'s uniqueness requirement, turning a
    #: previously-bound subject into an abstention. Aggregate totals would hide that.
    correct_cells: dict[str, list[str]] = {}
    try:
        for name, threshold, reconcile, align, scope, subject, self_canon in CONFIGS:
            tally = collections.Counter()
            for (suffix, trial), samples in sorted(samples_by_cell.items()):
                decisions = [d for d in gate_typed_scalars(
                    samples, threshold=threshold,
                    reconcile_attribute=reconcile, align_spans=align,
                    reconcile_scope=scope, reconcile_subject=subject,
                    canonical_self=self_canon) if d.committed]
                namespace = f"pr-fold-{args.run_id}-{name}-{suffix}-t{trial:02d}"

                episode_times: dict[str, str | None] = {}
                remapped = []
                for d in decisions:
                    original = d.proposal.episode_uuid
                    fresh = str(uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{args.run_id}|{name}|{suffix}|{trial}|{original}"))
                    content, valid_at = content_by_uuid.get(original, (None, None))
                    episode_times[fresh] = valid_at
                    repo.execute(
                        "MERGE (e:Episodic {uuid:$uuid}) SET e.group_id=$ns, e.content=$content, "
                        "e.valid_at=CASE WHEN $valid_at IS NULL OR $valid_at='' THEN null "
                        "ELSE datetime($valid_at) END",
                        {"uuid": fresh, "ns": namespace, "content": content, "valid_at": valid_at})
                    materialize_linked_entities(
                        repo, namespace=namespace, fresh_episode_uuid=fresh,
                        entities=entities_by_uuid.get(original, []),
                        salt=f"{args.run_id}|{name}|{suffix}|{trial}")
                    import dataclasses
                    remapped.append(dataclasses.replace(
                        d, proposal=dataclasses.replace(d.proposal, episode_uuid=fresh)))

                # Replay input is model output plus benchmark fixtures, not an owner-signed exact
                # assertion. It may exercise vote/fold behavior, but it must not mint semantic
                # authority for canonical self. Self-like decisions therefore persist through the
                # ordinary advisory path and produce no authoritative self View.
                bind_and_persist_typed_scalars(
                    remapped,
                    linked_entities_for_episode=adapter.fetch_linked_entities_for_episode,
                    record_assertion=adapter.record_typed_assertion,
                    rebuild_scalar_state=lambda u: service.rebuild_scalar_state(
                        u, namespace=namespace),
                    episode_reference_time=episode_times.get,
                    namespace=namespace, perceiver_version="fold-flags-v1",
                    now=lambda: datetime.now(timezone.utc).isoformat(),
                    mark_projection_complete=adapter.mark_projection_complete,
                )

                views = repo.execute(
                    "MATCH (v:Entity {view_kind:'scalar_state', group_id:$ns}) "
                    "WHERE coalesce(v.view_current,true) "
                    "RETURN v.ss_attribute AS attribute, v.ss_value AS value", {"ns": namespace})
                need = gt.required_values(suffix)[-1]
                if any(abs(n - need) < 1e-9 for v in views for n in _numbers(v.get("value"))):
                    tally["cells_with_correct_view"] += 1
                    correct_cells.setdefault(name, []).append(f"{suffix}-t{trial:02d}")
                tally["views"] += len(views)
                tally["commits"] += len(decisions)
                # Per-View verdict, so extra volume can be judged rather than merely counted.
                # WRONG is the one that must not grow: a number the benchmark contradicts, sitting
                # in a current View. STALE is a real earlier value resurfacing as current.
                for v in views:
                    tally[f"v_{gt.classify(suffix, v.get('value')).lower()}"] += 1
            results[name] = dict(tally)
    finally:
        repo.close()

    print(f"FINAL-VIEW scoring, {len(samples_by_cell)} namespace-trials, fold actually run\n")
    # `unmatched` is NOT an error count. It means "does not match the ONE value this namespace's
    # benchmark question asks about", which every true fact about any other slot also fails. A hand
    # decomposition of the 129 unmatched Views at t2of3+both found 123 true-but-unlabelled and 6
    # genuinely wrong numbers. Read growth here as volume, and decompose before calling it harm.
    print(f"{'config':<18}{'correct cells':>16}{'commits':>9}{'views':>7}"
          f"{'current':>9}{'stale':>7}{'unmatched':>11}{'unknown':>9}")
    for name, *_flags in CONFIGS:
        r = results[name]
        print(f"{name:<18}{r.get('cells_with_correct_view', 0):>9}/{len(samples_by_cell):<6}"
              f"{r.get('commits', 0):>9}{r.get('views', 0):>7}"
              f"{r.get('v_' + gt.CURRENT, 0):>9}{r.get('v_' + gt.STALE, 0):>7}"
              f"{r.get('v_' + gt.WRONG, 0):>7}{r.get('v_' + gt.UNKNOWN, 0):>9}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"totals": results, "correct_cells": correct_cells,
                       "bind_scope": args.bind_scope}, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
