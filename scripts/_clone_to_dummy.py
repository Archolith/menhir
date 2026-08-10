"""Throwaway: online bolt clone of the live menhir graph into the local dummy Neo4j.

Source (read-only) = main checkout .env Neo4j; target = local Docker dummy. Uses APOC on the
target for dynamic labels/rel-types. Not committed; one-shot dev tooling.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
SRC = (os.environ["NEO4J_URI"], os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", ""))
TGT = ("bolt://localhost:7687", "neo4j", "menhirdummy123")

NODE_BATCH = 500
REL_BATCH = 1000


def main() -> None:
    src = GraphDatabase.driver(SRC[0], auth=(SRC[1], SRC[2]))
    tgt = GraphDatabase.driver(TGT[0], auth=(TGT[1], TGT[2]))
    t0 = time.time()

    with tgt.session() as ts:
        ts.run("CREATE INDEX clone_src IF NOT EXISTS FOR (n:_Clone) ON (n._src_eid)")

    # --- nodes ---
    nodes = 0
    batch: list[dict] = []

    def flush_nodes(rows: list[dict]) -> None:
        with tgt.session() as ts:
            ts.run(
                """
                UNWIND $rows AS row
                CALL apoc.create.node(row.labels + ['_Clone'],
                     apoc.map.setKey(row.props, '_src_eid', row.eid)) YIELD node
                RETURN count(*)
                """,
                rows=rows,
            ).consume()

    with src.session() as ss:
        for rec in ss.run("MATCH (n) RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props"):
            batch.append({"eid": rec["eid"], "labels": rec["labels"], "props": dict(rec["props"])})
            if len(batch) >= NODE_BATCH:
                flush_nodes(batch); nodes += len(batch); batch = []
                print(f"  nodes {nodes} ... {time.time()-t0:.0f}s")
        if batch:
            flush_nodes(batch); nodes += len(batch); batch = []
    print(f"nodes copied: {nodes}")

    # --- relationships ---
    rels = 0
    rbatch: list[dict] = []

    def flush_rels(rows: list[dict]) -> None:
        with tgt.session() as ts:
            ts.run(
                """
                UNWIND $rows AS row
                MATCH (a:_Clone {_src_eid: row.sa}), (b:_Clone {_src_eid: row.sb})
                CALL apoc.create.relationship(a, row.t, row.props, b) YIELD rel
                RETURN count(*)
                """,
                rows=rows,
            ).consume()

    with src.session() as ss:
        for rec in ss.run(
            "MATCH (a)-[r]->(b) RETURN elementId(a) AS sa, elementId(b) AS sb, type(r) AS t, properties(r) AS props"
        ):
            rbatch.append({"sa": rec["sa"], "sb": rec["sb"], "t": rec["t"], "props": dict(rec["props"])})
            if len(rbatch) >= REL_BATCH:
                flush_rels(rbatch); rels += len(rbatch); rbatch = []
                print(f"  rels {rels} ... {time.time()-t0:.0f}s")
        if rbatch:
            flush_rels(rbatch); rels += len(rbatch); rbatch = []
    print(f"rels copied: {rels}")

    # --- cleanup temp clone markers ---
    with tgt.session() as ts:
        ts.run(
            "CALL apoc.periodic.iterate('MATCH (n:_Clone) RETURN n', "
            "'REMOVE n._src_eid REMOVE n:_Clone', {batchSize:1000})"
        ).consume()
        n = ts.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        r = ts.run("MATCH ()-[x]->() RETURN count(x) AS c").single()["c"]
    print(f"TARGET totals after cleanup: nodes={n} rels={r}  ({time.time()-t0:.0f}s)")
    src.close(); tgt.close()


if __name__ == "__main__":
    main()
