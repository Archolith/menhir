"""CF-17 residue audit: which apex-tier memories would FAIL today's admission gate?

READ-ONLY. This script executes no write of any kind, against any store. It is a census, and
the decision it informs -- leave, re-grade, or backfill -- is the operator's.

**The question, and why it is not the obvious one.** The obvious question is "which memories were
admitted through the old token-overlap branch". That is unanswerable: the old code never recorded
WHICH branch granted admission, so the information does not exist in the graph. The answerable
and more operationally useful question is:

    Which memories currently carrying apex/user trust would FAIL if their original evidence were
    evaluated by today's gate?

That is a direct re-evaluation rather than an inference, and its answer is the thing an operator
would act on either way.

**Three outcomes, and each means something different:**

* ``still_granted``  -- the claim is grounded under today's rules. Nothing to do.
* ``now_downgraded`` -- the claim carries apex trust that today's gate would refuse. This is the
  residue. Reported with its reason and a sample, because the reason decides what to do about it.
* ``unevaluable``    -- the original :TurnEvidence is gone, so the claim cannot be re-checked.
  Counted SEPARATELY and never folded into either other bucket: an unknown is not a pass, and
  reporting it as one would be the same "counts look correct" failure this programme keeps
  finding.

Usage:
    python -m scripts.audit_cf17_apex_residue                 # against the configured graph
    python -m scripts.audit_cf17_apex_residue --limit 500
    python -m scripts.audit_cf17_apex_residue --json          # machine-readable

Point ``NEO4J_URI`` at whichever graph you mean to audit. It reads the same configuration the
server does, so by default that is the operator's real graph -- which is correct here, since the
question is about real stored data, and the script cannot write to it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

#: Nodes stamped at or above this confidence are carrying apex/user trust.
#: Read from the domain authority rather than hardcoded -- `source_confidence_for` documents
#: itself as THE mapping, and a second copy here would be the drift it exists to end.
from menhir.domain.utils import source_confidence_for
from menhir.domain.truth.admission_gate import evaluate_user_tier_claim

APEX_SOURCES = ("user", "manual")


def _apex_floor() -> float:
    return min(source_confidence_for(s) for s in APEX_SOURCES)


def fetch_apex_claims(repo: Any, limit: int) -> list[dict[str, Any]]:
    """Every apex-tier memory, with its linked TurnEvidence if one survives.

    OPTIONAL MATCH on the evidence rather than MATCH: a claim whose evidence is gone is precisely
    the `unevaluable` bucket, and requiring the edge would silently drop exactly the rows the
    audit needs to count.
    """
    return repo.execute(
        """
        MATCH (n)
        WHERE (n:Entity OR n:Episodic)
          AND toLower(coalesce(n.source, '')) IN $apex_sources
        OPTIONAL MATCH (n)-[:ADMITTED_ON]->(t:TurnEvidence)
        RETURN n.uuid              AS uuid,
               n.source            AS source,
               n.source_confidence AS source_confidence,
               n.namespace         AS namespace,
               n.session_id        AS session_id,
               coalesce(n.content, n.summary, n.name, '') AS claimed_text,
               n.created_at        AS created_at,
               t.turn_id           AS turn_id,
               t.role              AS role,
               t.declarant         AS declarant,
               t.text              AS evidence_text,
               t.session_id        AS evidence_session_id,
               t.namespace         AS evidence_namespace
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        params={"apex_sources": list(APEX_SOURCES), "limit": int(limit)},
    )


def reevaluate(row: dict[str, Any]) -> tuple[str, str]:
    """Re-run TODAY's gate over one stored claim. Returns (bucket, reason)."""
    if not row.get("turn_id"):
        return "unevaluable", "no surviving :TurnEvidence linked to this memory"
    if not (row.get("evidence_text") or "").strip():
        return "unevaluable", "linked :TurnEvidence carries no text"

    verdict = evaluate_user_tier_claim(
        requested_source=str(row.get("source") or ""),
        turn_evidence={
            "turn_id": row.get("turn_id"),
            "role": row.get("role"),
            "declarant": row.get("declarant"),
            "text": row.get("evidence_text"),
            "session_id": row.get("evidence_session_id"),
            "namespace": row.get("evidence_namespace"),
        },
        claimed_text=str(row.get("claimed_text") or ""),
        session_id=row.get("session_id"),
        namespace=row.get("namespace"),
    )
    if verdict.granted:
        return "still_granted", verdict.reason
    return "now_downgraded", verdict.reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--sample", type=int, default=5, help="downgraded examples to print")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from menhir.config import MemorySettings
    from menhir.infrastructure.neo4j import Neo4jRepository

    settings = MemorySettings.from_env()
    repo = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    try:
        rows = fetch_apex_claims(repo, args.limit)
    finally:
        repo.close()

    buckets: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    for row in rows:
        bucket, reason = reevaluate(row)
        buckets[bucket] += 1
        if bucket != "still_granted":
            reasons[f"{bucket}: {reason}"] += 1
        if bucket == "now_downgraded" and len(samples) < args.sample:
            samples.append(
                {
                    "uuid": row.get("uuid"),
                    "source": row.get("source"),
                    "created_at": str(row.get("created_at")),
                    "reason": reason,
                    "claimed_text": str(row.get("claimed_text") or "")[:160],
                    "evidence_text": str(row.get("evidence_text") or "")[:160],
                }
            )

    report = {
        "apex_claims_scanned": len(rows),
        "apex_confidence_floor": _apex_floor(),
        "still_granted": buckets["still_granted"],
        "now_downgraded": buckets["now_downgraded"],
        "unevaluable": buckets["unevaluable"],
        "reasons": dict(reasons),
        "downgraded_sample": samples,
        "mutations_performed": 0,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"CF-17 apex residue audit  (READ-ONLY, {report['mutations_performed']} mutations)")
    print(f"  apex claims scanned : {report['apex_claims_scanned']}")
    print(f"  still granted       : {report['still_granted']}")
    print(f"  NOW DOWNGRADED      : {report['now_downgraded']}")
    print(f"  unevaluable         : {report['unevaluable']}  (evidence missing -- NOT a pass)")
    if reasons:
        print("\n  reasons:")
        for reason, count in reasons.most_common():
            print(f"    {count:6}  {reason}")
    if samples:
        print("\n  downgraded sample:")
        for s in samples:
            print(f"    {s['uuid']}  ({s['created_at']})")
            print(f"      reason : {s['reason']}")
            print(f"      claim  : {s['claimed_text']!r}")
            print(f"      source : {s['evidence_text']!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
