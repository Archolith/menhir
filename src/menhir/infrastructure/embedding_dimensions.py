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


#: Process-lifetime memo for the startup sweep (CF-173).
#:
#: `embedding_dimension_health` runs six unbounded scans, two of them over EVERY relationship in
#: the graph with no label to narrow them, and startup calls it twice: once from the `serve`
#: guard in `cli/__init__.py` and once from `core/runtime_preflight`. Two full sweeps, moments
#: apart, over a graph nothing has had a chance to change in between. This makes the second one
#: free.
#:
#: Deliberately keyed on the connection target and the expected dimension rather than being a
#: bare flag: a different database or a changed embedder is a different question, and answering
#: it from a memo would be worse than the cost it saves. Not invalidated by time, because its
#: only purpose is the startup pair -- a long-running process that wants a fresh answer should
#: call `reset_embedding_dimension_cache()`, which the health surface can do if it ever needs to.
#:
#: Opt-IN, not opt-out. Only the two startup callers pass `use_cache=True`. A memo that every
#: caller got by default would change behaviour for paths this finding says nothing about, and an
#: un-invalidated memo answering a question someone asked live is a worse defect than the double
#: sweep it saves.
_HEALTH_CACHE: dict[tuple[str, str, int | None], dict[str, Any]] = {}


def reset_embedding_dimension_cache() -> None:
    """Drop the memoized startup sweep. For tests, and for any caller that needs a live answer."""
    _HEALTH_CACHE.clear()


def embedding_dimension_health(
    neo4j: Neo4jRepository,
    *,
    expected_dim: int | None = None,
    use_cache: bool = False,
) -> dict[str, Any]:
    """Report embedding-dimension health for the graph's semantic vectors.

    When ``expected_dim`` is None (the configured embedder's dimension can't be
    inferred), the wrong-dimension counts are reported as 0 (nothing to compare
    against) but the raw per-dimension distributions and the model-agnostic
    ``mixed`` flag are still computed — so a mixed-dimension graph is caught even
    for an unknown embedding model. Zero-length vectors (``size() == 0``) are
    counted separately as ``zero_*_count`` and always count as wrong, regardless
    of ``expected_dim``.

    With ``use_cache=True`` the result is memoized per (uri, database, expected_dim) for the
    process lifetime. Only the two startup callers opt in; see ``_HEALTH_CACHE`` (CF-173).
    """
    cache_key = (str(getattr(neo4j, "uri", "")), str(getattr(neo4j, "database", "")), expected_dim)
    if use_cache and cache_key in _HEALTH_CACHE:
        return dict(_HEALTH_CACHE[cache_key])

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

    entity_dims = {int(row.get("dim") or 0): int(row.get("count") or 0) for row in entity_rows}
    community_dims = {int(row.get("dim") or 0): int(row.get("count") or 0) for row in community_rows}
    edge_dims = {int(row.get("dim") or 0): int(row.get("count") or 0) for row in edge_rows}

    # Zero-length vectors are a first-class corruption signal (CF-187): a stored
    # empty list is a real, non-NULL property whose size() is 0, so it is invisible
    # to the IS NULL sweeps below. Any vector of dimension 0 is wrong against every
    # expected dimension, known or not. Counted from the dim distributions above --
    # no extra Cypher query needed, the rows already come back grouped by size().
    zero_entity_count = int(entity_dims.get(0, 0) or 0)
    zero_community_count = int(community_dims.get(0, 0) or 0)
    zero_edge_count = int(edge_dims.get(0, 0) or 0)

    # Only meaningful when we know the target dimension; otherwise report 0 wrong
    # (there is nothing to compare against) and rely on the model-agnostic `mixed`
    # signal below. Zero-length vectors are the deliberate exception: they are wrong against
    # every dimension, so they count as wrong in BOTH branches -- explicitly in the unknown-dim
    # branch, and already included by `dim != expected_dim` in the known-dim one.
    if expected_dim is None:
        # Nothing to compare against, so the zero counts are the ONLY wrong-ness we can assert --
        # and this is the branch CF-187 is about, since expected_dim is None by default.
        wrong_entity_count = zero_entity_count
        wrong_community_count = zero_community_count
        wrong_edge_count = zero_edge_count
    else:
        # `dim != expected_dim` already counts the zero-dimension rows, so they must NOT be added
        # again below: doing so double-counted them and made `reason` report twice the number of
        # bad vectors that exist (the verdict booleans were unaffected, the operator-facing count
        # was not).
        wrong_entity_count = sum(int(row.get("count") or 0) for row in entity_rows if int(row.get("dim") or -1) != expected_dim)
        wrong_community_count = sum(int(row.get("count") or 0) for row in community_rows if int(row.get("dim") or -1) != expected_dim)
        wrong_edge_count = sum(int(row.get("count") or 0) for row in edge_rows if int(row.get("dim") or -1) != expected_dim)

    # Model-agnostic corruption signal: the graph holds vectors of more than one
    # distinct (non-zero) dimension. This is the signature of an embedder change
    # mid-life and is broken regardless of whether the current model is known.
    # Dimension 0 is deliberately excluded from `mixed`: zeros have their own
    # corruption signal (zero_*_count), and `mixed` means "two real dimensions",
    # so a zero vector must not be read as a second dimension (CF-187).
    def _distinct(dims: dict[int, int]) -> list[int]:
        return sorted(d for d in dims if d > 0)

    mixed = (
        len(_distinct(entity_dims)) > 1
        or len(_distinct(community_dims)) > 1
        or len(_distinct(edge_dims)) > 1
    )

    result = {
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
        "zero_entity_count": zero_entity_count,
        "zero_community_count": zero_community_count,
        "zero_edge_count": zero_edge_count,
        "entity_dims": entity_dims,
        "community_dims": community_dims,
        "edge_dims": edge_dims,
    }
    if use_cache:
        _HEALTH_CACHE[cache_key] = dict(result)
    return result


