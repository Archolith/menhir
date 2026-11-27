from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from menhir.config import MemorySettings
from menhir.infrastructure.embedding_dimensions import (
    embedding_dimension_health,
    expected_graphiti_embedding_dimension,
    fit_embedding_dimension,
    semantic_fact_edge_pattern,
)
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.observability import build_async_openai_client
from menhir.infrastructure.providers import ProviderConfig

DEFAULT_BATCH_SIZE = 128
# One type or a `|` join of types (semantic_fact_edge_pattern()); nothing else is interpolated.
_REL_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\|[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _load_settings() -> MemorySettings:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(os.getenv("ENV_FILE") or repo_root / ".env")
    return MemorySettings.from_env()


def _query_rows(neo4j: Neo4jRepository, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return neo4j.execute(query, params or {})


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _snapshot_records(neo4j: Neo4jRepository, *, expected_dim: int, output_dir: Path) -> dict[str, Any]:
    entity_rows = _query_rows(
        neo4j,
        """
        MATCH (n:Entity)
        WHERE n.name IS NOT NULL AND n.structure_role IS NULL
          AND (n.name_embedding IS NULL OR size(n.name_embedding) <> $expected_dim)
        RETURN elementId(n) AS element_id,
               coalesce(n.uuid, '') AS uuid,
               labels(n) AS labels,
               n.name AS name,
               n.summary AS summary,
               n.content AS content,
               properties(n) AS properties
        ORDER BY n.uuid
        """,
        {"expected_dim": expected_dim},
    )
    community_rows = _query_rows(
        neo4j,
        """
        MATCH (n:Community)
        WHERE n.name IS NOT NULL AND n.structure_role IS NULL
          AND (n.name_embedding IS NULL OR size(n.name_embedding) <> $expected_dim)
        RETURN elementId(n) AS element_id,
               coalesce(n.uuid, '') AS uuid,
               labels(n) AS labels,
               n.name AS name,
               n.summary AS summary,
               n.content AS content,
               properties(n) AS properties
        ORDER BY n.uuid
        """,
        {"expected_dim": expected_dim},
    )
    # Typed, not label-less (CF-252). This is the query that decides what gets EMBEDDED, so an
    # anchor edge selected here does not merely inflate a count -- it puts
    # 'Memory linked to code file: <path>' into the semantic vector space. The endpoint
    # structure_role test alone did not hold: 285 production code-file entities lack the stamp.
    edge_rows = _query_rows(
        neo4j,
        f"""
        MATCH (a)-[r:{semantic_fact_edge_pattern()}]->(b)
        WHERE r.fact IS NOT NULL AND a.structure_role IS NULL AND b.structure_role IS NULL
          AND (r.fact_embedding IS NULL OR size(r.fact_embedding) <> $expected_dim)
        RETURN elementId(r) AS element_id,
               coalesce(r.uuid, '') AS uuid,
               type(r) AS rel_type,
               coalesce(a.uuid, '') AS start_uuid,
               coalesce(b.uuid, '') AS end_uuid,
               r.fact AS fact,
               properties(r) AS properties
        ORDER BY coalesce(r.uuid, elementId(r))
        """,
        {"expected_dim": expected_dim},
    )

    manifest = {
        "created_at": _utc_stamp(),
        "expected_dim": expected_dim,
        "entity_count": len(entity_rows),
        "community_count": len(community_rows),
        "edge_count": len(edge_rows),
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_jsonl(output_dir / "entities.jsonl", entity_rows)
    _write_jsonl(output_dir / "communities.jsonl", community_rows)
    _write_jsonl(output_dir / "relationships.jsonl", edge_rows)
    return {
        "manifest": manifest,
        "entities": entity_rows,
        "communities": community_rows,
        "relationships": edge_rows,
    }


def _clear_wrong_dimensions(neo4j: Neo4jRepository, *, expected_dim: int) -> dict[str, int]:
    removed_entity_rows = _query_rows(
        neo4j,
        """
        MATCH (n:Entity)
        WHERE n.name_embedding IS NOT NULL AND size(n.name_embedding) <> $expected_dim
        WITH collect(n) AS nodes
        FOREACH (n IN nodes | REMOVE n.name_embedding)
        RETURN size(nodes) AS removed_nodes
        """,
        {"expected_dim": expected_dim},
    )
    removed_community_rows = _query_rows(
        neo4j,
        """
        MATCH (n:Community)
        WHERE n.name_embedding IS NOT NULL AND size(n.name_embedding) <> $expected_dim
        WITH collect(n) AS nodes
        FOREACH (n IN nodes | REMOVE n.name_embedding)
        RETURN size(nodes) AS removed_nodes
        """,
        {"expected_dim": expected_dim},
    )
    # Deliberately NOT typed, unlike the selection query above: this REMOVES wrong-dimension
    # vectors, so casting the widest net is the safe direction. If some type outside
    # SEMANTIC_FACT_EDGE_TYPES ever held a stale vector, it should still be cleared.
    removed_edge_rows = _query_rows(
        neo4j,
        """
        MATCH ()-[r]->()
        WHERE r.fact_embedding IS NOT NULL AND size(r.fact_embedding) <> $expected_dim
        WITH collect(r) AS rels
        FOREACH (r IN rels | REMOVE r.fact_embedding)
        RETURN size(rels) AS removed_edges
        """,
        {"expected_dim": expected_dim},
    )
    return {
        "entities": int((removed_entity_rows[0] if removed_entity_rows else {}).get("removed_nodes", 0)),
        "communities": int((removed_community_rows[0] if removed_community_rows else {}).get("removed_nodes", 0)),
        "relationships": int((removed_edge_rows[0] if removed_edge_rows else {}).get("removed_edges", 0)),
    }


def _resolve_embed_client(settings: MemorySettings) -> tuple[Any, str, str]:
    provider = ProviderConfig.for_graphiti_embedder(settings)
    if not provider.supports_graphiti_openai_contract():
        raise RuntimeError(
            f"Current Graphiti embed provider is not OpenAI-compatible: {provider.kind.value}"
        )
    if not provider.embed_model:
        raise RuntimeError("Current Graphiti embed model is blank.")
    base_url = provider.base_url
    client = build_async_openai_client(
        base_url=base_url,
        api_key=provider.api_key,
        settings=settings,
    )
    return client, provider.embed_model, base_url


async def _embed_texts(client: Any, *, model: str, texts: list[str]) -> list[list[float]]:
    response = await client.embeddings.create(
        model=model,
        input=texts,
    )
    data = list(getattr(response, "data", []))
    ordered = sorted(data, key=lambda item: int(getattr(item, "index", 0)))
    return [list(getattr(item, "embedding")) for item in ordered]


async def _reembed_nodes(
    neo4j: Neo4jRepository,
    *,
    client: Any,
    model: str,
    rows: list[dict[str, Any]],
    label: str,
    expected_dim: int | None = None,
) -> dict[str, int]:
    updated = 0
    skipped = 0
    stale = 0
    wrong_dimension = 0
    for start in range(0, len(rows), DEFAULT_BATCH_SIZE):
        batch = rows[start:start + DEFAULT_BATCH_SIZE]
        nonblank = [row for row in batch if str(row.get("name") or "").strip()]
        skipped += len(batch) - len(nonblank)
        if not nonblank:
            continue
        embeddings = await _embed_texts(
            client, model=model, texts=[str(row["name"]) for row in nonblank]
        )
        for row, embedding in zip(nonblank, embeddings, strict=True):
            if expected_dim is not None:
                embedding = fit_embedding_dimension(embedding, expected_dim)
                if embedding is None:
                    wrong_dimension += 1
                    continue
            matched = neo4j.execute(
                f"""
                MATCH (n:{label})
                WHERE elementId(n) = $element_id
                  AND coalesce(n.uuid, '') = $uuid
                  AND n.name = $name
                  AND all(l IN $labels WHERE l IN labels(n))
                SET n.name_embedding = $embedding
                RETURN count(n) AS matched
                """,
                {
                    "element_id": row["element_id"],
                    "uuid": str(row.get("uuid") or ""),
                    "name": row["name"],
                    "labels": list(row.get("labels") or [label]),
                    "embedding": embedding,
                },
            )
            if int((matched[0] if matched else {}).get("matched", 0)):
                updated += 1
            else:
                stale += 1
    return {"updated": updated, "skipped": skipped, "stale": stale, "wrong_dimension": wrong_dimension}


async def _reembed_relationships(
    neo4j: Neo4jRepository,
    *,
    client: Any,
    model: str,
    rows: list[dict[str, Any]],
    expected_dim: int | None = None,
) -> dict[str, int]:
    updated = 0
    skipped = 0
    stale = 0
    wrong_dimension = 0
    for start in range(0, len(rows), DEFAULT_BATCH_SIZE):
        batch = rows[start:start + DEFAULT_BATCH_SIZE]
        nonblank = [row for row in batch if str(row.get("fact") or "").strip()]
        skipped += len(batch) - len(nonblank)
        if not nonblank:
            continue
        embeddings = await _embed_texts(
            client, model=model, texts=[str(row["fact"]) for row in nonblank]
        )
        for row, embedding in zip(nonblank, embeddings, strict=True):
            rel_type = str(row.get("rel_type") or semantic_fact_edge_pattern())
            if not _REL_TYPE_RE.match(rel_type):  # a snapshot is a file: never interpolate it blindly
                stale += 1
                continue
            if expected_dim is not None:
                embedding = fit_embedding_dimension(embedding, expected_dim)
                if embedding is None:
                    wrong_dimension += 1
                    continue
            matched = neo4j.execute(
                f"""
                MATCH (a)-[r:{rel_type}]->(b)
                WHERE elementId(r) = $element_id
                  AND coalesce(r.uuid, '') = $uuid
                  AND r.fact = $fact
                  AND coalesce(a.uuid, '') = $start_uuid
                  AND coalesce(b.uuid, '') = $end_uuid
                SET r.fact_embedding = $embedding
                RETURN count(r) AS matched
                """,
                {
                    "element_id": row["element_id"],
                    "uuid": str(row.get("uuid") or ""),
                    "fact": row["fact"],
                    "start_uuid": str(row.get("start_uuid") or ""),
                    "end_uuid": str(row.get("end_uuid") or ""),
                    "embedding": embedding,
                },
            )
            if int((matched[0] if matched else {}).get("matched", 0)):
                updated += 1
            else:
                stale += 1
    return {"updated": updated, "skipped": skipped, "stale": stale, "wrong_dimension": wrong_dimension}


async def _run_apply(
    neo4j: Neo4jRepository,
    *,
    settings: MemorySettings,
    expected_dim: int,
    output_dir: Path,
) -> dict[str, Any]:
    snapshot = _snapshot_records(neo4j, expected_dim=expected_dim, output_dir=output_dir)
    cleared = _clear_wrong_dimensions(neo4j, expected_dim=expected_dim)
    client, model, base_url = _resolve_embed_client(settings)
    entity_result = await _reembed_nodes(
        neo4j,
        client=client,
        model=model,
        rows=list(snapshot["entities"]),
        label="Entity",
        expected_dim=expected_dim,
    )
    community_result = await _reembed_nodes(
        neo4j,
        client=client,
        model=model,
        rows=list(snapshot["communities"]),
        label="Community",
        expected_dim=expected_dim,
    )
    relationship_result = await _reembed_relationships(
        neo4j,
        client=client,
        model=model,
        rows=list(snapshot["relationships"]),
        expected_dim=expected_dim,
    )
    return {
        "snapshot_dir": str(output_dir),
        "embed_base_url": base_url,
        "embed_model": model,
        "cleared": cleared,
        "reembedded": {
            "entities": entity_result,
            "communities": community_result,
            "relationships": relationship_result,
        },
        "post_health": embedding_dimension_health(neo4j, expected_dim=expected_dim),
    }


async def _run_resume_from_snapshot(
    neo4j: Neo4jRepository,
    *,
    settings: MemorySettings,
    expected_dim: int,
    snapshot_dir: Path,
) -> dict[str, Any]:
    entities = _read_jsonl(snapshot_dir / "entities.jsonl")
    communities = _read_jsonl(snapshot_dir / "communities.jsonl")
    relationships = _read_jsonl(snapshot_dir / "relationships.jsonl")
    client, model, base_url = _resolve_embed_client(settings)
    entity_result = await _reembed_nodes(
        neo4j,
        client=client,
        model=model,
        rows=entities,
        label="Entity",
        expected_dim=expected_dim,
    )
    community_result = await _reembed_nodes(
        neo4j,
        client=client,
        model=model,
        rows=communities,
        label="Community",
        expected_dim=expected_dim,
    )
    relationship_result = await _reembed_relationships(
        neo4j,
        client=client,
        model=model,
        rows=relationships,
        expected_dim=expected_dim,
    )
    return {
        "snapshot_dir": str(snapshot_dir),
        "embed_base_url": base_url,
        "embed_model": model,
        "reembedded": {
            "entities": entity_result,
            "communities": community_result,
            "relationships": relationship_result,
        },
        "post_health": embedding_dimension_health(neo4j, expected_dim=expected_dim),
    }


def _refusal_exit(result: dict[str, Any]) -> int:
    """4 when any re-embedded vector was refused as shorter than expected (left without a vector)."""
    refused = sum(int(part.get("wrong_dimension", 0)) for part in result["reembedded"].values())
    return 4 if refused else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot, clear, and rebuild stale Neo4j embeddings after an embedder migration.",
        epilog="Exit codes: 0 ok; 1 post-repair health still not ok; 4 one or more vectors were "
        "refused for being shorter than the expected dimension (health ok, but those records "
        "have no vector). 1 wins over 4. A dry run (no --apply) always exits 0.",
    )
    parser.add_argument(
        "--expected-dim",
        type=int,
        default=None,
        help="Canonical embedding dimension to keep. Defaults to the current configured embedder when known.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Export affected records, clear wrong-dimension embeddings, and rebuild them with the current embedder.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for the backup snapshot. Defaults to .agent/backups/embedding-repair-<timestamp>/",
    )
    parser.add_argument(
        "--from-snapshot",
        default="",
        help="Resume only the re-embed phase from an existing snapshot directory.",
    )
    args = parser.parse_args()

    settings = _load_settings()
    expected_dim = int(args.expected_dim or expected_graphiti_embedding_dimension(settings) or 0)
    if expected_dim <= 0:
        raise SystemExit(
            "Could not infer the current embedder dimension. Re-run with --expected-dim <N>."
        )

    repo_root = Path(__file__).resolve().parents[1]
    output_dir = Path(args.output_dir) if args.output_dir else (
        repo_root / ".agent" / "backups" / f"embedding-repair-{_utc_stamp()}"
    )

    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    try:
        health = embedding_dimension_health(neo4j, expected_dim=expected_dim)
        print(json.dumps(health, indent=2))
        print()
        if args.from_snapshot:
            result = asyncio.run(
                _run_resume_from_snapshot(
                    neo4j,
                    settings=settings,
                    expected_dim=expected_dim,
                    snapshot_dir=Path(args.from_snapshot),
                )
            )
            print(json.dumps(result, indent=2, default=str))
            return _refusal_exit(result) if result["post_health"]["ok"] else 1

        if not args.apply:
            print("Dry run only. Re-run with --apply to snapshot, clear, and rebuild incompatible embeddings.")
            return 0

        result = asyncio.run(
            _run_apply(
                neo4j,
                settings=settings,
                expected_dim=expected_dim,
                output_dir=output_dir,
            )
        )
        print(json.dumps(result, indent=2, default=str))
        if not result["post_health"]["ok"]:
            return 1
        return _refusal_exit(result)
    finally:
        neo4j.close()


if __name__ == "__main__":
    raise SystemExit(main())
