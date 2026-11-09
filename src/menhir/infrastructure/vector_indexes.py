"""Neo4j vector indexes for similarity search (#574).

Neo4j uses a vector index only through ``db.index.vector.queryNodes`` / ``queryRelationships``;
``vector.similarity.cosine()`` in a MATCH always scans. This module owns the index table, the DDL,
the cached index state, and the rule that decides when an index-backed result is complete. The
search paths that use it fall back to the existing scan whenever an index is absent, not ONLINE,
of another dimension, or cannot prove its result complete.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VectorIndexSpec:
    name: str
    entity_type: str  # "NODE" or "RELATIONSHIP", as SHOW INDEXES reports it
    label: str
    prop: str

    def target(self) -> str:
        if self.entity_type == "RELATIONSHIP":
            return f"FOR ()-[r:{self.label}]-() ON r.{self.prop}"
        return f"FOR (n:{self.label}) ON n.{self.prop}"


ENTITY_NAME_EMBEDDING = VectorIndexSpec("entity_name_embedding_vector", "NODE", "Entity", "name_embedding")
RELATES_TO_FACT_EMBEDDING = VectorIndexSpec(
    "relates_to_fact_embedding_vector", "RELATIONSHIP", "RELATES_TO", "fact_embedding"
)
EPISODIC_CONTENT_EMBEDDING = VectorIndexSpec(
    "episodic_content_embedding_vector", "NODE", "Episodic", "content_embedding"
)
TYPED_ASSERTION_NAME_EMBEDDING = VectorIndexSpec(
    "typed_assertion_name_embedding_vector", "NODE", "TypedAssertion", "name_embedding"
)

VECTOR_INDEX_SPECS: tuple[VectorIndexSpec, ...] = (
    ENTITY_NAME_EMBEDDING,
    RELATES_TO_FACT_EMBEDDING,
    EPISODIC_CONTENT_EMBEDDING,
    TYPED_ASSERTION_NAME_EMBEDDING,
)

SHOW_VECTOR_INDEXES = (
    "SHOW VECTOR INDEXES YIELD name, state, entityType, labelsOrTypes, properties, options "
    "RETURN name, state, entityType, labelsOrTypes, properties, options"
)

#: ANN candidate counts. The first request oversamples the limit; an incomplete result doubles k
#: up to the cap, past which the caller scans. k is also HNSW's search breadth: on 17k clustered
#: 1536-d entities, k=64 skipped a true top-15 row in 5 of 200 searches and k=128 in 0 of 200.
ANN_OVERSAMPLE = 4
ANN_MIN_K = 128
ANN_MAX_K = 1024

#: Upper bound on |index score - exact score|. Quantized indexes (the Neo4j >= 5.23 default) were
#: measured 0.0075-0.0095 off on (1+cos)/2; unquantized float32 scores are exact to ~1e-7.
QUANTIZED_SCORE_MARGIN = 0.02
EXACT_SCORE_MARGIN = 1e-4

CACHE_TTL_S = 30.0


def vector_index_ddl(spec: VectorIndexSpec, dimensions: int, *, quantization: bool | None) -> str:
    """``CREATE VECTOR INDEX ... IF NOT EXISTS``; ``quantization=None`` leaves the server default."""
    config = [f"`vector.dimensions`: {int(dimensions)}", "`vector.similarity_function`: 'cosine'"]
    if quantization is not None:
        config.append(f"`vector.quantization.enabled`: {'true' if quantization else 'false'}")
    return (
        f"CREATE VECTOR INDEX {spec.name} IF NOT EXISTS {spec.target()} "
        f"OPTIONS {{indexConfig: {{{', '.join(config)}}}}}"
    )


@dataclass(frozen=True)
class VectorIndexStatus:
    name: str
    state: str
    dimensions: int | None
    quantized: bool

    @property
    def score_margin(self) -> float:
        return QUANTIZED_SCORE_MARGIN if self.quantized else EXACT_SCORE_MARGIN


def _covers(row: Mapping[str, Any], spec: VectorIndexSpec) -> bool:
    return (
        str(row.get("entityType") or "").upper() == spec.entity_type
        and list(row.get("labelsOrTypes") or []) == [spec.label]
        and list(row.get("properties") or []) == [spec.prop]
    )


def _status_from_row(row: Mapping[str, Any]) -> VectorIndexStatus:
    config = dict((row.get("options") or {}).get("indexConfig") or {})
    raw_dim = config.get("vector.dimensions")
    try:
        dimensions = int(raw_dim) if raw_dim is not None else None
    except (TypeError, ValueError):
        dimensions = None
    similarity = str(config.get("vector.similarity_function") or "").upper()
    state = str(row.get("state") or "UNKNOWN").upper()
    if similarity != "COSINE":
        # Euclidean scores are on another scale; never substitute them for cosine.
        state = f"UNUSABLE:{similarity or 'unknown'}"
    # Absent key = a server older than quantization support, which stores full floats.
    quantized = bool(config.get("vector.quantization.enabled", False))
    return VectorIndexStatus(str(row.get("name")), state, dimensions, quantized)


def statuses_by_spec(rows: Iterable[Mapping[str, Any]]) -> dict[VectorIndexSpec, VectorIndexStatus]:
    """Map each spec to the index covering its label and property, preferring our own name."""
    rows = list(rows)
    found: dict[VectorIndexSpec, VectorIndexStatus] = {}
    for spec in VECTOR_INDEX_SPECS:
        covering = [row for row in rows if _covers(row, spec)]
        covering.sort(key=lambda row: row.get("name") != spec.name)
        if covering:
            found[spec] = _status_from_row(covering[0])
    return found


class VectorIndexCatalog:
    """Cached ``SHOW VECTOR INDEXES`` result: which index, if any, a search may use right now."""

    def __init__(self, *, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._loaded_at: float | None = None
        self._statuses: dict[VectorIndexSpec, VectorIndexStatus] = {}
        self._last_decision: dict[VectorIndexSpec, str] = {}

    def needs_refresh(self) -> bool:
        return self._loaded_at is None or self._clock() - self._loaded_at >= self._ttl_s

    def update(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self._statuses = statuses_by_spec(rows)
        self._loaded_at = self._clock()

    def mark_unavailable(self) -> None:
        """Reading index state failed: scan until the next refresh succeeds."""
        self._statuses = {}
        self._loaded_at = self._clock()

    def invalidate(self) -> None:
        self._loaded_at = None

    def usable(self, spec: VectorIndexSpec, vector_length: int) -> VectorIndexStatus | None:
        status = self._statuses.get(spec)
        if status is None:
            decision, usable = "scan:missing", None
        elif status.state != "ONLINE":
            decision, usable = f"scan:{status.state.lower()}", None
        elif status.dimensions != vector_length:
            decision, usable = f"scan:dimensions {status.dimensions} != {vector_length}", None
        else:
            decision, usable = f"index:{status.name}", status
        if self._last_decision.get(spec) != decision:
            self._last_decision[spec] = decision
            logger.info("Vector search for %s.%s uses %s", spec.label, spec.prop, decision)
        return usable


def refresh_catalog_sync(catalog: VectorIndexCatalog, execute: Callable[[str], list[dict[str, Any]]]) -> None:
    if not catalog.needs_refresh():
        return
    try:
        catalog.update(execute(SHOW_VECTOR_INDEXES))
    except Exception as exc:
        logger.warning("Reading vector index state failed; similarity search will scan: %s", exc)
        catalog.mark_unavailable()


def ann_call(spec: VectorIndexSpec, var: str, vector_param: str = "query_vector") -> str:
    """The index call binding each candidate to ``var`` (and its index score to ``index_score``).

    Callers filter and rescore ``var`` in the same statement. Looking candidates up again by id
    or uuid in a second statement lets the planner pick another index for the lookup -- the
    ``group_id`` index, say, which in the default namespace matches nearly every node per hit."""
    if spec.entity_type == "RELATIONSHIP":
        return (
            f"CALL db.index.vector.queryRelationships($index_name, $k, ${vector_param}) "
            f"YIELD relationship AS {var}, score AS index_score"
        )
    return (
        f"CALL db.index.vector.queryNodes($index_name, $k, ${vector_param}) "
        f"YIELD node AS {var}, score AS index_score"
    )


def ann_stats_query(spec: VectorIndexSpec, vector_param: str = "query_vector") -> str:
    """How many candidates the index returned for ``k`` and the lowest index score among them."""
    return ann_call(spec, "hit", vector_param) + " RETURN count(hit) AS raw, min(index_score) AS lowest"


def ann_stats(row: Mapping[str, Any] | None) -> tuple[int, float | None]:
    if not row:
        return 0, None
    return int(row.get("raw") or 0), finite_or_none(row.get("lowest"))


def index_backed_rows_sync(
    *,
    execute: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    catalog: VectorIndexCatalog,
    spec: VectorIndexSpec,
    vector: list[float],
    limit: int,
    hydrate: Callable[[str, int], list[dict[str, Any]]],
    score_key: str,
    min_score: float | None = None,
) -> list[dict[str, Any]] | None:
    """Rows from the index, or None when the caller must run its scan.

    ``hydrate(index_name, k)`` runs ``ann_call`` with the caller's own filters, recomputes the
    exact score, and returns rows best first, at most ``limit``, each carrying ``score_key``.
    """
    refresh_catalog_sync(catalog, lambda query: execute(query, {}))
    status = catalog.usable(spec, len(vector))
    if status is None:
        return None
    k = initial_k(limit)
    try:
        while True:
            stats = execute(ann_stats_query(spec), {"index_name": status.name, "k": k, "query_vector": vector})
            raw, lowest = ann_stats(stats[0] if stats else None)
            rows = hydrate(status.name, k) if raw else []
            if ann_result_complete(
                raw_count=raw,
                k=k,
                lowest_index_score=lowest,
                exact_scores=[float(row[score_key]) for row in rows],
                limit=limit,
                min_score=min_score,
                margin=status.score_margin,
            ):
                return rows
            bigger = next_k(k)
            if bigger is None:
                return None
            k = bigger
    except Exception as exc:
        if not is_vector_index_query_error(exc):
            raise
        logger.warning("Vector index %s query failed; scanning instead: %s", status.name, exc)
        catalog.invalidate()
        return None


def initial_k(limit: int) -> int:
    return min(ANN_MAX_K, max(ANN_OVERSAMPLE * max(int(limit), 1), ANN_MIN_K))


def next_k(k: int) -> int | None:
    """The next, larger candidate count, or None when the cap is reached (the caller scans)."""
    return None if k >= ANN_MAX_K else min(ANN_MAX_K, 2 * k)


def ann_result_complete(
    *,
    raw_count: int,
    k: int,
    lowest_index_score: float | None,
    exact_scores: list[float],
    limit: int,
    min_score: float | None,
    margin: float,
) -> bool:
    """Whether filtered, exactly rescored ANN candidates equal the scan's top ``limit``.

    Any row the index did not return has an index score at or below the lowest returned one, so
    its exact score is at most ``bound = lowest + margin``. The result is complete when the index
    returned everything it holds, when no unseen row can pass ``min_score`` (the scan's test is
    strict ``>``), or when the ``limit``-th kept row already scores at least ``bound`` (unseen rows
    can then only tie). ``exact_scores`` is the kept rows' exact scores, best first.
    This holds up to the ANN's own recall, which oversampling keeps high.
    """
    if raw_count < k or lowest_index_score is None:
        return True
    bound = lowest_index_score + margin
    if min_score is not None and bound <= min_score:
        return True
    return len(exact_scores) >= limit and exact_scores[limit - 1] >= bound


def is_vector_index_query_error(exc: BaseException) -> bool:
    """A failed ``db.index.vector.query*`` call (index dropped, wrong dimension, still building)."""
    text = str(exc)
    return "db.index.vector.query" in text or "ProcedureCallFailed" in text


def resolve_vector_dimensions(
    configured: int | None, execute: Callable[[str], list[dict[str, Any]]]
) -> int | None:
    """The configured embedder dimension, else the length of one stored entity embedding."""
    if configured:
        return int(configured)
    rows = execute(
        "MATCH (n:Entity) WHERE n.name_embedding IS NOT NULL "
        "RETURN size(n.name_embedding) AS d LIMIT 1"
    )
    if rows and rows[0].get("d"):
        return int(rows[0]["d"])
    return None


def ensure_vector_indexes(
    execute: Callable[[str], list[dict[str, Any]]], dimensions: int | None
) -> dict[str, str]:
    """Create any missing vector index; return ``{spec name: state}``. Never raises.

    An index already covering the label and property is kept whatever its name or dimension:
    replacing one is an operator decision. Creation asks for unquantized vectors so index scores
    are exact, and retries with the server default where that option is not supported.
    """
    report: dict[str, str] = {}
    try:
        existing = statuses_by_spec(execute(SHOW_VECTOR_INDEXES))
    except Exception as exc:
        logger.warning("Vector indexes not checked; similarity search will scan: %s", exc)
        return {spec.name: f"error:{type(exc).__name__}" for spec in VECTOR_INDEX_SPECS}
    for spec in VECTOR_INDEX_SPECS:
        status = existing.get(spec)
        if status is not None:
            if dimensions is not None and status.dimensions != dimensions:
                logger.warning(
                    "Vector index %s has %s dimensions but the embedder produces %s; searches "
                    "with the new vectors will scan until the index is rebuilt",
                    status.name, status.dimensions, dimensions,
                )
            report[spec.name] = status.state.lower()
            continue
        if dimensions is None:
            report[spec.name] = "skipped:unknown_dimensions"
            continue
        report[spec.name] = _create(execute, spec, dimensions)
    logger.info("Vector indexes: %s", ", ".join(f"{k}={v}" for k, v in report.items()))
    return report


def _create(execute: Callable[[str], list[dict[str, Any]]], spec: VectorIndexSpec, dimensions: int) -> str:
    last: Exception | None = None
    for quantization in (False, None):
        try:
            execute(vector_index_ddl(spec, dimensions, quantization=quantization))
            return "created"
        except Exception as exc:
            last = exc
    logger.warning("Creating vector index %s failed; its searches will scan: %s", spec.name, last)
    return f"error:{type(last).__name__}"


def finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