@dataclass(frozen=True)
class EmbeddingCompatibility:
    """Verdict on whether the configured embedder matches the graph's vectors.

    ``ok`` means the graph's semantic embeddings are healthy — no wrong-dimension
    vectors, no null (missing) vectors, no zero-length vectors, and no mixed
    dimensions. It is the same health predicate ``embedding_dimension_health``
    computes, so the two cannot drift.

    ``blocking`` is True only when a mismatch is *certain* — either the graph holds
    multiple embedding dimensions (an embedder was changed mid-life, always broken),
    the current model's dimension is known and stored vectors of a different
    dimension exist, or stored vectors have dimension 0 (a certain corruption). An
    unknown embedding model over a uniform graph is unverifiable and is deliberately
    NOT blocking, so a legitimate unlisted model never locks the operator out of
    their own memory.

    ``missing_vectors`` is the count of rows with no embedding at all (NULL
    ``name_embedding``/``fact_embedding``). Those are invisible to vector recall but
    are deliberately NOT blocking — a backfill gap must not lock the operator out —
    so a caller can warn loudly without refusing to start.
    """

    ok: bool
    blocking: bool
    expected_dim: int | None
    model_name: str
    stored_entity_dims: dict[int, int]
    mixed: bool
    missing_vectors: int
    reason: str

    def banner(self) -> str:
        return render_embedding_dimension_banner(self)


def evaluate_embedding_compatibility(
    neo4j: Neo4jRepository, settings: MemorySettings, *, use_cache: bool = False
) -> EmbeddingCompatibility:
    """Classify embedding/graph dimension compatibility for the ``serve`` startup guard.

    This used to claim to be the "single source of truth ... used by both the serve startup guard
    and the runtime health surface, so the classification cannot drift between them". It is not:
    `core/runtime_preflight` calls `embedding_dimension_health` directly, with its own
    `expected_dim is not None` gate and its own reading of the result. The two paths already
    differ, and the comment describing them as one was the seventeenth instance in this codebase
    of a comment asserting an invariant the code does not implement (CF-173).

    They now at least share the underlying sweep through `_HEALTH_CACHE`, so they see the same
    numbers even though they classify them separately.

    Note on why the ``serve`` guard is NOT gated on ``expected_dim is not None`` to match
    preflight, which is what CF-173 proposes: ``blocking`` is
    ``mixed or (expected_dim is not None and wrong > 0) or zero_vectors > 0``, and ``mixed``
    (plus the model-agnostic zero-length signal) is the corruption signal that exists precisely
    for the case where the dimension cannot be inferred. Gating the guard would delete the check
    in the only situation it was written for.
    """
    provider = ProviderConfig.for_graphiti_embedder(settings)
    model_name = provider.embed_model or ""
    expected_dim = expected_graphiti_embedding_dimension(settings)
    health = embedding_dimension_health(neo4j, expected_dim=expected_dim, use_cache=use_cache)

    stored = {int(d): int(c) for d, c in health["entity_dims"].items() if int(d) > 0}
    mixed = bool(health["mixed"])
    wrong = (
        int(health["wrong_entity_count"])
        + int(health["wrong_community_count"])
        + int(health["wrong_edge_count"])
    )
    missing_vectors = (
        int(health["null_entity_count"])
        + int(health["null_community_count"])
        + int(health["null_edge_count"])
    )
    zero_vectors = (
        int(health["zero_entity_count"])
        + int(health["zero_community_count"])
        + int(health["zero_edge_count"])
    )

    blocking = (
        mixed
        or (expected_dim is not None and wrong > 0)
        or zero_vectors > 0
    )
    if blocking:
        if mixed:
            reason = (
                f"graph holds multiple embedding dimensions {sorted(stored)} — "
                "an embedding model was changed after data was written"
            )
        elif zero_vectors > 0:
            reason = (
                f"{zero_vectors} stored vector(s) have dimension 0 (empty embeddings); "
                "they are corrupted and invisible to vector recall"
            )
        else:
            reason = (
                f"{wrong} stored vector(s) differ from the current embedder "
                f"dimension {expected_dim}"
            )
    elif missing_vectors > 0:
        reason = (
            f"{missing_vectors} stored node(s)/edge(s) have no embedding; they are "
            "invisible to vector recall (not blocking)"
        )
    elif expected_dim is None and stored:
        reason = (
            f"embedding model '{model_name}' dimension is unknown; cannot verify "
            f"against stored dims {sorted(stored)} (not blocking)"
        )
    else:
        reason = "ok"

    return EmbeddingCompatibility(
        ok=bool(health["ok"]),
        blocking=blocking,
        expected_dim=expected_dim,
        model_name=model_name,
        stored_entity_dims=stored,
        mixed=mixed,
        missing_vectors=missing_vectors,
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
