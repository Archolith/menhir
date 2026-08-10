"""Helpers for embedding-dimension inference and compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.config import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.providers import ProviderConfig, ProviderKind

_KNOWN_EMBEDDING_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text-v1.5": 768,
    "bge-base-en-v1.5": 768,
    "bge-large-en-v1.5": 1024,
}


def infer_embedding_dimension_for_model(model_name: str) -> int | None:
    normalized = (model_name or "").strip().lower()
    if not normalized:
        return None
    for known_name, dim in _KNOWN_EMBEDDING_DIMENSIONS.items():
        if known_name in normalized:
            return dim
    return None


def expected_graphiti_embedding_dimension(settings: MemorySettings) -> int | None:
    provider = ProviderConfig.for_graphiti_embedder(settings)
    if provider.kind not in {ProviderKind.LOCAL, ProviderKind.OPENAI}:
        return None
    return infer_embedding_dimension_for_model(provider.embed_model)


def embedding_dimension_health(
    neo4j: Neo4jRepository,
    *,
    expected_dim: int | None = None,
) -> dict[str, Any]:
    """Report embedding-dimension health for the graph's semantic vectors.

    When ``expected_dim`` is None (the configured embedder's dimension can't be
    inferred), the wrong-dimension counts are reported as 0 (nothing to compare
    against) but the raw per-dimension distributions and the model-agnostic
    ``mixed`` flag are still computed — so a mixed-dimension graph is caught even
    for an unknown embedding model.
    """
    entity_rows = neo4j.execute(
        """
        MATCH (n:Entity)
        WHERE n.name_embedding IS NOT NULL
        RETURN size(n.name_embedding) AS dim, count(n) AS count
        ORDER BY count DESC
        """
    )
    community_rows = neo4j.execute(
        """
        MATCH (n:Community)
        WHERE n.name_embedding IS NOT NULL
        RETURN size(n.name_embedding) AS dim, count(n) AS count
        ORDER BY count DESC
        """
    )
    edge_rows = neo4j.execute(
        """
        MATCH ()-[r]->()
        WHERE r.fact_embedding IS NOT NULL
        RETURN size(r.fact_embedding) AS dim, count(r) AS count
        ORDER BY count DESC
        """
    )

    # Missing (NULL) embeddings on SEMANTIC nodes are as broken as wrong-dimension
    # ones: invisible to vector recall. STRUCTURAL nodes (code files/dirs/symbols
    # from the structure scanner, identified by n.structure_role) are a path/symbol
    # index and are intentionally never embedded -- they must be excluded here, or
    # health reports a false problem and a backfill would pollute the vector space.
    null_entity_count = int((neo4j.execute(
        "MATCH (n:Entity) WHERE n.name_embedding IS NULL AND n.structure_role IS NULL "
        "RETURN count(n) AS c"
    ) or [{}])[0].get("c", 0) or 0)
    null_community_count = int((neo4j.execute(
        "MATCH (n:Community) WHERE n.name_embedding IS NULL AND n.structure_role IS NULL "
        "RETURN count(n) AS c"
    ) or [{}])[0].get("c", 0) or 0)
    # ANCHORED_TO and other edges touching a structural node are structural
    # anchors ("Memory linked to code file: ..."), not semantic facts -- exclude.
    null_edge_count = int((neo4j.execute(
        "MATCH (a)-[r]->(b) WHERE r.fact IS NOT NULL AND r.fact_embedding IS NULL "
        "AND a.structure_role IS NULL AND b.structure_role IS NULL RETURN count(r) AS c"
    ) or [{}])[0].get("c", 0) or 0)

    # Only meaningful when we know the target dimension; otherwise report 0 wrong
    # (there is nothing to compare against) and rely on the model-agnostic `mixed`
    # signal below.
    if expected_dim is None:
        wrong_entity_count = wrong_community_count = wrong_edge_count = 0
    else:
        wrong_entity_count = sum(int(row.get("count") or 0) for row in entity_rows if int(row.get("dim") or -1) != expected_dim)
        wrong_community_count = sum(int(row.get("count") or 0) for row in community_rows if int(row.get("dim") or -1) != expected_dim)
        wrong_edge_count = sum(int(row.get("count") or 0) for row in edge_rows if int(row.get("dim") or -1) != expected_dim)

    entity_dims = {int(row.get("dim") or 0): int(row.get("count") or 0) for row in entity_rows}
    community_dims = {int(row.get("dim") or 0): int(row.get("count") or 0) for row in community_rows}
    edge_dims = {int(row.get("dim") or 0): int(row.get("count") or 0) for row in edge_rows}

    # Model-agnostic corruption signal: the graph holds vectors of more than one
    # distinct (non-zero) dimension. This is the signature of an embedder change
    # mid-life and is broken regardless of whether the current model is known.
    def _distinct(dims: dict[int, int]) -> list[int]:
        return sorted(d for d in dims if d > 0)

    mixed = (
        len(_distinct(entity_dims)) > 1
        or len(_distinct(community_dims)) > 1
        or len(_distinct(edge_dims)) > 1
    )

    return {
        "expected_dim": expected_dim,
        "ok": (
            wrong_entity_count == 0 and wrong_community_count == 0 and wrong_edge_count == 0
            and null_entity_count == 0 and null_community_count == 0 and null_edge_count == 0
            and not mixed
        ),
        "mixed": mixed,
        "wrong_entity_count": wrong_entity_count,
        "wrong_community_count": wrong_community_count,
        "wrong_edge_count": wrong_edge_count,
        "null_entity_count": null_entity_count,
        "null_community_count": null_community_count,
        "null_edge_count": null_edge_count,
        "entity_dims": entity_dims,
        "community_dims": community_dims,
        "edge_dims": edge_dims,
    }


@dataclass(frozen=True)
class EmbeddingCompatibility:
    """Verdict on whether the configured embedder matches the graph's vectors.

    ``blocking`` is True only when a mismatch is *certain* — either the graph holds
    multiple embedding dimensions (an embedder was changed mid-life, always broken),
    or the current model's dimension is known and stored vectors of a different
    dimension exist. An unknown embedding model over a uniform graph is
    unverifiable and is deliberately NOT blocking, so a legitimate unlisted model
    never locks the operator out of their own memory.
    """

    ok: bool
    blocking: bool
    expected_dim: int | None
    model_name: str
    stored_entity_dims: dict[int, int]
    mixed: bool
    reason: str

    def banner(self) -> str:
        return render_embedding_dimension_banner(self)


def evaluate_embedding_compatibility(
    neo4j: Neo4jRepository, settings: MemorySettings
) -> EmbeddingCompatibility:
    """Single source of truth for embedding/graph dimension compatibility.

    Used by both the ``serve`` startup guard (hard block) and the runtime health
    surface, so the classification cannot drift between them.
    """
    provider = ProviderConfig.for_graphiti_embedder(settings)
    model_name = provider.embed_model or ""
    expected_dim = expected_graphiti_embedding_dimension(settings)
    health = embedding_dimension_health(neo4j, expected_dim=expected_dim)

    stored = {int(d): int(c) for d, c in health["entity_dims"].items() if int(d) > 0}
    mixed = bool(health["mixed"])
    wrong = (
        int(health["wrong_entity_count"])
        + int(health["wrong_community_count"])
        + int(health["wrong_edge_count"])
    )

    blocking = mixed or (expected_dim is not None and wrong > 0)
    if blocking:
        if mixed:
            reason = (
                f"graph holds multiple embedding dimensions {sorted(stored)} — "
                "an embedding model was changed after data was written"
            )
        else:
            reason = (
                f"{wrong} stored vector(s) differ from the current embedder "
                f"dimension {expected_dim}"
            )
    elif expected_dim is None and stored:
        reason = (
            f"embedding model '{model_name}' dimension is unknown; cannot verify "
            f"against stored dims {sorted(stored)} (not blocking)"
        )
    else:
        reason = "ok"

    return EmbeddingCompatibility(
        ok=not blocking,
        blocking=blocking,
        expected_dim=expected_dim,
        model_name=model_name,
        stored_entity_dims=stored,
        mixed=mixed,
        reason=reason,
    )


def render_embedding_dimension_banner(compat: EmbeddingCompatibility) -> str:
    """Render an unmissable multi-line banner for a blocking dimension mismatch."""
    dims = ", ".join(f"{d}:{c}" for d, c in sorted(compat.stored_entity_dims.items())) or "(none)"
    expected = compat.expected_dim if compat.expected_dim is not None else "?"
    bar = "=" * 72
    return "\n".join(
        [
            "",
            bar,
            "  FATAL: EMBEDDING DIMENSION MISMATCH -- refusing to start",
            "-" * 72,
            f"  configured embedder : {compat.model_name or '(unknown)'} (dim={expected})",
            f"  stored graph vectors: {{{dims}}}  (Entity.name_embedding, dim:count)",
            f"  reason              : {compat.reason}",
            "",
            "  While this mismatch stands, vector.similarity queries fail with a",
            "  dimension error: semantic recall is broken and newly ingested",
            "  memories can be silently lost.",
            "",
            "  Fix one of:",
            "    - restore the embedding model the graph was built with, or",
            "    - re-embed the graph to the new model:",
            "        python scripts/repair_embedding_dimensions.py --apply",
            bar,
            "",
        ]
    )
