"""Shared fixtures for unit tests."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.infrastructure import LLMAdapter, PhaseOneSchemaResult, PolicyStampResult
from menhir.infrastructure import operation_owner as _operation_owner
from menhir.infrastructure.memory_graph_adapter import is_context_window_error_text


_LOCAL_PYTEST_TEMP = Path.home() / ".codex" / "memories" / "pytest_tmp"
_LOCAL_PYTEST_TEMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(_LOCAL_PYTEST_TEMP))
os.environ.setdefault("TEMP", str(_LOCAL_PYTEST_TEMP))
os.environ.setdefault("TMP", str(_LOCAL_PYTEST_TEMP))
tempfile.tempdir = str(_LOCAL_PYTEST_TEMP)


def pytest_configure(config: pytest.Config) -> None:
    """Redirect NEO4J_* onto the test instance BEFORE any test module is imported.

    This used to live in the ``force_all_tests_onto_test_neo4j`` session fixture, which was too
    late: a session fixture runs AFTER collection, and collection imports every test module. A
    module that resolves the URI at import time -- ``load_dotenv(); NEO4J_URI = os.getenv(...)``
    at module scope, as test_phase_one_bootstrap_live.py does -- had therefore already captured
    the PRODUCTION uri before the fixture could override it, bypassing that guard, the
    ``test_neo4j_repo`` guard, and ``MENHIR_TEST_NEO4J_URI`` all at once. It then ran schema
    backfill (`MATCH (n:Entity) ... SET ...`) against the operator's real graph.

    ``pytest_configure`` runs before collection, so the override is in place for import-time
    reads. A module calling ``load_dotenv()`` cannot undo it either: python-dotenv defaults to
    ``override=False``, so an already-set environment variable wins over the .env file.
    """
    import os

    from menhir.config import MemorySettings

    # The real prod URI. Normally read from the environment before this function overrides it --
    # but under pytest-xdist this runs again in every WORKER, which inherits the parent's already
    # overridden NEO4J_URI. Reading it fresh there yields the TEST uri, the guard below compares it
    # against itself, and every worker aborts with "the test URI is the PRODUCTION graph". That
    # false positive is why xdist could not be used on this suite at all.
    #
    # The snapshot the parent exports is the authority whenever it exists, and using it makes the
    # guard STRONGER, not weaker: it is the genuine pre-override prod URI rather than whatever
    # NEO4J_URI happens to say by the time a worker starts.
    real_prod_uri = (
        os.environ.get("MENHIR_PROD_NEO4J_URI_SNAPSHOT")
        or MemorySettings.from_env().neo4j_uri
    )
    test_uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688")
    test_user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    test_password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    test_database = os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")

    if _normalize_uri(test_uri) == _normalize_uri(real_prod_uri):
        raise pytest.UsageError(
            f"MENHIR_TEST_NEO4J_URI ({test_uri}) is the PRODUCTION graph ({real_prod_uri}). "
            "Refusing to collect any test -- online tests run destructive, unscoped queries "
            "and would destroy real memories. Point MENHIR_TEST_NEO4J_URI at the test "
            "instance (:7688) instead."
        )

    os.environ["MENHIR_PROD_NEO4J_URI_SNAPSHOT"] = real_prod_uri
    os.environ["NEO4J_URI"] = test_uri
    os.environ["NEO4J_USER"] = test_user
    os.environ["NEO4J_PASSWORD"] = test_password
    os.environ["NEO4J_DATABASE"] = test_database


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-online",
        action="store_true",
        default=False,
        help="Run tests marked with 'online' that hit live services.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-online"):
        return

    skip_online = pytest.mark.skip(
        reason="online tests are disabled by default; pass --run-online to execute them"
    )
    for item in items:
        if "online" in item.keywords:
            item.add_marker(skip_online)


@pytest.fixture(autouse=True)
def _isolate_client_restrictions(monkeypatch):
    """No test may inherit the developer's per-client policy from `.env` (CF-32 / CF-83).

    Both of those refusals key on server config -- an unrecognized client name is refused, and an
    undeclared name cannot be minted -- and `MemorySettings.from_env()` reads the real `.env`
    through dotenv. So on a machine that configures MENHIR_CLIENT_NAMESPACES, tests that merely
    bind `client_name="web"` or mint `"alpha"` began failing with an identity error, in three
    files that have nothing to do with identity. The failure looked unrelated to what they test,
    which is the expensive kind.

    Cleared here rather than per-file because it is one class of failure, not three incidents. A
    test that WANTS a restricted deployment sets these itself; `monkeypatch.setenv` inside the
    test overrides this fixture, since both write the same environment.
    """
    for var in ("MENHIR_CLIENT_NAMESPACES", "MENHIR_CLIENT_TOOLS", "MENHIR_KNOWN_CLIENTS"):
        monkeypatch.setenv(var, "")


@pytest.fixture(autouse=True, scope="session")
def force_all_tests_onto_test_neo4j():
    """SAFETY (2026-07-13): no test may ever connect to the PRODUCTION graph.

    Online tests build services via ``MemorySettings.from_env()``, which reads ``NEO4J_URI``.
    With ``.env`` sourced (python-dotenv is a dependency) or ``NEO4J_URI`` exported, that
    resolves to the operator's real graph -- so ``pytest --run-online`` would run destructive,
    unscoped decay/consolidation/ingest against the operator's live memories. That is exactly
    the 2026-07-12 incident, and the ``test_neo4j_repo`` guard only covered tests that opt into
    that fixture, not the many that call ``from_env()`` directly.

    This session-autouse fixture forces EVERY ``from_env()`` resolution onto the dedicated test
    instance (:7688) for the whole run, so a test cannot reach prod even if it bypasses
    ``test_neo4j_repo``. It refuses to run at all if the configured test URI is itself prod.
    Prod is only ever touched by the running server, never by pytest.

    The override itself now happens in ``pytest_configure`` (before collection), because a
    session fixture runs too late for modules that resolve the URI at IMPORT time. This fixture
    is kept for its teardown, which restores the pre-run environment. It MUST read the real prod
    URI from the snapshot rather than calling ``from_env()``: by the time it runs, ``NEO4J_URI``
    is already the test URI, so ``from_env()`` would return the test URI as "prod" and the
    equality guard below would abort the entire suite.
    """
    import os

    real_prod_uri = os.environ.get("MENHIR_PROD_NEO4J_URI_SNAPSHOT", "")
    test_uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688")
    test_user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    test_password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")

    if _normalize_uri(test_uri) == _normalize_uri(real_prod_uri):
        pytest.exit(
            f"MENHIR_TEST_NEO4J_URI ({test_uri}) is the PRODUCTION graph ({real_prod_uri}). "
            "Refusing to run any test -- online tests run destructive, unscoped queries and "
            "would destroy real memories. Point MENHIR_TEST_NEO4J_URI at the test instance "
            "(:7688) instead.",
            returncode=3,
        )

    saved = {
        k: os.environ.get(k)
        for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "MENHIR_PROD_NEO4J_URI_SNAPSHOT")
    }
    os.environ["MENHIR_PROD_NEO4J_URI_SNAPSHOT"] = real_prod_uri
    os.environ["NEO4J_URI"] = test_uri
    os.environ["NEO4J_USER"] = test_user
    os.environ["NEO4J_PASSWORD"] = test_password
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def isolated_telemetry_db(tmp_path, monkeypatch):
    """Point the telemetry sidecar at a temp DB for EVERY test.

    ``telemetry_store`` is a module-level singleton resolving to the operator's real
    SQLite database. Any test that reaches code calling record_merge / record_lifecycle_action
    / record_memory_revision writes into it -- and merge_entity's merge_audit row is
    DESIGNED to outlive graph cleanup, so it does not even get cleaned up with the fixture
    nodes. Offline unit tests with stubbed graphs were polluting production telemetry too,
    not just the online ones.

    autouse: no test should ever be able to write to the operator's telemetry database.
    """
    from menhir.infrastructure.telemetry import telemetry_store

    db = tmp_path / "test_telemetry.db"
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(db))
    original = telemetry_store.db_path
    telemetry_store.db_path = db
    telemetry_store._initialized = False  # force schema re-init against the temp DB
    try:
        yield db
    finally:
        telemetry_store.db_path = original
        telemetry_store._initialized = False


@pytest.fixture
def test_neo4j_repo(isolated_telemetry_db):
    """A Neo4j repository pointed at a STOOD-UP TEST INSTANCE -- never the real graph.

    Online tests execute unscoped, destructive queries against whatever database they are
    handed. `apply_decay()` rewrites sharpness and edge counts across all nodes and can
    compress across the entire database. Run against the operator's live instance, they
    destroy real memories -- which is exactly what happened on 2026-07-12.

    So this fixture does NOT fall back to MemorySettings.from_env(). It requires an
    explicit test instance -- a SEPARATE Neo4j on port 7688, which docker-compose.test.yml
    publishes on localhost -- refuses to run if that URI is the same as the configured
    production URI, and skips when no test instance is reachable. A missing test database
    must SKIP, never silently retarget prod.
    """
    import os

    from menhir.config import MemorySettings
    from menhir.infrastructure.neo4j import Neo4jRepository

    # Default: the throwaway instance published on localhost:7688 by
    # docker-compose.test.yml, a separate Neo4j from any real graph. Tests run destructive, unscoped
    # queries, so they MUST hit their own instance, never prod's database.
    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688")
    user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    database = os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")

    # Compare against the REAL prod URI. force_all_tests_onto_test_neo4j overrides NEO4J_URI
    # to the test instance for the whole session, so MemorySettings.from_env() now returns the
    # test URI -- it snapshots the pre-override prod URI here so this guard stays meaningful.
    prod_uri = os.getenv("MENHIR_PROD_NEO4J_URI_SNAPSHOT") or MemorySettings.from_env().neo4j_uri
    if _normalize_uri(uri) == _normalize_uri(prod_uri):
        pytest.fail(
            f"MENHIR_TEST_NEO4J_URI ({uri}) is the PRODUCTION graph ({prod_uri}). "
            "Online tests run destructive, unscoped queries and would destroy real "
            "memories. Stand up the test instance instead:\n"
            "    bash scripts/setup_remote_test_neo4j.sh up"
        )

    repo = Neo4jRepository(uri=uri, database=database, user=user, password=password)
    try:
        repo.execute("RETURN 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        repo.close()
        pytest.skip(
            f"no test Neo4j at {uri} ({type(exc).__name__}). Start it with:\n"
            "    docker compose -f docker-compose.test.yml up -d"
        )

    yield repo

    # Scratch instance: leave it empty for the next test.
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
    finally:
        repo.close()


def _normalize_uri(uri: str) -> str:
    """Compare bolt URIs ignoring scheme/host spelling differences."""
    value = (uri or "").strip().lower()
    for scheme in ("bolt+s://", "bolt+ssc://", "bolt://", "neo4j+s://", "neo4j+ssc://", "neo4j://"):
        if value.startswith(scheme):
            value = value[len(scheme):]
            break
    return value.replace("127.0.0.1", "localhost").rstrip("/")


@pytest.fixture
def tmp_path() -> Path:
    """Provide a workspace-local tmp_path to avoid Windows tempdir permission issues."""

    path = Path(tempfile.mkdtemp(prefix="pytest-case-", dir=str(_LOCAL_PYTEST_TEMP)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _default_schema_result() -> PhaseOneSchemaResult:
    """Factory for a default successful schema bootstrap result."""
    return PhaseOneSchemaResult(success=True, queries_executed=1, failures=[])


@dataclass
class StubMemoryGraphAdapter:
    """In-memory graph adapter used by orchestration/service unit tests."""

    calls: int = 0
    result: PhaseOneSchemaResult = field(default_factory=_default_schema_result)
    stamp_calls: list[dict[str, object]] = field(default_factory=list)
    stamp_result: PolicyStampResult = field(
        default_factory=lambda: PolicyStampResult(nodes_touched=2, edges_touched=2)
    )
    flagged_nodes: list[str] = field(default_factory=list)
    deleted_nodes: list[str] = field(default_factory=list)
    completed_episode_artifacts: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    weighted_edges: list[str] = field(default_factory=list)
    candidate_metadata: list[dict[str, object]] = field(default_factory=list)
    candidate_provenance_rows: list[dict[str, object]] = field(default_factory=list)
    adjacency_pairs: list[dict[str, object]] = field(default_factory=list)
    adjacency_calls: list[dict[str, object]] = field(default_factory=list)
    temporal_fact_rows: list[dict[str, object]] = field(default_factory=list)
    touch_count: int = 0
    pending_episode_rows: dict[str, dict[str, object]] = field(default_factory=dict)
    edge_counts_synced: int = 0
    sync_edge_counts_calls: int = 0
    decay_candidates_by_freshness: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    compressed_nodes: list[dict[str, str]] = field(default_factory=list)
    bridge_delete_results: dict[str, dict[str, int]] = field(default_factory=dict)
    deleted_decay_nodes: list[str] = field(default_factory=list)
    node_freshness: dict[str, str] = field(default_factory=dict)
    rehydrated_nodes: list[dict[str, object]] = field(default_factory=list)
    raw_capture_calls: list[dict[str, object]] = field(default_factory=list)
    content_embedding_results: list[dict[str, object]] = field(default_factory=list)
    content_embedding_calls: list[dict[str, object]] = field(default_factory=list)

    def bootstrap_phase_one(self) -> PhaseOneSchemaResult:
        self.calls += 1
        return self.result

    def sync_edge_counts(self) -> int:
        self.sync_edge_counts_calls += 1
        return self.edge_counts_synced

    def stamp_ingest_metadata(
        self,
        *,
        node_uuids: list[str],
        edge_uuids: list[str],
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        namespace: str = "default",
        belief_commit: str | None = None,
        belief_branch: str | None = None,
    ) -> PolicyStampResult:
        self.stamp_calls.append(
            {
                "node_uuids": node_uuids,
                "edge_uuids": edge_uuids,
                "session_id": session_id,
                "user_id": user_id,
                "source": source,
                "source_confidence": source_confidence,
                "namespace": namespace,
                "belief_commit": belief_commit,
                "belief_branch": belief_branch,
            }
        )
        return self.stamp_result

    def scalar_view_has_user_foundation(self, *, view_uuid: str, namespace=None) -> bool:
        # G14 slice 3: default = no declarant-user foundation reachable (the governance-safe advisory
        # default). Tests that exercise the leads path override this attribute with a lambda.
        return False

    def assertions_have_user_foundation(self, *, assertion_ids, namespace=None) -> bool:
        # G13 expiry-verdict basis: default = no user foundation (advisory). Overridden in tests.
        return False

    def fetch_scalar_authority_contributors(
        self, *, view_uuid, limit=8, offset=0, namespace=None
    ):
        rows = [{
            "assertion_id": "assert-current", "relation": "CURRENT_ANCHOR",
            "operation": "absolute", "value": 37, "stated_span": "I own 37 rare coins",
            "valid_at": "2026-07-02T00:00:00+00:00", "evidence_tier": "user",
            "episode_uuid": "ep-current",
        }]
        return {"contributors": rows[offset:offset + limit], "total": len(rows),
                "next_offset": None}

    def fetch_assertion_contributors(self, *, assertion_ids, relations=None, limit=8):
        rows = [{
            "assertion_id": assertion_id,
            "relation": (relations or {}).get(assertion_id, "CONTRIBUTOR"),
            "operation": "absolute", "value": 37, "stated_span": "I owned 37 rare coins",
            "valid_at": "2026-07-02T00:00:00+00:00", "evidence_tier": "user",
            "episode_uuid": "ep-current",
        } for assertion_id in assertion_ids[:limit]]
        return {"contributors": rows, "total": len(assertion_ids),
                "next_offset": len(rows) if len(rows) < len(assertion_ids) else None}

    def flag_memory(self, node_uuid: str) -> bool:
        self.flagged_nodes.append(node_uuid)
        return True

    def unflag_memory(self, node_uuid: str) -> bool:
        try:
            self.flagged_nodes.remove(node_uuid)
        except ValueError:
            pass
        return True

    def delete_memory(self, node_uuid: str) -> bool:
        row = self.pending_episode_rows.get(node_uuid)
        if row is not None and row.get("processing_state") in {ProcessingState.PENDING, ProcessingState.ENRICHING}:
            row["processing_state"] = ProcessingState.FAILED
            row["processing_stage"] = "failed"
            row["processing_substage"] = "deleted_by_user"
            row["processing_error"] = "deleted_by_user"
            row["processing_owner"] = None
            row["processing_lease_expires_at"] = None
        self.deleted_nodes.append(node_uuid)
        return True

    def increment_edge_weight(self, edge_uuid: str) -> bool:
        self.weighted_edges.append(edge_uuid)
        return True

    def update_edge_facts(self, updates: list[dict[str, str]]) -> int:
        self.edge_fact_updates = updates
        return len(updates)

    def write_project_structure(self, scan: object, session_id: str, user_id: str) -> dict[str, int]:
        return {"entities": 0, "edges": 0}

    def get_scan_fingerprint(self, project_name: str) -> str | None:
        return None

    def query_structure(self, project: str, query_type: str, **kwargs: object) -> object:
        return {}

    def list_structure_projects(self) -> list[dict[str, str]]:
        return []

    def fetch_candidate_metadata(self, node_uuids: list[str]) -> list[dict[str, object]]:
        return self.candidate_metadata

    def search_content_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        self.content_embedding_calls.append(
            {"query_vector": query_vector, "limit": limit, "group_ids": group_ids}
        )
        return self.content_embedding_results

    def fetch_temporal_facts(self, node_uuids: list[str]) -> list[dict[str, object]]:
        return self.temporal_fact_rows

    def fetch_candidate_provenance(self, node_uuids: list[str]) -> list[dict[str, object]]:
        return self.candidate_provenance_rows

    def fetch_decay_candidates(
        self,
        freshness: str,
        *,
        min_days_since_accessed: float,
        max_edge_count: int,
        max_sharpness: float | None = None,
    ) -> list[dict[str, object]]:
        return [dict(row) for row in self.decay_candidates_by_freshness.get(freshness, [])]

    def compress_node(self, node_uuid: str, compressed_summary: str) -> bool:
        self.compressed_nodes.append(
            {"uuid": node_uuid, "summary": compressed_summary}
        )
        self.node_freshness[node_uuid] = FreshnessState.COMPRESSED
        return True

    def fetch_node_freshness(self, node_uuids: list[str]) -> dict[str, str]:
        freshness_map: dict[str, str] = {}
        metadata_by_uuid = {
            str(row.get("uuid")): str(row.get("freshness") or FreshnessState.ACTIVE)
            for row in self.candidate_metadata
            if row.get("uuid")
        }
        for node_uuid in node_uuids:
            if node_uuid in self.node_freshness:
                freshness_map[node_uuid] = self.node_freshness[node_uuid]
            elif node_uuid in metadata_by_uuid:
                freshness_map[node_uuid] = metadata_by_uuid[node_uuid]
        return freshness_map

    def complete_rehydration(self, node_uuid: str, updated_content: str | None = None) -> bool:
        self.rehydrated_nodes.append({"uuid": node_uuid, "content": updated_content})
        self.node_freshness[node_uuid] = FreshnessState.ACTIVE
        for row in self.candidate_metadata:
            if str(row.get("uuid")) == node_uuid:
                row["freshness"] = FreshnessState.ACTIVE
                row["rehydration_count"] = int(row.get("rehydration_count", 0)) + 1
                if updated_content is not None:
                    row["content"] = updated_content
        return True

    def bridge_and_delete(self, node_uuid: str) -> dict[str, int]:
        self.deleted_decay_nodes.append(node_uuid)
        return dict(self.bridge_delete_results.get(node_uuid, {"edges_bridged": 0, "deleted": 1}))

    def create_pending_episode(
        self,
        *,
        episode_uuid: str,
        name: str,
        content: str,
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        diff: str | None = None,
        user_flagged: bool = False,
        namespace: str = "default",
        reference_time: "datetime | None" = None,
    ) -> str:
        self.pending_episode_rows[episode_uuid] = {
            "uuid": episode_uuid,
            "name": name,
            "content": content,
            "diff": diff,
            "scope": NodeScope.SESSION,
            "type": "EPISODIC",
            "source": source,
            "source_confidence": source_confidence,
            "session_id": session_id,
            "user_id": user_id,
            "namespace": namespace,
            "reference_time": reference_time,
            "processing_state": ProcessingState.PENDING,
            "processing_stage": "queued",
            "processing_substage": "queued",
            "processing_substage_started_at": "2026-03-09T00:00:00+00:00",
            "processing_progress": 0.0,
            "processing_steps_total": 5,
            "processing_steps_completed": 0,
            "processing_llm_tasks_attempt": 0,
            "processing_llm_tasks_total": 0,
            "processing_llm_last_task_at": None,
            "processing_llm_active_task": None,
            "processing_llm_active_kind": None,
            "processing_llm_active_model": None,
            "processing_llm_active_endpoint": None,
            "processing_attempts": 0,
            "processing_owner": None,
            "processing_lease_expires_at": None,
            "processing_heartbeat_at": "2026-03-09T00:00:00+00:00",
            "resolved_episode_uuid": None,
        }
        return episode_uuid

    def create_raw_capture_entity(self, **kwargs: object) -> str | None:
        """Record a degraded raw-capture entity for a terminally-broken episode (Part 2)."""
        self.raw_capture_calls.append(dict(kwargs))
        return f"raw-capture-{kwargs.get('episode_uuid', '')}"

    def mark_raw_capture_superseded(self, episode_uuid: str) -> bool:
        return True

    def list_pending_episode_uuids(self, *, max_attempts: int, limit: int = 100) -> list[str]:
        uuids = [
            uuid
            for uuid, row in self.pending_episode_rows.items()
            if row.get("processing_state") == ProcessingState.PENDING
            and int(row.get("processing_attempts") or 0) < max(1, int(max_attempts))
        ]
        return uuids[:limit]

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        rows = [
            {
                "uuid": uuid,
                "processing_attempts": row.get("processing_attempts"),
                "processing_error": row.get("processing_error"),
                "processing_completed_at": row.get("processing_completed_at"),
            }
            for uuid, row in self.pending_episode_rows.items()
            if row.get("processing_state") == ProcessingState.FAILED
        ]
        return rows[:limit]

    def find_completed_episode_artifact(
        self,
        *,
        anchor_uuid: str,
        anchor_name: str,
    ) -> dict[str, object] | None:
        artifact = self.completed_episode_artifacts.get((anchor_uuid, anchor_name))
        if artifact is None:
            return None
        return dict(artifact)

    def claim_pending_episode(
        self,
        episode_uuid: str,
        *,
        max_attempts: int,
        context_retry_attempts: int | None = None,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, object] | None:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return None
        attempts = int(row.get("processing_attempts") or 0)
        if row.get("processing_state") not in {ProcessingState.PENDING, ProcessingState.FAILED}:
            return None
        context_cap = max(max_attempts, int(context_retry_attempts or max_attempts))
        error_text = str(row.get("processing_error") or "").lower()
        context_window_mismatch = is_context_window_error_text(error_text)
        if attempts >= max_attempts and not (
            row.get("processing_state") == ProcessingState.FAILED
            and context_window_mismatch
            and attempts < context_cap
        ):
            return None
        row["processing_state"] = ProcessingState.ENRICHING
        row["processing_stage"] = "claiming"
        row["processing_substage"] = "lease_acquired"
        row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_progress"] = 5.0
        row["processing_steps_total"] = 5
        row["processing_steps_completed"] = 0
        row["processing_llm_tasks_attempt"] = 0
        row["processing_llm_last_task_at"] = None
        row["processing_llm_active_task"] = None
        row["processing_llm_active_kind"] = None
        row["processing_llm_active_model"] = None
        row["processing_llm_active_endpoint"] = None
        row["processing_attempts"] = attempts + 1
        row["processing_owner"] = worker_id
        row["processing_lease_expires_at"] = lease_seconds
        row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        return dict(row)

    def mark_episode_ready(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        required_state: str | None = None,
        resolved_episode_uuid: str,
        nodes_touched: int,
        edges_touched: int,
    ) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return False
        if required_state is not None and row.get("processing_state") != required_state:
            return False
        if worker_id is not None and (
            row.get("processing_state") != ProcessingState.ENRICHING
            or row.get("processing_owner") != worker_id
        ):
            return False
        row["processing_state"] = ProcessingState.READY
        row["processing_stage"] = "ready"
        row["processing_substage"] = "complete"
        row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_progress"] = 100.0
        row["processing_steps_completed"] = int(row.get("processing_steps_total") or 5)
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_llm_active_task"] = None
        row["processing_llm_active_kind"] = None
        row["processing_llm_active_model"] = None
        row["processing_llm_active_endpoint"] = None
        row["resolved_episode_uuid"] = resolved_episode_uuid
        row["enriched_nodes_touched"] = nodes_touched
        row["enriched_edges_touched"] = edges_touched
        return True

    def mark_episode_failed(self, episode_uuid: str, error: str, *, worker_id: str | None = None) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return False
        if worker_id is not None and (
            row.get("processing_state") != ProcessingState.ENRICHING
            or row.get("processing_owner") != worker_id
        ):
            return False
        row["processing_state"] = ProcessingState.FAILED
        row["processing_stage"] = "failed"
        row["processing_substage"] = "failed"
        row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_progress"] = 100.0
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_llm_active_task"] = None
        row["processing_llm_active_kind"] = None
        row["processing_llm_active_model"] = None
        row["processing_llm_active_endpoint"] = None
        row["processing_error"] = error
        row["processing_completed_at"] = "2026-03-09T00:00:00+00:00"
        return True

    def reset_stale_enriching_episodes(self, *, max_attempts: int) -> int:
        reset = 0
        for row in self.pending_episode_rows.values():
            if row.get("processing_state") != ProcessingState.ENRICHING:
                continue
            lease_expires_at = row.get("processing_lease_expires_at")
            attempts = int(row.get("processing_attempts") or 0)
            exhausted = attempts >= max(1, int(max_attempts))
            row["processing_state"] = ProcessingState.FAILED if exhausted else ProcessingState.PENDING
            row["processing_stage"] = "failed" if exhausted else "queued"
            row["processing_substage"] = (
                "lease_missing_attempts_exhausted"
                if exhausted and lease_expires_at is None
                else "lease_expired_attempts_exhausted"
                if exhausted
                else "lease_missing_reset"
                if lease_expires_at is None
                else "lease_expired_reset"
            )
            row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_progress"] = 100.0 if exhausted else 0.0
            row["processing_steps_completed"] = int(row.get("processing_steps_total") or 5) if exhausted else 0
            row["processing_llm_tasks_attempt"] = 0
            row["processing_owner"] = None
            row["processing_lease_expires_at"] = None
            row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_llm_active_task"] = None
            row["processing_llm_active_kind"] = None
            row["processing_llm_active_model"] = None
            row["processing_llm_active_endpoint"] = None
            row["processing_error"] = (
                "lease_missing_reset_attempts_exhausted"
                if exhausted and lease_expires_at is None
                else "lease_expired_reset_attempts_exhausted"
                if exhausted
                else "lease_missing_reset"
                if lease_expires_at is None
                else "lease_expired_reset"
            )
            row["processing_completed_at"] = "2026-03-09T00:00:00+00:00" if exhausted else None
            reset += 1
        return reset

    def reset_orphaned_enriching_episodes(self, *, max_attempts: int) -> int:
        reset = 0
        for row in self.pending_episode_rows.values():
            if row.get("processing_state") != ProcessingState.ENRICHING:
                continue
            owner = row.get("processing_owner")
            if owner not in {None, ""}:
                continue
            attempts = int(row.get("processing_attempts") or 0)
            exhausted = attempts >= max(1, int(max_attempts))
            row["processing_state"] = ProcessingState.FAILED if exhausted else ProcessingState.PENDING
            row["processing_stage"] = "failed" if exhausted else "queued"
            row["processing_substage"] = (
                "orphaned_reset_attempts_exhausted" if exhausted else "orphaned_reset"
            )
            row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_progress"] = 100.0 if exhausted else 0.0
            row["processing_steps_completed"] = int(row.get("processing_steps_total") or 5) if exhausted else 0
            row["processing_llm_tasks_attempt"] = 0
            row["processing_owner"] = None
            row["processing_lease_expires_at"] = None
            row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_started_at"] = None
            row["processing_completed_at"] = "2026-03-09T00:00:00+00:00" if exhausted else None
            row["processing_llm_active_task"] = None
            row["processing_llm_active_kind"] = None
            row["processing_llm_active_model"] = None
            row["processing_llm_active_endpoint"] = None
            row["processing_error"] = (
                "orphaned_enriching_reset_attempts_exhausted"
                if exhausted
                else "orphaned_enriching_reset"
            )
            reset += 1
        return reset

    def fail_exhausted_pending_episodes(self, *, max_attempts: int) -> int:
        failed = 0
        for row in self.pending_episode_rows.values():
            if row.get("processing_state") != ProcessingState.PENDING:
                continue
            attempts = int(row.get("processing_attempts") or 0)
            if attempts < max(1, int(max_attempts)):
                continue
            row["processing_state"] = ProcessingState.FAILED
            row["processing_stage"] = "failed"
            row["processing_substage"] = "pending_attempts_exhausted"
            row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_progress"] = 100.0
            row["processing_steps_completed"] = int(
                row.get("processing_steps_total")
                or row.get("processing_steps_completed")
                or 5
            )
            row["processing_llm_tasks_attempt"] = 0
            row["processing_owner"] = None
            row["processing_lease_expires_at"] = None
            row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_started_at"] = None
            row["processing_completed_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_llm_active_task"] = None
            row["processing_llm_active_kind"] = None
            row["processing_llm_active_model"] = None
            row["processing_llm_active_endpoint"] = None
            row["processing_error"] = row.get("processing_error") or "pending_attempts_exhausted"
            failed += 1
        return failed

    def release_worker_episode_leases(self, worker_id: str, *, max_attempts: int) -> int:
        released = 0
        for row in self.pending_episode_rows.values():
            if row.get("processing_state") != ProcessingState.ENRICHING:
                continue
            if row.get("processing_owner") != worker_id:
                continue
            attempts = int(row.get("processing_attempts") or 0)
            exhausted = attempts >= max(1, int(max_attempts))
            row["processing_state"] = ProcessingState.FAILED if exhausted else ProcessingState.PENDING
            row["processing_stage"] = "failed" if exhausted else "queued"
            row["processing_substage"] = (
                "worker_shutdown_release_attempts_exhausted"
                if exhausted
                else "worker_shutdown_release"
            )
            row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_progress"] = 100.0 if exhausted else 0.0
            row["processing_steps_completed"] = int(row.get("processing_steps_total") or 5) if exhausted else 0
            row["processing_llm_tasks_attempt"] = 0
            row["processing_owner"] = None
            row["processing_lease_expires_at"] = None
            row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
            row["processing_llm_active_task"] = None
            row["processing_llm_active_kind"] = None
            row["processing_llm_active_model"] = None
            row["processing_llm_active_endpoint"] = None
            row["processing_completed_at"] = "2026-03-09T00:00:00+00:00" if exhausted else None
            row["processing_error"] = (
                "worker_shutdown_release_attempts_exhausted"
                if exhausted
                else "worker_shutdown_release"
            )
            released += 1
        return released

    def mark_episode_pending(
        self,
        episode_uuid: str,
        *,
        retry_after_s: float = 0.0,
        worker_id: str | None = None,
    ) -> bool:
        """Release back to PENDING without incrementing attempts (circuit breaker requeue)."""
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return False
        if worker_id is not None and (
            row.get("processing_state") != ProcessingState.ENRICHING
            or row.get("processing_owner") != worker_id
        ):
            return False
        row["processing_state"] = ProcessingState.PENDING
        row["processing_stage"] = "queued"
        row["processing_substage"] = "circuit_breaker_requeue"
        row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_error"] = None
        row["processing_llm_active_task"] = None
        row["processing_llm_active_kind"] = None
        row["processing_llm_active_model"] = None
        row["processing_llm_active_endpoint"] = None
        if retry_after_s > 0:
            row["retry_after_s"] = retry_after_s
        else:
            row.pop("retry_after_s", None)
        return True

    def force_release_episode_lease(self, episode_uuid: str, *, max_attempts: int) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None or row.get("processing_state") != ProcessingState.ENRICHING:
            return False
        attempts = int(row.get("processing_attempts") or 0)
        exhausted = attempts >= max(1, int(max_attempts))
        row["processing_state"] = ProcessingState.FAILED if exhausted else ProcessingState.PENDING
        row["processing_stage"] = "failed" if exhausted else "queued"
        row["processing_substage"] = (
            "lease_released_attempts_exhausted" if exhausted else "lease_released"
        )
        row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_progress"] = 100.0 if exhausted else 0.0
        row["processing_steps_completed"] = (
            int(row.get("processing_steps_total") or 5) if exhausted else 0
        )
        row["processing_llm_tasks_attempt"] = 0
        row["processing_llm_active_task"] = None
        row["processing_llm_active_kind"] = None
        row["processing_llm_active_model"] = None
        row["processing_llm_active_endpoint"] = None
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_started_at"] = None
        row["processing_completed_at"] = "2026-03-09T00:00:00+00:00" if exhausted else None
        row["processing_error"] = (
            "manual_lease_release_attempts_exhausted" if exhausted else "manual_lease_release"
        )
        return True

    def list_episode_processing(
        self,
        *,
        processing_states: list[str] | None = None,
        limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        states = {str(state).strip().upper() for state in (processing_states or []) if str(state).strip()}
        rows = []
        for row in self.pending_episode_rows.values():
            current_state = str(row.get("processing_state") or "").upper()
            if states and current_state not in states:
                continue
            rows.append(dict(row))
        rows.sort(key=lambda item: str(item.get("uuid") or ""))
        return rows[:limit]

    def fetch_stale_enriching_episodes(
        self,
        *,
        include_missing_lease: bool = True,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        rows = []
        for row in self.pending_episode_rows.values():
            if row.get("processing_state") != ProcessingState.ENRICHING:
                continue
            lease = row.get("processing_lease_expires_at")
            if lease is None and not include_missing_lease:
                continue
            rows.append(dict(row))
        rows.sort(key=lambda item: str(item.get("uuid") or ""))
        return rows[:limit]

    def fetch_episode_processing(self, episode_uuid: str) -> dict[str, object] | None:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return None
        return dict(row)

    def update_episode_processing(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        stage: str | None = None,
        substage: str | None = None,
        progress: float | None = None,
        steps_completed: int | None = None,
        steps_total: int | None = None,
        llm_active_task: str | None = None,
        llm_active_kind: str | None = None,
        llm_active_model: str | None = None,
        llm_active_endpoint: str | None = None,
        clear_llm_active: bool = False,
        heartbeat: bool = True,
    ) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return False
        if worker_id is not None and (
            row.get("processing_state") != ProcessingState.ENRICHING
            or row.get("processing_owner") != worker_id
        ):
            return False
        if stage is not None:
            row["processing_stage"] = stage
        if substage is not None:
            row["processing_substage"] = substage
            row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        if progress is not None:
            row["processing_progress"] = max(0.0, min(100.0, float(progress)))
        if steps_total is not None:
            row["processing_steps_total"] = max(1, int(steps_total))
        if steps_completed is not None:
            cap = int(row.get("processing_steps_total") or 5)
            row["processing_steps_completed"] = max(0, min(cap, int(steps_completed)))
        if clear_llm_active:
            row["processing_llm_active_task"] = None
            row["processing_llm_active_kind"] = None
            row["processing_llm_active_model"] = None
            row["processing_llm_active_endpoint"] = None
        else:
            if llm_active_task is not None:
                row["processing_llm_active_task"] = llm_active_task
            if llm_active_kind is not None:
                row["processing_llm_active_kind"] = llm_active_kind
            if llm_active_model is not None:
                row["processing_llm_active_model"] = llm_active_model
            if llm_active_endpoint is not None:
                row["processing_llm_active_endpoint"] = llm_active_endpoint
        if heartbeat:
            row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        return True

    def increment_episode_llm_usage(
        self,
        episode_uuid: str,
        *,
        task_delta: int = 1,
        phase: str = "started",
        kind: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        task: str | None = None,
        error: str | None = None,
    ) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return False
        delta = max(0, int(task_delta))
        phase_normalized = (phase or "started").strip().lower()
        if delta == 0 and phase_normalized == "started":
            return False
        if phase_normalized == "started":
            row["processing_llm_tasks_attempt"] = int(row.get("processing_llm_tasks_attempt") or 0) + delta
        row["processing_llm_tasks_total"] = int(row.get("processing_llm_tasks_total") or 0) + delta
        row["processing_llm_last_task_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_substage"] = {
            "started": "llm_request_started",
            "completed": "llm_response_received",
            "failed": "llm_request_failed",
        }.get(phase_normalized, "llm_activity_observed")
        row["processing_substage_started_at"] = "2026-03-09T00:00:00+00:00"
        row["processing_llm_active_task"] = task
        row["processing_llm_active_kind"] = kind
        row["processing_llm_active_model"] = model
        row["processing_llm_active_endpoint"] = endpoint
        if phase_normalized == "failed" and error is not None:
            row["processing_error"] = error
        return True

    def touch_episode_processing_heartbeat(self, episode_uuid: str, *, worker_id: str | None = None) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None:
            return False
        if worker_id is not None and (
            row.get("processing_state") != ProcessingState.ENRICHING
            or row.get("processing_owner") != worker_id
        ):
            return False
        row["processing_heartbeat_at"] = "2026-03-09T00:00:00+00:00"
        return True

    def fetch_relevant_pending_episodes(
        self, query: str, limit: int = 3, *, namespace: str | None = None
    ) -> list[dict[str, object]]:
        # `namespace` mirrors the real adapter signature (tenant scoping, CF-104). This stub
        # serves canned rows for recall-wiring tests and does not model the filter; the
        # predicate itself is covered by tests/test_pending_episode_namespace_isolation.py.
        token = (query or "").strip().lower()
        rows = []
        for row in self.pending_episode_rows.values():
            if row.get("processing_state") not in {ProcessingState.PENDING, ProcessingState.ENRICHING}:
                continue
            content = str(row.get("content") or "").lower()
            name = str(row.get("name") or "").lower()
            if token and token not in content and token not in name:
                continue
            rows.append(dict(row))
        return rows[:limit]

    def fetch_linked_entity_uuids_for_episode(self, episode_uuid: str) -> list[str]:
        row = self.pending_episode_rows.get(episode_uuid, {})
        linked = row.get("linked_entity_uuids", [])
        return list(linked)

    # --- Consolidation stubs ---
    session_entities: list[dict[str, object]] = field(default_factory=list)
    persistent_edge_counts: dict[str, int] = field(default_factory=dict)
    promoted_nodes: list[str] = field(default_factory=list)
    deleted_session_nodes: list[str] = field(default_factory=list)
    sharpness_updates: dict[str, float] = field(default_factory=dict)
    conflict_calls: list[dict[str, object]] = field(default_factory=list)
    conflict_groups: dict[str, dict[str, object]] = field(default_factory=dict)
    resolve_conflict_calls: list[dict[str, object]] = field(default_factory=list)
    merge_entity_calls: list[dict[str, object]] = field(default_factory=list)
    related_edge_calls: list[dict[str, object]] = field(default_factory=list)
    orphan_episodes_cleaned: int = 0
    pending_episodes_count: int = 0
    # F5 demote-with-TTL stubs
    set_demote_ttl_calls: list[dict[str, object]] = field(default_factory=list)
    demoted_uuids: list[str] = field(default_factory=list)
    ttl_expired_uuids: list[dict[str, object]] = field(default_factory=list)
    # Phase 6 delete-saga stubs: per-uuid scope override (models a node promoted out of
    # SESSION mid-race) and the report-only unreferenced-evidence result.
    node_scopes: dict[str, str] = field(default_factory=dict)
    unreferenced_evidence: list[str] = field(default_factory=list)

    def fetch_session_entities(
        self,
        session_id: str | None = None,
        max_age_hours: float = 0,
    ) -> list[dict[str, object]]:
        return list(self.session_entities)

    def count_persistent_edges(self, node_uuid: str) -> int:
        return self.persistent_edge_counts.get(node_uuid, 0)

    def promote_to_persistent(self, node_uuids: list[str]) -> int:
        self.promoted_nodes.extend(node_uuids)
        return len(node_uuids)

    def delete_session_nodes(self, node_uuids: list[str]) -> int:
        self.deleted_session_nodes.extend(node_uuids)
        return len(node_uuids)

    def set_demote_ttl(self, node_uuids: list[str], ttl_days: int) -> int:
        """Stub for F5 demote-with-TTL: record call and return demoted count."""
        self.set_demote_ttl_calls.append({
            "node_uuids": node_uuids,
            "ttl_days": ttl_days,
        })
        # Track newly demoted (for test assertions)
        newly_demoted = [u for u in node_uuids if u not in self.demoted_uuids]
        self.demoted_uuids.extend(newly_demoted)
        return len(newly_demoted)

    def fetch_ttl_expired_session_uuids(self, session_id: str | None = None) -> list[dict[str, object]]:
        """Stub for F5: return programmed expired uuids."""
        return list(self.ttl_expired_uuids)

    # --- Phase 6 delete-saga stubs (the DeleteCoordinator graph_adapter contract) ---
    # DeleteCoordinator routes session-TTL expiry and single-entity deletes through
    # capture -> mutate -> verify-absence. Keep these in sync with MemoryGraphAdapter:
    # the coordinator reads `capture_node_state(u) is None` as PROOF a node is gone, so a
    # stub that always returned a snapshot would report every real delete as failed.

    def _known_node_uuids(self) -> set[str]:
        known = {str(e["uuid"]) for e in self.session_entities if e.get("uuid")}
        known |= {str(e["uuid"]) for e in self.ttl_expired_uuids if e.get("uuid")}
        known |= set(self.demoted_uuids)
        return known

    def _node_scope(self, uuid: str) -> str:
        """Scope of a known node. Programmable via node_scopes to model the promotion race."""
        if uuid in self.node_scopes:
            return self.node_scopes[uuid]
        for entity in self.session_entities:
            if str(entity.get("uuid")) == uuid:
                return str(entity.get("scope") or "SESSION")
        # TTL-expired rows come from fetch_ttl_expired_session_uuids, which by definition
        # only yields SESSION-scoped nodes.
        return "SESSION"

    def capture_node_state(self, uuid: str) -> dict[str, object] | None:
        """Lossless before-snapshot; None when the node does not exist.

        The None is load-bearing: the coordinator treats it as `already_absent` before the
        mutation and as proof of success after it.
        """
        if uuid in self.deleted_session_nodes or uuid not in self._known_node_uuids():
            return None
        return {
            "uuid": uuid,
            "labels": ["Entity"],
            "properties": {"uuid": uuid, "scope": self._node_scope(uuid)},
            "edges": [],
        }

    def delete_entities_returning_uuids(
        self, node_uuids: list[str], *, require_scope: str | None = None
    ) -> list[str]:
        """Return the uuids ACTUALLY deleted, mirroring the real scope filter.

        A node whose scope no longer matches require_scope survives -- the promotion race the
        real query guards -- so the coordinator can report it as conflicted rather than deleted.
        """
        deleted = [
            u
            for u in node_uuids
            if u not in self.deleted_session_nodes
            and u in self._known_node_uuids()
            and (require_scope is None or self._node_scope(u) == require_scope)
        ]
        self.deleted_session_nodes.extend(deleted)
        return deleted

    def newly_unreferenced_evidence(self, node_uuids: list[str]) -> list[str]:
        """REPORT ONLY -- never deletes. Programmable via unreferenced_evidence."""
        return list(self.unreferenced_evidence)

    def update_sharpness(self, node_uuid: str, sharpness: float) -> bool:
        self.sharpness_updates[node_uuid] = sharpness
        return True

    def create_related_to_edge(
        self, source_uuid: str, target_uuid: str, *, similarity: float = 0.0
    ) -> str:
        self.related_edge_calls.append({
            "source_uuid": source_uuid,
            "target_uuid": target_uuid,
            "similarity": similarity,
        })
        return f"rel-{source_uuid}-{target_uuid}"

    def merge_entity(
        self, keep_uuid: str, absorbed_uuid: str, *, similarity: float = 0.0
    ) -> dict[str, int]:
        self.merge_entity_calls.append({
            "keep_uuid": keep_uuid,
            "absorbed_uuid": absorbed_uuid,
            "similarity": similarity,
        })
        return {"edges_bridged": 1, "deleted": 1}

    def correlation_exists(self, node_uuid_a: str, node_uuid_b: str) -> bool:
        for call in self.related_edge_calls:
            if (call["source_uuid"] == node_uuid_a and call["target_uuid"] == node_uuid_b) or \
               (call["source_uuid"] == node_uuid_b and call["target_uuid"] == node_uuid_a):
                return True
        return False

    # uuid -> scope override, for tests exercising the PROMOTED merge-immunity
    # veto (SSOT-08). Default empty: fetch_entity_merge_metadata returns a bare
    # {"uuid": ...} row (no scope) for every requested uuid, matching "no veto".
    entity_scopes: dict[str, str] = field(default_factory=dict)

    def fetch_entity_merge_metadata(self, uuids: list[str]) -> list[dict[str, object]]:
        return [
            {"uuid": uuid, "name": uuid, "content": "", "scope": self.entity_scopes.get(uuid)}
            for uuid in uuids
        ]

    # --- Deterministic merge vetoes (CorrelationService.classify_pair, SSOT-03) ---
    # No fixture currently needs a veto to fire; override these on the fixture
    # instance in a specific test if one does.
    ineligible_node_veto: bool = False
    co_mention_veto: bool = False
    anchor_project_veto: bool = False

    def check_ineligible_node_veto(self, survivor_uuid: str, absorbed_uuid: str) -> bool:
        return self.ineligible_node_veto

    def check_co_mention_veto(self, uuid_a: str, uuid_b: str) -> bool:
        return self.co_mention_veto

    def check_anchor_project_veto(self, uuid_a: str, uuid_b: str) -> bool:
        return self.anchor_project_veto

    def set_conflict(
        self,
        node_uuid_a: str,
        node_uuid_b: str,
        conflict_group_id: str,
        *,
        initial_status: str = "pending_llm_review",
    ) -> tuple[str, int]:
        self.conflict_calls.append({
            "uuid_a": node_uuid_a,
            "uuid_b": node_uuid_b,
            "group_id": conflict_group_id,
            "initial_status": initial_status,
        })
        # Maintain conflict_groups state for lifecycle tests
        if conflict_group_id not in self.conflict_groups:
            self.conflict_groups[conflict_group_id] = {
                "group_id": conflict_group_id,
                "created_at": "2026-03-09T00:00:00+00:00",
                "status": initial_status,
                "members": [],
            }
        group = self.conflict_groups[conflict_group_id]
        existing_uuids = {str(m.get("uuid")) for m in group["members"]}
        for uuid in (node_uuid_a, node_uuid_b):
            if uuid not in existing_uuids:
                group["members"].append({
                    "uuid": uuid,
                    "name": uuid,
                    "content": "",
                    "status": initial_status,
                    "scope": "PERSISTENT",
                })
        return conflict_group_id, 2

    def set_conflict_group_status(self, group_id: str, status: str) -> int:
        group = self.conflict_groups.get(group_id)
        if group is None:
            return 0
        group["status"] = status
        for member in group["members"]:
            member["status"] = status
        return len(group["members"])

    def list_conflict_groups(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
        namespace: str | None = None,
        created_before: object | None = None,
        oldest_first: bool = False,
    ) -> list[dict[str, object]]:
        results = []
        for group in self.conflict_groups.values():
            if status is not None and group.get("status") != status:
                continue
            results.append({
                "group_id": group["group_id"],
                "created_at": group.get("created_at"),
                "members": list(group.get("members", [])),
            })
        return results[:limit]

    def resolve_conflict_group(
        self,
        conflict_group_id: str,
        action: str,
        *,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        resolution_status: str = "resolved",
        allow_promoted_removal: bool = False,
    ) -> dict[str, object]:
        _VALID_ACTIONS = {"keep_both", "replace", "discard_new"}
        if action not in _VALID_ACTIONS:
            raise ValueError(f"Invalid action {action!r}; must be one of {_VALID_ACTIONS}")

        group = self.conflict_groups.get(conflict_group_id)
        members = list(group["members"]) if group else []
        member_uuids = {str(m["uuid"]) for m in members}

        if action == "keep_both":
            if group:
                group["status"] = resolution_status
                for m in group["members"]:
                    m["status"] = resolution_status
            self.resolve_conflict_calls.append({
                "group_id": conflict_group_id,
                "action": action,
                "resolution_status": resolution_status,
            })
            return {
                "action": action,
                "group_id": conflict_group_id,
                "resolved": len(member_uuids),
                "removed_uuid": None,
                "removed_uuids": [],
                "bridged_edges": 0,
                "member_uuids": sorted(member_uuids),
            }

        if not keep_uuid:
            raise ValueError(f"action={action!r} requires keep_uuid")
        if keep_uuid not in member_uuids:
            raise ValueError(
                f"keep_uuid {keep_uuid!r} is not a member of conflict_group_id={conflict_group_id!r}"
            )

        if remove_uuid is None:
            remove_uuids = sorted(uuid for uuid in member_uuids if uuid != keep_uuid)
        else:
            if remove_uuid == keep_uuid:
                raise ValueError("remove_uuid cannot equal keep_uuid")
            if remove_uuid not in member_uuids:
                raise ValueError(
                    f"remove_uuid {remove_uuid!r} is not a member of "
                    f"conflict_group_id={conflict_group_id!r}"
                )
            remove_uuids = [remove_uuid]

        if not allow_promoted_removal:
            promoted = [
                m for m in members
                if str(m["uuid"]) in set(remove_uuids) and str(m.get("scope", "")) == "PROMOTED"
            ]
            if promoted:
                names = ", ".join(
                    f"{m.get('name', '(unnamed)')} ({m['uuid']})" for m in promoted
                )
                raise ValueError(
                    f"Refusing to GONE PROMOTED node(s): {names}. "
                    "Pass allow_promoted_removal=True to override."
                )

        if group:
            group["status"] = resolution_status
            for m in group["members"]:
                m["status"] = resolution_status

        self.resolve_conflict_calls.append({
            "group_id": conflict_group_id,
            "action": action,
            "keep_uuid": keep_uuid,
            "remove_uuids": remove_uuids,
            "resolution_status": resolution_status,
            "allow_promoted_removal": allow_promoted_removal,
        })
        return {
            "action": action,
            "group_id": conflict_group_id,
            "resolved": len(member_uuids) - len(remove_uuids),
            "removed_uuid": remove_uuids[0] if remove_uuids else None,
            "removed_uuids": remove_uuids,
            "bridged_edges": len(remove_uuids),
            "member_uuids": sorted(member_uuids),
        }

    def requeue_conflicts_for_llm_review(
        self,
        *,
        from_status: str = "unresolved",
        limit: int = 200,
    ) -> int:
        requeued = 0
        for group in list(self.conflict_groups.values())[:limit]:
            if group.get("status") != from_status:
                continue
            group["status"] = "pending_llm_review"
            for m in group["members"]:
                m["status"] = "pending_llm_review"
            requeued += 1
        return requeued

    def bridge_edges_for_node(self, node_uuid: str) -> int:
        return 1

    def bridge_edges_for_nodes(self, node_uuids: list[str]) -> int:
        return len(node_uuids)

    def count_pending_episodes(self, session_id: str | None = None) -> int:
        return self.pending_episodes_count

    def cleanup_orphan_episodes(self, session_id: str | None = None) -> int:
        return self.orphan_episodes_cleaned

    def fetch_adjacency_pairs(
        self,
        candidate_uuids: list[str],
        context_uuids: list[str] | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        self.adjacency_calls.append(
            {
                "candidate_uuids": list(candidate_uuids),
                "context_uuids": list(context_uuids or []),
                "namespace": namespace,
            }
        )
        return self.adjacency_pairs

    def touch_retrieved_nodes(self, node_uuids: list[str]) -> int:
        self.touch_count += len(node_uuids)
        return len(node_uuids)

    # --- Missing stubs (filled 2026-03-21) ---

    def phase_one_schema_ready(self) -> bool:
        return True

    def fetch_memory_overview(self, namespace=None) -> dict[str, object]:
        return {
            "total_memories": 0,
            "entity_count": 0,
            "episode_count": len(self.pending_episode_rows),
            "flagged_count": len(self.flagged_nodes),
            "session_count": 0,
            "persistent_count": 0,
            "promoted_count": 0,
            "pending_count": sum(
                1 for r in self.pending_episode_rows.values()
                if r.get("processing_state") == ProcessingState.PENDING
            ),
            "enriching_count": sum(
                1 for r in self.pending_episode_rows.values()
                if r.get("processing_state") == ProcessingState.ENRICHING
            ),
            "ready_count": sum(
                1 for r in self.pending_episode_rows.values()
                if r.get("processing_state") == ProcessingState.READY
            ),
            "failed_count": sum(
                1 for r in self.pending_episode_rows.values()
                if r.get("processing_state") == ProcessingState.FAILED
            ),
        }

    def fetch_recent_memories(self, limit: int = 10) -> list[dict[str, object]]:
        return []

    def fetch_flagged_memories(self, limit: int = 10) -> list[dict[str, object]]:
        return []

    def fetch_flagged_memory_bootstrap_version(self) -> str:
        return "0:0"

    def fetch_memory_by_uuid(self, node_uuid: str) -> dict[str, object] | None:
        return None

    def fetch_memories_by_scope(self, scope: str, limit: int = 10) -> list[dict[str, object]]:
        return []

    def fetch_memories_by_type(self, memory_type: str, limit: int = 10) -> list[dict[str, object]]:
        return []

    def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        row = self.pending_episode_rows.get(episode_uuid)
        if row is None or row.get("processing_state") != ProcessingState.FAILED:
            return False
        row["processing_state"] = ProcessingState.PENDING
        row["processing_stage"] = "queued"
        row["processing_substage"] = "force_reset"
        row["processing_attempts"] = 0
        row["processing_error"] = None
        row["processing_owner"] = None
        row["processing_lease_expires_at"] = None
        return True

    def reset_zero_extraction_episodes(self) -> int:
        return 0

    def list_conflict_pairs(self, *, status: str | None = "unresolved", limit: int = 25) -> list[dict[str, object]]:
        return []


@dataclass
class StubGraphitiClient:
    """Async Graphiti stand-in for ingestion and recall service tests."""

    add_episode_calls: list[dict[str, object]] = field(default_factory=list)
    build_indices_calls: int = 0
    build_indices_forces: list[bool] = field(default_factory=list)
    search_scored_results: list[tuple[str, str, float]] = field(default_factory=list)
    search_scored_calls: list[dict[str, object]] = field(default_factory=list)
    # Per-method ranked hits for the attributed hybrid path (enable_bm25=True).
    ranked_by_method_results: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    ranked_by_method_calls: list[dict[str, object]] = field(default_factory=list)
    embed_query_result: list[float] = field(default_factory=lambda: [0.1, 0.2])
    embed_query_calls: list[str] = field(default_factory=list)
    # F2: count_similar_by_cosine support
    count_similar_by_cosine_results: int | None = field(default=None)
    count_similar_by_cosine_exception: Exception | None = field(default=None)
    count_similar_by_cosine_calls: list[dict[str, object]] = field(default_factory=list)

    async def build_indices_and_constraints(self, *, force: bool = False) -> None:
        self.build_indices_calls += 1
        self.build_indices_forces.append(force)

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: object,
        episode_uuid: str | None = None,
        attempt: int = 1,
        group_id: str = "",
    ) -> None:
        self.add_episode_calls.append(
            {
                "name": name,
                "episode_body": episode_body,
                "source_description": source_description,
                "reference_time": reference_time,
                "episode_uuid": episode_uuid,
                "attempt": attempt,
                "group_id": group_id,
            }
        )
        suffix = str(len(self.add_episode_calls))
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=f"episode-{suffix}"),
            nodes=[SimpleNamespace(uuid=f"entity-{suffix}")],
            edges=[SimpleNamespace(uuid=f"rel-{suffix}")],
            episodic_edges=[SimpleNamespace(uuid=f"mentions-{suffix}")],
        )

    async def search_scored(
        self,
        query: str,
        *,
        num_results: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        self.search_scored_calls.append(
            {"query": query, "num_results": num_results, "group_ids": group_ids}
        )
        return self.search_scored_results

    async def embed_query(self, query: str) -> list[float]:
        self.embed_query_calls.append(query)
        return self.embed_query_result

    async def count_similar_by_cosine(
        self,
        query: str,
        *,
        exclude_uuid: str,
        min_cosine: float,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> int:
        """Stub implementation for count_similar_by_cosine."""
        self.count_similar_by_cosine_calls.append(
            {
                "query": query,
                "exclude_uuid": exclude_uuid,
                "min_cosine": min_cosine,
                "limit": limit,
                "group_ids": group_ids,
            }
        )
        if self.count_similar_by_cosine_exception is not None:
            raise self.count_similar_by_cosine_exception
        if self.count_similar_by_cosine_results is not None:
            return self.count_similar_by_cosine_results
        return 0

    async def search_ranked_by_method(
        self,
        query: str,
        *,
        methods: list[str],
        num_results: int = 50,
        group_ids: list[str] | None = None,
    ) -> dict[str, list[tuple[str, str]]]:
        self.ranked_by_method_calls.append(
            {"query": query, "methods": methods, "num_results": num_results, "group_ids": group_ids}
        )
        return {m: self.ranked_by_method_results.get(m, []) for m in methods}


def _build_stub_llm_adapter() -> LLMAdapter:
    """Build a deterministic LLM adapter instance for tests."""
    return LLMAdapter(
        base_url="http://localhost:1234/v1",
        api_key="local",
        chat_model="chat",
        embed_model="embed",
    )


@pytest.fixture
def stub_memory_graph_adapter() -> StubMemoryGraphAdapter:
    """Return a clean stub adapter per test."""
    return StubMemoryGraphAdapter()


@pytest.fixture
def stub_llm_adapter() -> LLMAdapter:
    """Return a consistent stub LLM adapter."""
    return _build_stub_llm_adapter()


@pytest.fixture
def stub_graphiti_client() -> StubGraphitiClient:
    """Return a clean async Graphiti stub per test."""
    return StubGraphitiClient()


@pytest.fixture(autouse=True)
def _reset_oauth_as_rate_limits():
    """Reset the embedded OAuth AS in-process rate limiters and single-use consent-token
    state before every test.

    The limiters (`_register_limiter`, `_approve_limiter`) and the spent-jti set are
    module-level singletons, so their counters would otherwise accumulate across the whole
    test process (TestClient's peer host is a constant key) and spuriously trip 429s in
    unrelated tests. Rebuilding them per test isolates each test without altering any
    individual test's assertions.
    """
    try:
        from menhir.api import oauth_as_register, oauth_authorize
    except Exception:
        yield
        return

    oauth_as_register._register_limiter = oauth_as_register.build_register_limiter()
    oauth_authorize._approve_limiter = oauth_authorize.build_approve_limiter()
    oauth_authorize._spent_consent_jtis.clear()
    yield


@pytest.fixture
def pid_namespace_verifiable(monkeypatch):
    """Declare, for this test, the deployment property that licenses PID-based death evidence.

    ``classify_ownership`` inspects a local PID only when the deployment has asserted that a
    hostname identifies exactly one PID namespace. The default is NOT asserted, so any test that
    exercises the PID-evidence path must say so explicitly. Keeping it a fixture rather than an
    ambient environment value is deliberate: the precondition stays visible in the tests that
    depend on it, and a test that forgets it fences instead of silently passing.
    """
    monkeypatch.setenv(_operation_owner.HOST_PID_NAMESPACE_ENV, "1")


@pytest.fixture(autouse=True)
def _clear_saga_admission_refusal():
    """Reset the process-lifetime saga refusal latch around EVERY test.

    ``runtime._saga_admission_refusal`` is deliberately a module global that is never cleared in
    production: a refusal to admit saga writers is a fact about the process, and a shutdown must not
    be able to erase it. Under pytest the whole suite is one process, so any test that triggers a
    refusal permanently poisons every later test that reaches ``_initialize_services`` -- which is
    exactly what happened: two startup-refusal tests latched it, and the M0 retrieval and sidecar
    consistency tests then failed with "already refused saga write admission" instead of running.

    ``monkeypatch.setattr(rt, "_saga_admission_refusal", None)`` inside a test is not a substitute.
    It restores whatever the value was on entry, so once the latch is set it puts the poison back.

    autouse and reset on BOTH sides: before, so a test never inherits a neighbour's refusal; after,
    so a test that legitimately triggers one does not leak it forward.
    """
    from menhir.core import runtime as _runtime

    _runtime._saga_admission_refusal = None
    try:
        yield
    finally:
        _runtime._saga_admission_refusal = None
