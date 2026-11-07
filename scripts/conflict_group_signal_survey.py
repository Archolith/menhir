"""Survey current conflict groups: scope, shared episodes, shared anchors, pair cosine, RRF score.

WHY THIS EXISTS
`CorrelationService` compares graphiti RRF rank-fusion scores (range [0, 2], ordinal) against cosine
thresholds (plan `menhir-conflict-detection-signal-2026-08-09`). This walks the existing conflict
groups and records, per member pair, what each candidate signal would have said, so the RRF claim
(scores cluster at 1.0 and 0.5) can be checked against real data before any routing changes.

READ-ONLY. Every query is a MATCH/RETURN; there is no write path and no --apply flag. It makes NO
LLM call. The RRF score is NOT stored on the graph, so it is only available with --rrf, which runs
the same read-only `search_scored` the enrichment pass uses (one embedding call per member, so it is
opt-in). Without --rrf the `rrf_score` column is null and says so.

Usage:
  python scripts/conflict_group_signal_survey.py bolt://localhost:7687 --password "$NEO4J_PASSWORD"
  python scripts/conflict_group_signal_survey.py bolt://localhost:7687 --password ... --namespace work --out survey.json
  python scripts/conflict_group_signal_survey.py ... --rrf   # adds the fused score (embedding calls)
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from collections import Counter
from typing import Any

from neo4j import GraphDatabase

from menhir.services.correlation_service_signals import attributes_overlap, pair_cosine

GROUPS = """
MATCH (n:Entity)
WHERE n.conflict_group_id IS NOT NULL
  AND ($ns = '' OR n.group_id = $ns OR n.namespace = $ns)
RETURN n.conflict_group_id AS group_id, n.uuid AS uuid, n.name AS name,
       coalesce(n.summary, n.content, '') AS content, n.scope AS scope,
       n.conflict_status AS status, n.name_embedding AS embedding
"""
SHARED_EPISODES = """
MATCH (a:Entity {uuid: $a})-[:MENTIONS]-(ep:Episodic)
MATCH (b:Entity {uuid: $b})-[:MENTIONS]-(ep)
RETURN count(DISTINCT ep) AS shared
"""
ANCHORS = """
MATCH (n:Entity {uuid: $u})-[:ANCHORED_TO]->(f)
RETURN collect(DISTINCT coalesce(f.path, f.name)) AS paths
"""


def _bucket(value: float) -> str:
    return f"{round(value * 4) / 4:.2f}"


def survey(session: Any, namespace: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in session.run(GROUPS, ns=namespace)]
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_group.setdefault(row["group_id"], []).append(row)
    out: list[dict[str, Any]] = []
    for gid, members in by_group.items():
        for a, b in itertools.combinations(members, 2):
            shared = session.run(SHARED_EPISODES, a=a["uuid"], b=b["uuid"]).single()["shared"]
            paths_a = set(session.run(ANCHORS, u=a["uuid"]).single()["paths"] or [])
            paths_b = set(session.run(ANCHORS, u=b["uuid"]).single()["paths"] or [])
            out.append({
                "group_id": gid, "a": a["uuid"], "b": b["uuid"],
                "name_a": a["name"], "name_b": b["name"],
                "scope_a": a["scope"], "scope_b": b["scope"],
                "status_a": a["status"], "status_b": b["status"],
                "shared_episodes": shared,
                "shared_anchor_paths": sorted(paths_a & paths_b),
                "attribute_overlap": attributes_overlap(a["name"], b["name"]),
                "cosine": pair_cosine(a["embedding"], b["embedding"]),
                "rrf_score": None,
            })
    return out


async def add_rrf(pairs: list[dict[str, Any]], names: dict[str, str]) -> None:
    from menhir.infrastructure.graphiti_client import GraphitiClient  # lazy: needs full config

    client = GraphitiClient()
    for pair in pairs:
        hits = await client.search_scored(names[pair["a"]] or "", num_results=10)
        pair["rrf_score"] = next((s for u, _n, s in hits if u == pair["b"]), None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("uri")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--namespace", default="")
    ap.add_argument("--rrf", action="store_true", help="also compute the RRF score (embedding calls)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        with driver.session(default_access_mode="READ") as session:
            pairs = survey(session, args.namespace)
    finally:
        driver.close()
    if args.rrf:
        names = {p["a"]: p["name_a"] for p in pairs}
        asyncio.run(add_rrf(pairs, names))

    scored = [p["rrf_score"] for p in pairs if p["rrf_score"] is not None]
    summary = {
        "pairs": len(pairs),
        "groups": len({p["group_id"] for p in pairs}),
        "rrf_scored": len(scored),
        "rrf_histogram": dict(sorted(Counter(_bucket(s) for s in scored).items())),
        "rrf_at_1_0_or_0_5": sum(1 for s in scored if abs(s - 1.0) < 1e-6 or abs(s - 0.5) < 1e-6),
        "cosine_histogram": dict(sorted(Counter(
            _bucket(p["cosine"]) for p in pairs if p["cosine"] is not None).items())),
        "shared_episode_pairs": sum(1 for p in pairs if p["shared_episodes"]),
        "shared_episode_and_disjoint_attributes": sum(
            1 for p in pairs if p["shared_episodes"] and not p["attribute_overlap"]),
        "shared_episode_and_overlapping_attributes": sum(
            1 for p in pairs if p["shared_episodes"] and p["attribute_overlap"]),
    }
    payload = {"summary": summary, "pairs": pairs}
    text = json.dumps(payload, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
