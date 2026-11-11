"""Shared canary search for erasure tests (remediation plan R2).

After an erase, nothing in Neo4j or the sidecar may still hold the erased memory's words. These
helpers search for a planted canary string in EVERY property of every node and relationship, and
in every TEXT/BLOB column of every sidecar table. That is a superset of both inventories
(``graph_text_inventory`` and ``telemetry/erasure_inventory.CONTENT_COLUMNS``), so a holder that was
never inventoried is reported too, with its label and property, instead of being missed.
"""

from __future__ import annotations

import sqlite3
import uuid as uuidlib
from pathlib import Path
from typing import Any, NamedTuple


class Hit(NamedTuple):
    store: str  # "node" | "relationship" | "sqlite"
    where: str  # label(s) / rel type / table
    key: str  # uuid or elementId / rowid
    prop: str  # property or column


def new_canary(tag: str = "") -> str:
    return f"CANARY{tag}{uuidlib.uuid4().hex}"


_GRAPH_SCAN = """
MATCH (n)
UNWIND keys(n) AS k
WITH n, k, n[k] AS v
WHERE (v IS :: STRING AND v CONTAINS $c)
   OR (v IS :: LIST<STRING> AND any(x IN v WHERE x CONTAINS $c))
RETURN 'node' AS store, labels(n) AS where, coalesce(n.uuid, elementId(n)) AS key, k AS prop
UNION ALL
MATCH ()-[r]->()
UNWIND keys(r) AS k
WITH r, k, r[k] AS v
WHERE (v IS :: STRING AND v CONTAINS $c)
   OR (v IS :: LIST<STRING> AND any(x IN v WHERE x CONTAINS $c))
RETURN 'relationship' AS store, [type(r)] AS where, coalesce(r.uuid, elementId(r)) AS key,
       k AS prop
"""


def graph_hits(repo: Any, canary: str) -> list[Hit]:
    """Every node/relationship property (string or string list) containing ``canary``."""
    rows = repo.execute(_GRAPH_SCAN, params={"c": canary})
    return [Hit(r["store"], ":".join(sorted(r["where"])), str(r["key"]), r["prop"]) for r in rows]


def sqlite_hits(db_path: str | Path, canary: str) -> list[Hit]:
    """Every row of every sidecar table whose TEXT/BLOB/untyped column contains ``canary``."""
    hits: list[Hit] = []
    if not Path(db_path).exists():
        return hits
    con = sqlite3.connect(str(db_path))
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        needle = canary.encode()
        for table in tables:
            cols = [
                r[1]
                for r in con.execute(f'PRAGMA table_info("{table}")')
                if (r[2] or "").upper() in {"", "TEXT", "BLOB"} or "CHAR" in (r[2] or "").upper()
            ]
            for col in cols:
                # instr on the raw bytes also finds the canary inside JSON or BLOB payloads.
                for (rowid,) in con.execute(
                    f'SELECT rowid FROM "{table}" WHERE instr(CAST("{col}" AS BLOB), ?) > 0',
                    (needle,),
                ):
                    hits.append(Hit("sqlite", table, str(rowid), col))
    finally:
        con.close()
    return hits


def all_hits(repo: Any, db_path: str | Path | None, canary: str) -> list[Hit]:
    return graph_hits(repo, canary) + (sqlite_hits(db_path, canary) if db_path else [])
