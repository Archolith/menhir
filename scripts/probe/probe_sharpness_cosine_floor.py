#!/usr/bin/env python
"""P3 calibration probe for SHARPNESS_COSINE_FLOOR (menhir lifecycle F2).

READ-ONLY. Measures the sharpness distribution that the new true-cosine neighbor count
(`GraphitiClient.count_similar_by_cosine`) produces over a real corpus, at several candidate
cosine floors, so an operator can pick a floor that DISCRIMINATES (not everything unique, not a
mass-compress cliff). No graph writes, no lifecycle actions — only MATCH reads and cosine searches.

It exercises the exact production path: for each sampled PERSISTENT Entity node it calls
`count_similar_by_cosine(query=name/summary, exclude_uuid=uuid, min_cosine=floor,
group_ids=namespace_to_group_ids(node.namespace))` and computes `sharpness = 1/(1+count)`.

Usage:
  .venv/Scripts/python.exe scripts/probe/probe_sharpness_cosine_floor.py \\
    --namespace agent-experience \\
    --sample 200 --seed 0 \\
    --floors 0.60,0.70,0.80,0.85 \\
    --out .agent/test_tmp/f2-cosine-floor-probe.md

  # omit --namespace to sample PERSISTENT Entity nodes across ALL namespaces (each node's
  # count is still scoped to its own namespace, matching production).

  # Runbook (re-run triggers, selection criteria, last result):
  #   docs/runbooks/sharpness-cosine-floor-calibration.md
  # Raw output is transient scratch (.agent/test_tmp/ is gitignored); promote the winning
  # row into the runbook + set SHARPNESS_COSINE_FLOOR in lifecycle_service.py.

Selection criterion for a "discriminating" floor (per plan P3):
  - =1.0 (zero-neighbor) share <= 60%   (not everything is "unique")
  - <0.3 (compress-eligible) share <= 25% (not a mass-compress cliff)
  - the 0.2-0.5 band is populated (promote/compress actually discriminate)
Choose the SMALLEST floor meeting all three.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("f2-probe")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines into os.environ if unset."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# Buckets are half-open [lo, hi); the =1.0 bucket is exact.
_BUCKETS = [
    ("<0.2", 0.0, 0.2),
    ("0.2-<0.3", 0.2, 0.3),
    ("0.3-<0.5", 0.3, 0.5),
    ("0.5-<1.0", 0.5, 1.0),
]


@dataclass
class FloorStats:
    floor: float
    sharpness: list[float] = field(default_factory=list)

    def histogram(self) -> dict[str, int]:
        hist = {name: 0 for name, _, _ in _BUCKETS}
        hist["=1.0"] = 0
        for s in self.sharpness:
            if s >= 1.0:
                hist["=1.0"] += 1
                continue
            for name, lo, hi in _BUCKETS:
                if lo <= s < hi:
                    hist[name] += 1
                    break
        return hist

    def frac(self, predicate) -> float:
        if not self.sharpness:
            return 0.0
        return sum(1 for s in self.sharpness if predicate(s)) / len(self.sharpness)

    def median(self) -> float:
        return statistics.median(self.sharpness) if self.sharpness else float("nan")


def _fetch_sample(neo4j, namespace: str | None, sample: int, seed: int):
    """Deterministically sample PERSISTENT Entity nodes (uuid, name, summary, namespace)."""
    where = ["n.scope = 'PERSISTENT'", "n.name IS NOT NULL"]
    params: dict[str, object] = {}
    if namespace:
        where.append("coalesce(n.namespace, 'default') = $ns")
        params["ns"] = namespace
    where_clause = " AND ".join(where)

    id_rows = neo4j.execute(
        f"MATCH (n:Entity) WHERE {where_clause} RETURN n.uuid AS uuid ORDER BY n.uuid",
        params=params,
    )
    uuids = [r["uuid"] for r in id_rows if r.get("uuid")]
    logger.info("Population: %d PERSISTENT Entity nodes%s", len(uuids),
                f" in namespace '{namespace}'" if namespace else " (all namespaces)")
    if not uuids:
        return []
    picked = random.Random(seed).sample(uuids, min(sample, len(uuids)))

    rows = neo4j.execute(
        "MATCH (n:Entity) WHERE n.uuid IN $uuids "
        "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, "
        "coalesce(n.namespace, 'default') AS namespace",
        params={"uuids": picked},
    )
    return rows


async def _run(args) -> int:
    _load_dotenv()
    from menhir.config.settings import MemorySettings
    from menhir.core.bootstrap import build_memory_services
    from menhir.domain.namespace import namespace_to_group_ids

    settings = MemorySettings.from_env()
    logger.info("Neo4j: %s | embed: %s", settings.neo4j_uri,
                getattr(settings, "graphiti_embed_provider", "?"))
    artifacts = build_memory_services(settings)
    neo4j = artifacts.neo4j
    graphiti = artifacts.graphiti_client
    if type(graphiti).__name__ == "UnavailableGraphitiClient":
        logger.error("Graphiti client unavailable (reads not ready). Aborting — cannot embed.")
        return 2

    floors = [float(f.strip()) for f in args.floors.split(",")]
    nodes = _fetch_sample(neo4j, args.namespace, args.sample, args.seed)
    if not nodes:
        logger.error("No PERSISTENT Entity nodes matched — nothing to calibrate.")
        return 3
    logger.info("Sampled %d nodes; floors=%s", len(nodes), floors)

    stats = {f: FloorStats(floor=f) for f in floors}
    skipped = 0
    for i, node in enumerate(nodes, 1):
        query = str(node.get("name") or node.get("summary") or "").strip()
        if not query:
            continue
        uuid = str(node["uuid"])
        group_ids = namespace_to_group_ids(str(node.get("namespace") or "default"))
        for floor in floors:
            count = await graphiti.count_similar_by_cosine(
                query, exclude_uuid=uuid, min_cosine=floor, group_ids=group_ids,
            )
            if count < 0:
                skipped += 1
                continue
            stats[floor].sharpness.append(1.0 / (1.0 + count))
        if i % 25 == 0:
            logger.info("  ... %d/%d nodes probed", i, len(nodes))

    _write_report(Path(args.out), args, floors, stats, len(nodes), skipped)
    logger.info("Wrote %s", args.out)
    return 0


def _write_report(out: Path, args, floors, stats, n_nodes, skipped) -> None:
    lines: list[str] = []
    lines.append("# F2 cosine-floor calibration probe")
    lines.append("")
    lines.append(f"- Namespace: `{args.namespace or 'ALL'}`  |  sampled nodes: {n_nodes}  "
                 f"|  seed: {args.seed}  |  skipped (search unavailable): {skipped}")
    lines.append(f"- Floors: {', '.join(str(f) for f in floors)}")
    lines.append("- sharpness = 1/(1+count of true-cosine neighbors strictly above the floor)")
    lines.append("")
    lines.append("| floor | n | median | <0.2 | 0.2-<0.3 | 0.3-<0.5 | 0.5-<1.0 | =1.0 | "
                 "compress(<0.3) | promote(>=0.5) | zero(=1.0) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for f in floors:
        st = stats[f]
        h = st.histogram()
        n = len(st.sharpness)
        comp = st.frac(lambda s: s < 0.3)
        prom = st.frac(lambda s: s >= 0.5)
        zero = st.frac(lambda s: s >= 1.0)
        lines.append(
            f"| {f:.2f} | {n} | {st.median():.3f} | {h['<0.2']} | {h['0.2-<0.3']} | "
            f"{h['0.3-<0.5']} | {h['0.5-<1.0']} | {h['=1.0']} | "
            f"{comp:.0%} | {prom:.0%} | {zero:.0%} |"
        )
    lines.append("")
    lines.append("## Selection")
    lines.append("Smallest floor with: zero(=1.0) <= 60%, compress(<0.3) <= 25%, and a populated "
                 "0.2-0.5 band. Set `SHARPNESS_COSINE_FLOOR` in `lifecycle_service.py` to that value "
                 "and record the winning row here.")
    lines.append("")
    winner = None
    for f in sorted(floors):
        st = stats[f]
        if not st.sharpness:
            continue
        zero = st.frac(lambda s: s >= 1.0)
        comp = st.frac(lambda s: s < 0.3)
        mid = st.frac(lambda s: 0.2 <= s < 0.5)
        if zero <= 0.60 and comp <= 0.25 and mid > 0.0:
            winner = f
            break
    lines.append(f"**Auto-suggested floor (criteria met, smallest):** "
                 f"{winner if winner is not None else 'NONE met the criteria — widen floors or review'}")
    lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate SHARPNESS_COSINE_FLOOR (read-only)")
    parser.add_argument("--namespace", type=str, default=None,
                        help="Namespace to sample from; omit for ALL namespaces")
    parser.add_argument("--sample", type=int, default=200, help="Nodes to sample (default 200)")
    parser.add_argument("--seed", type=int, default=0, help="Deterministic sample seed")
    parser.add_argument("--floors", type=str, default="0.60,0.70,0.80,0.85",
                        help="Comma-separated candidate cosine floors")
    parser.add_argument("--out", type=str, default=".agent/test_tmp/f2-cosine-floor-probe.md",
                        help="Output markdown artifact path (transient scratch; promote the winning "
                             "row into docs/runbooks/sharpness-cosine-floor-calibration.md)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
