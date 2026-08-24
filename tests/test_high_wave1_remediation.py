"""Counterexample tests for the 15 findings remediated in HIGH remediation wave 1:
CF-11, 12, 66, 103, 144, 156, 170, 172, 183, 184, 185, 186, 188, 197, 204.

Each test reproduces the failing scenario the finding recorded, not the shape of the fix.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


# ---------------------------------------------------------------------------
# CF-183 / CF-184 / CF-185 -- graphiti_helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"name": "Alice"} and also {"name": "Bob"}',
        '{"name": "Alice"}\nNote: use {curly} braces carefully.',
        'Here you go:\n```json\n{"name": "Alice"}\n```\nHope that helps {ok}',
    ],
)
def test_cf183_chatty_wrappers_still_yield_parseable_json(raw: str) -> None:
    from menhir.infrastructure.graphiti_helpers import _extract_first_json_payload

    assert json.loads(_extract_first_json_payload(raw)) == {"name": "Alice"}


def test_cf183_unrecoverable_text_is_returned_unchanged() -> None:
    from menhir.infrastructure.graphiti_helpers import _extract_first_json_payload

    assert _extract_first_json_payload("no json here at all") == "no json here at all"


def test_cf184_entity_name_survives_regardless_of_key_order() -> None:
    """Two payloads differing only in serialization order must not produce different identities."""
    from menhir.infrastructure.graphiti_helpers import _normalize_graphiti_json_payload

    order1 = {"entity_name": "Alice Smith", "entity": "Bob Jones", "summary": "s"}
    order2 = {"entity": "Bob Jones", "entity_name": "Alice Smith", "summary": "s"}

    assert _normalize_graphiti_json_payload(order1)["name"] == "Alice Smith"
    assert _normalize_graphiti_json_payload(order2)["name"] == "Alice Smith"


def test_cf184_explicit_name_wins_over_both_aliases() -> None:
    from menhir.infrastructure.graphiti_helpers import _normalize_graphiti_json_payload

    payload = {"entity": "Bob", "name": "Alice", "entity_name": "Carol"}
    assert _normalize_graphiti_json_payload(payload)["name"] == "Alice"


def test_cf185_summary_promoted_to_a_fact_is_marked_synthetic() -> None:
    """A node summary reused as an edge fact was never asserted by the model as that fact."""
    from menhir.infrastructure.graphiti_helpers import _build_graphiti_edge_fact

    assert _build_graphiti_edge_fact({"summary": "Alice is a senior engineer."}) == (
        "Alice is a senior engineer.",
        True,
    )
    assert _build_graphiti_edge_fact({"description": "d"})[1] is True
    # `relationship` is the key-variant spelling of the edge's OWN fact text, not a borrowed
    # node field, so it stays model-asserted.
    assert _build_graphiti_edge_fact({"relationship": "r"})[1] is False
    assert _build_graphiti_edge_fact({"fact": "Alice works with Bob"}) == (
        "Alice works with Bob",
        False,
    )


def test_cf185_synthetic_fallback_reaches_storage_with_the_prefix() -> None:
    """End to end through the normalizer: the marker the storage boundary classifies on."""
    from menhir.infrastructure.graphiti_helpers import (
        SYNTHETIC_FACT_PREFIX,
        _normalize_graphiti_json_payload,
    )

    normalized = _normalize_graphiti_json_payload(
        {
            "source_entity_name": "Alice",
            "target_entity_name": "Bob",
            "relation_type": "WORKS_WITH",
            "summary": "Alice is a senior engineer.",
        }
    )
    assert normalized["fact"].startswith(SYNTHETIC_FACT_PREFIX)


# ---------------------------------------------------------------------------
# CF-186 / CF-197 / CF-185 titled list -- graphiti_extraction_patches
# ---------------------------------------------------------------------------


def test_cf186_patched_model_keeps_upstream_required_fields_and_descriptions() -> None:
    """`{}` must fail validation, not validate as a successful zero-extraction."""
    pydantic = pytest.importorskip("pydantic")
    ene = pytest.importorskip("graphiti_core.prompts.extract_nodes_and_edges")
    from menhir.infrastructure.graphiti_extraction_patches import (
        _patch_graphiti_combined_extraction_models,
    )

    _patch_graphiti_combined_extraction_models()
    patched = ene.CombinedExtraction
    schema = patched.model_json_schema()

    assert set(schema.get("required", [])) == {"extracted_entities", "edges"}
    props = schema["properties"]
    assert props["extracted_entities"].get("description")
    assert props["edges"].get("description")

    with pytest.raises(pydantic.ValidationError):
        patched(**{})


def test_cf197_echo_edge_is_dropped_even_when_the_model_lists_user() -> None:
    """The prompt tells the model to include `user` in extracted_entities. That used to put the
    endpoint in `known`, short-circuit the echo branch, and persist the echo edge with the
    receipt reporting zero suppressed."""
    from menhir.infrastructure.graphiti_extraction_patches import (
        CombinedExtractionReceipt,
        _sanitize_combined_payload,
    )

    episode = "assistant: You mentioned you want to build a web scraper."
    payload = {
        "extracted_entities": [
            {"name": "user", "entity_type_id": -1},
            {"name": "web scraper", "entity_type_id": -1},
        ],
        "edges": [
            {
                "relation_type": "WANTS_TO_BUILD",
                "source_entity_name": "user",
                "target_entity_name": "web scraper",
                "fact": "user wants to build a web scraper",
                "episode_indices": [0],
            }
        ],
    }
    receipt = CombinedExtractionReceipt(episode_key="e1", episode_text=episode)
    out = _sanitize_combined_payload(payload, receipt, episode)

    assert out["edges"] == []
    assert receipt.self_echo_edges_suppressed == 1


def test_cf197_non_echo_edges_on_an_assistant_turn_survive() -> None:
    from menhir.infrastructure.graphiti_extraction_patches import (
        CombinedExtractionReceipt,
        _sanitize_combined_payload,
    )

    episode = "assistant: Redis and Postgres are both datastores."
    payload = {
        "extracted_entities": [
            {"name": "Redis", "entity_type_id": -1},
            {"name": "Postgres", "entity_type_id": -1},
        ],
        "edges": [
            {
                "relation_type": "SIMILAR_TO",
                "source_entity_name": "Redis",
                "target_entity_name": "Postgres",
                "fact": "Redis and Postgres are both datastores",
                "episode_indices": [0],
            }
        ],
    }
    receipt = CombinedExtractionReceipt(episode_key="e2", episode_text=episode)
    out = _sanitize_combined_payload(payload, receipt, episode)

    assert len(out["edges"]) == 1
    assert receipt.self_echo_edges_suppressed == 0


def test_cf12_combined_extractor_is_bound_at_patch_time() -> None:
    """The replacement's dependency must be proven inside the patch's own ImportError guard."""
    import menhir.infrastructure.graphiti_extraction_patches as patches

    patches._patch_graphiti_combined_extraction()
    assert patches._graphiti_combined_extraction_module is not None
    assert patches._original_graphiti_extract_nodes is not None


def test_cf12_extractor_is_resolved_per_call_not_frozen(monkeypatch) -> None:
    """Binding the FUNCTION at patch time would freeze the seam; bind the module and read it."""
    import menhir.infrastructure.graphiti_extraction_patches as patches
    from graphiti_core.utils.maintenance import combined_extraction as ce

    patches._patch_graphiti_combined_extraction()
    sentinel = object()
    monkeypatch.setattr(ce, "extract_nodes_and_edges", sentinel)
    assert patches._resolve_combined_extractor() is sentinel


def test_cf12_patch_restores_originals_when_the_dependency_is_missing(monkeypatch) -> None:
    """A patch that cannot complete must leave Graphiti on its own extractors, not on a
    replacement whose dependency is absent while logging success."""
    import graphiti_core.graphiti as graphiti_module
    from graphiti_core.utils.maintenance import combined_extraction as ce

    import menhir.infrastructure.graphiti_extraction_patches as patches

    monkeypatch.setattr(graphiti_module, "_menhir_combined_extraction_patched", False, raising=False)
    sentinel_nodes = object()
    sentinel_edges = object()
    monkeypatch.setattr(graphiti_module, "extract_nodes", sentinel_nodes, raising=False)
    monkeypatch.setattr(graphiti_module, "extract_edges", sentinel_edges, raising=False)
    monkeypatch.setattr(patches, "_original_graphiti_extract_nodes", None, raising=False)
    monkeypatch.setattr(patches, "_original_graphiti_extract_edges", None, raising=False)
    monkeypatch.setattr(patches, "_graphiti_combined_extraction_module", None, raising=False)
    monkeypatch.delattr(ce, "extract_nodes_and_edges")

    patches._patch_graphiti_combined_extraction()

    assert graphiti_module.extract_nodes is sentinel_nodes
    assert graphiti_module.extract_edges is sentinel_edges
    assert not getattr(graphiti_module, "_menhir_combined_extraction_patched", False)
    assert patches._graphiti_combined_extraction_module is None


# ---------------------------------------------------------------------------
# CF-156 -- embedding cache
# ---------------------------------------------------------------------------


def test_cf156_short_upstream_response_is_not_padded_with_empty_vectors() -> None:
    """One cache hit, two misses, upstream returns one item. The gap used to become `[]`."""
    from menhir.infrastructure.observability import _CachingEmbeddingsEndpoint

    class _Cache:
        def get(self, text, model=""):
            return [0.1, 0.2] if text == "hit" else None

        def set(self, text, vec, model=""):
            return None

    upstream_result = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.9, 0.9], index=0, object="embedding")],
        model="m",
        object="list",
        usage=SimpleNamespace(prompt_tokens=0, total_tokens=0),
    )

    class _Inner:
        async def create(self, **kwargs):
            return upstream_result

    endpoint = _CachingEmbeddingsEndpoint(_Inner(), _Cache())
    result = asyncio.run(endpoint.create(input=["hit", "miss1", "miss2"], model="m"))

    assert result is upstream_result
    assert all(len(item.embedding) > 0 for item in result.data)


# ---------------------------------------------------------------------------
# CF-11 -- list repair regex
# ---------------------------------------------------------------------------


def test_cf11_quantities_do_not_fragment_a_fact() -> None:
    """`Alice owns 2 dogs` used to be persisted as two fragments, `Alice owns` and `dogs`,
    stamped fact_source="llm_repaired"."""
    from menhir.infrastructure.llm import LLMAdapter

    adapter = LLMAdapter.__new__(LLMAdapter)

    async def _fake_chat_text(**kwargs):
        return "1. Bob owns a cat 2. Alice owns 2 dogs"

    adapter._chat_text = _fake_chat_text  # type: ignore[method-assign]
    edges = [
        {"source": "Bob", "target": "cat", "relation": "OWNS"},
        {"source": "Alice", "target": "dogs", "relation": "OWNS"},
    ]
    assert asyncio.run(adapter.repair_edge_facts("episode", edges)) == [
        "Bob owns a cat",
        "Alice owns 2 dogs",
    ]


# ---------------------------------------------------------------------------
# CF-144 -- one connect seam for every writer of the shared telemetry file
# ---------------------------------------------------------------------------


def test_cf144_busy_timeout_env_var_reaches_the_connection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MENHIR_TELEMETRY_BUSY_TIMEOUT_S", "37")
    import importlib

    from menhir.infrastructure.telemetry import helpers as helpers_module

    importlib.reload(helpers_module)
    try:
        conn = helpers_module.connect_telemetry_db(tmp_path / "t.db")
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 37000
        finally:
            conn.close()
    finally:
        monkeypatch.delenv("MENHIR_TELEMETRY_BUSY_TIMEOUT_S", raising=False)
        importlib.reload(helpers_module)


def test_cf144_no_writer_of_the_shared_db_opens_it_bare() -> None:
    """Every store defaulting its db_path to the telemetry file must route through the seam.

    The register listed five; erasure_subjects and scheduler_lease are a sixth and seventh.
    """
    offenders: list[str] = []
    for path in list(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "default_factory=default_telemetry_db_path" not in text:
            continue
        if "sqlite3.connect(self.db_path)" in text:
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == []


# ---------------------------------------------------------------------------
# CF-170 / CF-204 -- no synchronous SQLite left on the event loop
# ---------------------------------------------------------------------------


def _sync_calls_in_async_bodies(path: pathlib.Path, names: set[str]) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def in_async(node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.AsyncFunctionDef):
                return True
            if isinstance(cur, ast.FunctionDef):
                return False
            cur = parents.get(cur)
        return False

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in names and in_async(node):
            hits.append(node.lineno)
    return hits


def test_cf170_no_bare_lifecycle_writes_remain_inside_async_bodies() -> None:
    for rel in (
        "infrastructure/graphiti_client.py",
        "infrastructure/llama_endpoint.py",
        "infrastructure/scheduler_trace.py",
    ):
        assert _sync_calls_in_async_bodies(_SRC / rel, {"record_lifecycle_event"}) == [], rel


def test_cf170_breaker_keeps_exactly_one_synchronous_write_inside_its_lock() -> None:
    """The HALF_OPEN transition write must NOT be awaited: it sits between
    `_probe_in_flight = True` and the caller reaching `await fn()`, the one stretch the
    probe-abandon handler does not cover, so a cancellation there wedges the breaker."""
    path = _SRC / "infrastructure/circuit_breaker.py"
    remaining = _sync_calls_in_async_bodies(path, {"record_lifecycle_event"})
    assert len(remaining) == 1

    tree = ast.parse(path.read_text(encoding="utf-8"))
    inside_lock = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "record_lifecycle_event":
                inside_lock = True
    assert inside_lock, "the surviving synchronous write should be the in-lock transition"


def test_cf204_explorer_does_not_read_pending_actions_on_the_loop() -> None:
    assert _sync_calls_in_async_bodies(_SRC / "explorer/app.py", {"fetch_pending"}) == []


# ---------------------------------------------------------------------------
# CF-66 -- raw-capture preservation is reachable, bounded, and indexed
# ---------------------------------------------------------------------------


def test_cf66_fail_exhausted_is_defined_exactly_once_on_the_adapter() -> None:
    tree = ast.parse((_SRC / "infrastructure/memory_graph_adapter.py").read_text(encoding="utf-8"))
    defs = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "fail_exhausted_pending_episodes"
    ]
    assert len(defs) == 1


def test_cf66_surviving_definition_creates_raw_captures() -> None:
    """The bare-delegation duplicate is gone, so the preservation body must be the live one."""
    import inspect

    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    body = inspect.getsource(MemoryGraphAdapter.fail_exhausted_pending_episodes)
    assert "create_raw_capture_entity" in body
    assert "fetch_exhausted_pending_episodes" in body


def test_cf66_exhausted_fetch_is_bounded() -> None:
    captured: dict[str, object] = {}

    class _Neo4j:
        def execute(self, query, params=None):
            captured["query"] = query
            captured["params"] = params
            return []

    from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository

    repo = EpisodeMaintenanceRepository()
    repo.neo4j = _Neo4j()
    repo.fetch_exhausted_pending_episodes(max_attempts=3)

    assert "LIMIT" in str(captured["query"]).upper()
    assert isinstance(captured["params"], dict) and captured["params"]["limit"] > 0


def test_cf66_raw_capture_for_is_indexed() -> None:
    from menhir.infrastructure.schema import get_phase1_bootstrap_queries

    joined = " ".join(get_phase1_bootstrap_queries())
    assert "raw_capture_for" in joined


# ---------------------------------------------------------------------------
# CF-103 -- the ingest path guard covers the sibling too
# ---------------------------------------------------------------------------


def test_cf103_symbol_rescan_path_is_guarded_at_both_sites() -> None:
    source = (_SRC / "core/backend_runtime_data_ops.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for fname in ("write_project_structure", "_background_symbol_rescan"):
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == fname
        )
        calls = {
            getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
        }
        assert "ensure_ingest_path_allowed" in calls, fname


def test_cf103_rescan_refuses_a_root_outside_the_allowed_ingest_roots(monkeypatch, tmp_path) -> None:
    from menhir.core import ingest_guard
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(ingest_guard, "allowed_ingest_roots", lambda: [allowed])

    scanned: list[str] = []

    class _Scanner:
        def scan(self, root, name):
            scanned.append(root)
            return SimpleNamespace(symbols=[], root_path=root, name=name)

    import menhir.infrastructure.project_scanner as scanner_module

    monkeypatch.setattr(scanner_module, "ProjectScanner", _Scanner)

    ops = RuntimeProviderDataOpsMixin()
    # CF-257 phase 0 gave the rescan a second guard, which reads the project's recorded root_path
    # to refuse a fork. That is a new collaborator, not a new assertion: everything this test
    # checks about CF-103's containment is unchanged below. `None` means "no project has claimed
    # this name", which is the state that lets the operator-tier case proceed exactly as before.
    ops.built = SimpleNamespace(
        graph_adapter=SimpleNamespace(get_project_root_path=lambda name: None)
    )
    asyncio.run(
        ops._background_symbol_rescan(str(outside), "proj", "s", "u", tier="agent")
    )
    assert scanned == []

    asyncio.run(
        ops._background_symbol_rescan(str(outside), "proj", "s", "u", tier="operator")
    )
    assert scanned == [str(outside.resolve())]


# ---------------------------------------------------------------------------
# CF-188 -- the sync chat seam never silently egresses to api.openai.com
# ---------------------------------------------------------------------------


def test_cf188_empty_base_url_refuses_rather_than_defaulting_to_openai(monkeypatch) -> None:
    from menhir.config import MemorySettings
    from menhir.infrastructure import sync_llm
    from menhir.infrastructure.providers import ProviderConfig, ProviderKind

    monkeypatch.setattr(
        ProviderConfig,
        "for_chat",
        classmethod(
            lambda cls, settings: ProviderConfig(
                kind=ProviderKind.LOCAL,
                base_url="",
                api_key="not-needed",
                chat_model="local-model",
                embed_model="",
            )
        ),
    )
    monkeypatch.setattr(sync_llm, "should_use_scheduler", lambda base_url: True)
    monkeypatch.setattr(
        sync_llm, "acquire_llama_url_sync", lambda **kwargs: kwargs.get("fallback") or ""
    )

    chat = sync_llm.make_sync_chat(MemorySettings.from_env())
    assert chat is not None
    with pytest.raises(RuntimeError):
        chat("system", "personal memory content")


def test_cf188_resolved_scheduler_url_is_used(monkeypatch) -> None:
    from menhir.config import MemorySettings
    from menhir.infrastructure import sync_llm
    from menhir.infrastructure.providers import ProviderConfig, ProviderKind

    built: dict[str, object] = {}

    class _Client:
        # **_bounds absorbs timeout/max_retries, which the seam now passes explicitly (CF-190).
        # This test is about WHICH base_url is used, so the bounds are irrelevant to it -- but a
        # stub narrower than the real constructor turns an added argument into a TypeError here.
        def __init__(self, api_key=None, base_url=None, **_bounds):
            built["base_url"] = base_url
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
                    )
                )
            )

    monkeypatch.setattr(
        ProviderConfig,
        "for_chat",
        classmethod(
            lambda cls, settings: ProviderConfig(
                kind=ProviderKind.LOCAL,
                base_url="http://127.0.0.1:8081/v1",
                api_key="not-needed",
                chat_model="local-model",
                embed_model="",
            )
        ),
    )
    monkeypatch.setattr(sync_llm, "should_use_scheduler", lambda base_url: True)
    monkeypatch.setattr(
        sync_llm, "acquire_llama_url_sync", lambda **kwargs: "http://127.0.0.1:9099/v1"
    )
    import openai

    monkeypatch.setattr(openai, "OpenAI", _Client)

    chat = sync_llm.make_sync_chat(MemorySettings.from_env())
    chat("system", "user")
    assert built["base_url"] == "http://127.0.0.1:9099/v1"
