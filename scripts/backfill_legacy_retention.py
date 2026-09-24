"""Inventory legacy flags and backfill individually reviewed source links.

The inventory covers every flagged Entity/Episodic and every processed source episode,
including unflagged sources. A resolved UUID and MENTIONS edge are candidates, not proof
of historical provenance: reconciliation once selected a Graphiti episode by name.
No entity flag or bootstrap scope is changed by this script.

Apply only during a quiesced maintenance window with a verified recoverable backup.
Production use must follow deploy/RUNBOOK.md and install a #142-capable runtime
before writers resume. This script does not classify direct entity-flag intent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from menhir.config import MemorySettings
from menhir.domain.retention import same_tenant_cypher
from menhir.domain.structural_memory import (
    legacy_structural_memory_cypher,
    non_structural_memory_cypher,
)
from menhir.infrastructure.neo4j import Neo4jRepository

KIND = "menhir-legacy-retention-v1"
LIMIT = 100_000

FLAGS = """
MATCH (n)
WHERE (n:Entity OR n:Episodic) AND coalesce(n.user_flagged, false)
RETURN n.uuid AS uuid, labels(n) AS labels, n.namespace AS namespace,
       n.group_id AS group_id, n.bootstrap_scope AS bootstrap_scope,
       n.structure_role AS structure_role, n.processing_state AS processing_state,
       n.resolved_episode_uuid AS resolved_episode_uuid
ORDER BY uuid
LIMIT $limit
"""

SOURCES = f"""
MATCH (s:Episodic)
WHERE s.processing_state IS NOT NULL OR s.resolved_episode_uuid IS NOT NULL
OPTIONAL MATCH (g:Episodic {{uuid: s.resolved_episode_uuid}})
OPTIONAL MATCH (g)-[:MENTIONS]->(e:Entity)
OPTIONAL MATCH (s)-[r:RETENTION_SOURCE]->(e)
RETURN s.uuid AS source_uuid, s.name AS source_name,
       s.processing_state AS source_state, s.namespace AS source_namespace,
       s.group_id AS source_group_id, s.resolved_episode_uuid AS resolved_episode_uuid,
       g.uuid AS graphiti_uuid, g.name AS graphiti_name,
       s.content AS source_content, g.content AS graphiti_content,
       toString(s.created_at) AS source_created_at,
       toString(g.created_at) AS graphiti_created_at,
       s.content IS NOT NULL AND s.content = g.content AS content_matches,
       s.created_at IS NOT NULL AND g.created_at IS NOT NULL
         AND s.created_at <= g.created_at AS chronology_valid,
       g.namespace AS graphiti_namespace, g.group_id AS graphiti_group_id,
       e.uuid AS entity_uuid, e.namespace AS entity_namespace,
       e.group_id AS entity_group_id, e.structure_role AS structure_role,
       e.is_evidence_projection AS is_evidence_projection,
       ({legacy_structural_memory_cypher("e")}) AS legacy_structural,
       size(coalesce(e.merge_audit, [])) AS merge_audit_count,
       r IS NOT NULL AS already_linked, r.direct AS existing_direct
ORDER BY source_uuid, entity_uuid
LIMIT $limit
"""


def _tenant(namespace: Any, group_id: Any) -> str:
    raw = namespace if namespace is not None else group_id
    return "default" if raw in (None, "", "default") else str(raw)


def _reason(row: dict[str, Any]) -> str:
    if not row.get("source_uuid") or not row.get("resolved_episode_uuid"):
        return "MISSING_BINDING"
    if row["source_uuid"] == row["resolved_episode_uuid"]:
        return "SELF_BINDING"
    if row.get("source_state") != "READY":
        return "NOT_READY"
    if not row.get("graphiti_uuid"):
        return "MISSING_GRAPHITI_EPISODE"
    if row.get("source_name") != row.get("graphiti_name"):
        return "NAME_MISMATCH"
    if not row.get("content_matches"):
        return "CONTENT_MISMATCH"
    if not row.get("chronology_valid"):
        return "CHRONOLOGY_UNTRUSTED"
    if _tenant(row.get("source_namespace"), row.get("source_group_id")) != _tenant(
        row.get("graphiti_namespace"), row.get("graphiti_group_id")
    ):
        return "GRAPHITI_TENANT_MISMATCH"
    if not row.get("entity_uuid"):
        return "NO_MENTIONED_ENTITY"
    if _tenant(row.get("source_namespace"), row.get("source_group_id")) != _tenant(
        row.get("entity_namespace"), row.get("entity_group_id")
    ):
        return "ENTITY_TENANT_MISMATCH"
    if row.get("is_evidence_projection"):
        return "EVIDENCE_PROJECTION"
    if row.get("structure_role") is not None or row.get("legacy_structural"):
        return "STRUCTURAL_ENTITY"
    if row.get("merge_audit_count"):
        return "MERGE_LINEAGE_UNKNOWN"
    if row.get("already_linked"):
        return "ALREADY_LINKED"
    return "REVIEW_REQUIRED"


def _inventory(
    repo: Any, *, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    flags = [dict(row) for row in repo.execute(FLAGS, {"limit": limit + 1})]
    sources = [dict(row) for row in repo.execute(SOURCES, {"limit": limit + 1})]
    if len(flags) > limit or len(sources) > limit:
        raise ValueError(
            "inventory exceeds limit; raise --limit to obtain a complete census"
        )
    for row in flags:
        row["labels"] = sorted(row.get("labels") or [])
    flag_ids = [row.get("uuid") for row in flags]
    if None in flag_ids or len(set(flag_ids)) != len(flag_ids):
        raise ValueError("flag inventory has missing or duplicate UUIDs")
    source_keys = [
        (row.get("source_uuid"), row.get("graphiti_uuid"), row.get("entity_uuid"))
        for row in sources
    ]
    if len(set(source_keys)) != len(source_keys):
        raise ValueError("source inventory has duplicate source/Graphiti/entity rows")
    for row in sources:
        row["reason"] = _reason(row)
        for name in ("source_content", "graphiti_content"):
            content = row.pop(name, None)
            row[f"{name}_sha256"] = (
                hashlib.sha256(content.encode("utf-8")).hexdigest()
                if isinstance(content, str)
                else None
            )
    return flags, sources


def _digest(flags: list[dict[str, Any]], sources: list[dict[str, Any]]) -> str:
    material = json.dumps(
        [flags, sources], sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def plan(repo: Neo4jRepository, uri: str, out: Path, limit: int) -> None:
    flags, sources = _inventory(repo, limit=limit)
    header = {
        "kind": KIND,
        "uri": uri,
        "database": repo.database,
        "flag_count": len(flags),
        "source_row_count": len(sources),
        "inventory_sha256": _digest(flags, sources),
    }
    rows = [{"kind": "flag", **row} for row in flags]
    rows += [
        {"kind": "source", **row, "decision": "UNREVIEWED", "proof": ""}
        for row in sources
    ]
    out.write_text(
        "\n".join(
            json.dumps(row, sort_keys=True, default=str) for row in [header, *rows]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"READ-ONLY: {len(flags)} flags, {len(sources)} source rows")
    print(json.dumps(Counter(row["reason"] for row in sources), sort_keys=True))
    print(f"Manifest: {out}")


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not rows or rows[0].get("kind") != KIND:
        raise ValueError("invalid legacy retention manifest")
    return rows[0], rows[1:]


def _approved(
    repo: Any, uri: str, database: str, path: Path, limit: int
) -> list[dict[str, Any]]:
    header, rows = _load_manifest(path)
    flags, sources = _inventory(repo, limit=limit)
    if (header.get("uri"), header.get("database"), header.get("inventory_sha256")) != (
        uri,
        database,
        _digest(flags, sources),
    ):
        raise ValueError(
            "graph inventory changed since plan; regenerate and re-review manifest"
        )
    actual = [{"kind": "flag", **row} for row in flags]
    actual += [{"kind": "source", **row} for row in sources]
    if len(actual) != len(rows):
        raise ValueError("manifest row count differs from live inventory")
    approved: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str]] = set()
    for expected, row in zip(actual, rows, strict=True):
        if {k: v for k, v in row.items() if k not in ("decision", "proof")} != expected:
            raise ValueError("manifest inventory rows were modified or reordered")
        if row["kind"] == "flag":
            continue
        key = (
            str(row.get("source_uuid")),
            str(row.get("graphiti_uuid")),
            str(row.get("entity_uuid")),
        )
        if key in keys:
            raise ValueError("duplicate source/Graphiti/entity row in inventory")
        keys.add(key)
        decision = row.get("decision")
        if decision not in ("UNREVIEWED", "SKIP", "APPROVE"):
            raise ValueError(f"invalid review decision for {row['source_uuid']}")
        if decision == "APPROVE":
            if (
                row["reason"] != "REVIEW_REQUIRED"
                or not str(row.get("proof") or "").strip()
            ):
                raise ValueError(
                    "APPROVE requires an eligible row and explicit provenance proof"
                )
            approved.append(row)
    return approved


def apply(repo: Any, approved: list[dict[str, Any]]) -> int:
    """Additive write. The maintenance procedure must fence other writers."""
    changed = 0
    for row in approved:
        result = repo.execute(
            f"""
            MATCH (s:Episodic {{uuid:$source}}), (g:Episodic {{uuid:$graphiti}}),
                  (g)-[:MENTIONS]->(e:Entity {{uuid:$entity}})
            WHERE s.resolved_episode_uuid = g.uuid AND s.processing_state = 'READY'
              AND s.name = g.name AND s.content IS NOT NULL AND s.content = g.content
              AND s.created_at IS NOT NULL AND g.created_at IS NOT NULL
              AND s.created_at <= g.created_at
              AND size(coalesce(e.merge_audit, [])) = 0
              AND ({non_structural_memory_cypher("e")})
              AND {same_tenant_cypher("s", "g")}
              AND {same_tenant_cypher("s", "e")}
            MERGE (s)-[r:RETENTION_SOURCE]->(e)
            ON CREATE SET r.direct = true
            RETURN count(r) AS matched, collect(r.direct) AS direct_values
            """,
            {
                "source": row["source_uuid"],
                "graphiti": row["graphiti_uuid"],
                "entity": row["entity_uuid"],
            },
        )
        if (
            not result
            or result[0]["matched"] != 1
            or result[0]["direct_values"] != [True]
        ):
            raise ValueError(
                f"candidate changed at write: {row['source_uuid']} -> {row['entity_uuid']}"
            )
        changed += 1
    return changed


def verify(repo: Any, uri: str, database: str, path: Path, limit: int) -> int:
    """Require the exact reviewed delta and unchanged flag inventory before writers resume."""
    header, rows = _load_manifest(path)
    before_flags = [
        {k: v for k, v in row.items() if k != "kind"}
        for row in rows
        if row.get("kind") == "flag"
    ]
    before_sources = [
        {k: v for k, v in row.items() if k not in ("kind", "decision", "proof")}
        for row in rows
        if row.get("kind") == "source"
    ]
    if (header.get("uri"), header.get("database"), header.get("inventory_sha256")) != (
        uri,
        database,
        _digest(before_flags, before_sources),
    ):
        raise ValueError("manifest inventory is invalid")
    expected_sources = []
    approved = 0
    for original, row in zip(
        before_sources, (r for r in rows if r.get("kind") == "source"), strict=True
    ):
        expected = dict(original)
        if row.get("decision") == "APPROVE":
            if (
                expected["reason"] != "REVIEW_REQUIRED"
                or not str(row.get("proof") or "").strip()
            ):
                raise ValueError("invalid approved row in manifest")
            expected.update(
                reason="ALREADY_LINKED", already_linked=True, existing_direct=True
            )
            approved += 1
        expected_sources.append(expected)
    flags, sources = _inventory(repo, limit=limit)
    if flags != before_flags or sources != expected_sources:
        raise ValueError("post-apply graph differs from the exact reviewed delta")
    return approved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri")
    parser.add_argument("--limit", type=int, default=LIMIT)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="read-only full inventory")
    p.add_argument("--out", type=Path, required=True)
    a = sub.add_parser("apply", help="dry-run by default; additive approved links only")
    a.add_argument("--manifest", type=Path, required=True)
    a.add_argument("--backup", type=Path, required=True)
    a.add_argument("--quiesced", action="store_true")
    a.add_argument("--yes", action="store_true")
    v = sub.add_parser("verify", help="read-only exact post-apply comparison")
    v.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    settings = MemorySettings.from_env()
    uri = args.uri or settings.neo4j_uri
    repo = Neo4jRepository(
        uri=uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    try:
        if args.command == "plan":
            plan(repo, uri, args.out, args.limit)
            return 0
        if args.command == "verify":
            print(
                f"Verified {verify(repo, uri, repo.database, args.manifest, args.limit)} "
                "reviewed links and unchanged flags"
            )
            return 0
        approved = _approved(repo, uri, repo.database, args.manifest, args.limit)
        print(
            f"Reviewed additive links: {len(approved)}; direct flags remain unchanged"
        )
        if not args.yes:
            print("DRY RUN: no graph writes")
            return 0
        if (
            not args.quiesced
            or not args.backup.is_file()
            or args.backup.stat().st_size == 0
        ):
            raise ValueError("apply requires --quiesced and a nonempty verified backup")

        # Re-inventory and write inside one transaction. A changed row rolls back every link.
        def work(tx: Any) -> int:
            current = _approved(tx, uri, repo.database, args.manifest, args.limit)
            return apply(tx, current)

        print(f"Applied {repo.execute_write(work)} additive links")
        print(
            f"Verified {verify(repo, uri, repo.database, args.manifest, args.limit)} "
            "reviewed links and unchanged flags"
        )
        return 0
    except ValueError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
