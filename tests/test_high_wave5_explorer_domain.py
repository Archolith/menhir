"""Counterexample tests for HIGH wave 5 (CF-13, CF-18, CF-70, CF-117, CF-130, CF-138).

Each test reproduces the scenario the register recorded, not the shape of the fix.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


# ---------------------------------------------------------------------------
# CF-70 -- 36 explorer routes mounted with no tier enforcement at all
# ---------------------------------------------------------------------------


def test_cf70_every_explorer_route_inherits_a_tier_floor() -> None:
    """The same FastAPI app enforced tier 23 times on its own routes and zero times on the
    explorer's. Enforcement is on the ROUTER so a new route cannot be added without it --
    a per-route fix would have covered the 36 that existed and exempted the 37th."""
    source = (_SRC / "explorer/app.py").read_text(encoding="utf-8")
    assert "APIRouter(dependencies=[Depends(_require_explorer_readonly)])" in source


def test_cf70_readonly_floor_delegates_to_the_api_tier_check() -> None:
    """Reusing api.routes_support._require_tier keeps the two surfaces from drifting apart in
    how they rank tiers or shape the 403."""
    source = (_SRC / "explorer/app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_require_explorer_readonly"
    )
    body = ast.unparse(fn)
    assert "_require_tier" in body
    assert "readonly" in body


def test_cf70_mutating_routes_are_escalated_above_the_floor() -> None:
    """The floor is readonly. Routes that mutate graph state or spend LLM budget re-assert
    agent in their own bodies."""
    tree = ast.parse((_SRC / "explorer/app.py").read_text(encoding="utf-8"))
    escalated = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_require_explorer_agent"
            for c in ast.walk(n)
        )
    }
    for required in (
        "recall_lab_api",
        "extraction_lab_api",
        "approve_candidate",
        "reject_candidate",
        "bench_task_query_filtered_packet_api",
    ):
        assert required in escalated, required


def test_cf70_set_privacy_stays_at_the_floor_deliberately() -> None:
    """It mutates a per-browser cookie, not server state, and the direction that matters --
    reveal -- is independently gated on a loopback bind. Escalating it would restrict turning
    redaction ON, which is the safe direction. Pinned so the reasoning is not lost."""
    tree = ast.parse((_SRC / "explorer/app.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "set_privacy"
    )
    calls = {
        getattr(c.func, "id", None)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    }
    assert "_require_explorer_agent" not in calls
    assert "DELIBERATELY left at the router's readonly floor" in ast.get_docstring(fn)


# ---------------------------------------------------------------------------
# CF-13 -- the explorer was fully constructed on every import
# ---------------------------------------------------------------------------


def test_cf13_no_module_level_app_instance() -> None:
    """`app = create_app()` at module scope built a full FastAPI application on every import,
    and the import chain runs on every production start -- so the `explorer_enabled` gate 176
    lines later could not prevent it."""
    tree = ast.parse((_SRC / "explorer/app.py").read_text(encoding="utf-8"))
    module_level_assigns = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert "app" not in module_level_assigns


def test_cf13_package_does_not_rebind_the_submodule_name() -> None:
    """`from .app import app` rebound the package attribute `app` from the SUBMODULE to a
    FastAPI instance, so `menhir.explorer.app` meant different things in attribute context and
    in sys.modules -- where the tests patch."""
    import menhir.explorer as explorer_pkg

    assert "app" not in getattr(explorer_pkg, "__all__", [])
    import types

    assert isinstance(getattr(explorer_pkg, "app", None), (types.ModuleType, type(None)))


# ---------------------------------------------------------------------------
# CF-18 -- timeline mis-ordered the slash dates it deliberately accepts
# ---------------------------------------------------------------------------


def _ev(when: str, what: str) -> Any:
    return SimpleNamespace(when=when, what=what, identity=None, episode_uuid=None)


def test_cf18_slash_dates_order_by_calendar_not_by_byte() -> None:
    """`_parse` goes out of its way to accept slash dates. `timeline` sorted on the RAW string,
    where "-" (0x2D) precedes "/" (0x2F), so every slash date sorted after every dash date
    regardless of its actual calendar position."""
    from menhir.domain.fold_algebra import timeline

    rows = timeline([
        _ev("2023/05/07", "slash-may"),
        _ev("2023-12-01", "dash-december"),
        _ev("2023-01-02", "dash-january"),
    ])
    assert [r["what"] for r in rows] == ["dash-january", "slash-may", "dash-december"]


def test_cf18_unparseable_values_sort_last_and_are_not_dropped() -> None:
    from menhir.domain.fold_algebra import timeline

    rows = timeline([
        _ev("not a date", "junk"),
        _ev("2023-01-02", "real"),
    ])
    assert [r["what"] for r in rows] == ["real", "junk"]


def test_cf18_raw_when_is_still_returned_unchanged() -> None:
    """Only the ORDER changes; the returned value is still the raw stored string."""
    from menhir.domain.fold_algebra import timeline

    rows = timeline([_ev("2023/05/07", "slash")])
    assert rows[0]["when"] == "2023/05/07"


# ---------------------------------------------------------------------------
# CF-130 -- a stated count lost to an intervening noun, overwritten with 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "I read 2 books every week",
        "I take 3 pills every day",
        "I do 5 loads of laundry every month",
    ],
)
def test_cf130_abstains_when_a_stated_count_could_not_be_attributed(phrase: str) -> None:
    """The count group requires its number IMMEDIATELY before "every", so an intervening noun
    defeats it and the fabricated 1 then OVERWRITES the model's own extracted value at
    parse_scalar_row -- with no fallback and no drop. Abstaining leaves the model's value."""
    from menhir.services.typed_scalar_rules import _normalize_interval_frequency

    assert _normalize_interval_frequency(phrase) is None


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("I go 3 times every week", (3, "week")),
        ("every other week", (0.5, "week")),
        ("I review the plan every week", (1, "week")),
    ],
)
def test_cf130_genuine_no_count_and_adjacent_count_are_unchanged(phrase: str, expected) -> None:
    """A phrase with no number at all is genuinely count-1, and an adjacent count still
    normalizes. The abstention must not swallow either."""
    from menhir.services.typed_scalar_rules import _normalize_interval_frequency

    assert _normalize_interval_frequency(phrase) == expected


def test_cf130_caller_keeps_the_model_value_when_normalization_abstains() -> None:
    """The abstention is only safe because the caller treats None as "leave it alone"."""
    source = (_SRC / "services/typed_scalar_rules.py").read_text(encoding="utf-8")
    assert "if normalized_frequency is not None:" in source


# ---------------------------------------------------------------------------
# CF-138 -- embedder_ready reported True having checked nothing
# ---------------------------------------------------------------------------


def test_cf138_no_path_reports_ready_without_a_determination() -> None:
    """`embedder_ready = True` was an optimistic initial value, and when the embed endpoint
    equalled the llama endpoint AND no embed model was configured, neither branch ran -- so a
    flag meaning "checked and healthy" was produced by a path that checked nothing."""
    source = (_SRC / "core/runtime_preflight.py").read_text(encoding="utf-8")
    assert "\n    embedder_ready = True\n" not in source


def test_cf138_unconfigured_embedder_is_not_ready_and_does_not_gate_startup() -> None:
    """`local_llm_embed_model` defaults to "", so a local deployment with no embed model is a
    real supported configuration. Reporting it NOT ready is honest; adding it to `failures`
    would refuse to start for configurations that work today."""
    tree = ast.parse((_SRC / "core/runtime_preflight.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "collect_runtime_capabilities"
    )
    src = ast.unparse(fn)
    assert "embedder_ready = False" in src


# ---------------------------------------------------------------------------
# CF-117 -- transition_artifact mutated lifecycle by uuid with no tenant predicate
# ---------------------------------------------------------------------------


class _RecordingNeo4j:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows if rows is not None else []

    def execute(self, query: str, params: dict[str, Any] | None = None):
        self.calls.append((query, params or {}))
        return list(self._rows)


def test_cf117_tool_declares_namespace_so_the_pin_can_reach_it() -> None:
    tree = ast.parse((_SRC / "mcp/tools/ops/transition_artifact.py").read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "TransitionArtifactTool"
    )
    endpoint = next(
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "endpoint"
    )
    assert "namespace" in [a.arg for a in endpoint.args.args + endpoint.args.kwonlyargs]


def test_cf117_both_the_legality_read_and_the_mutation_are_scoped() -> None:
    """Scoping only the preflight read leaves the read-to-write window unguarded. The merge
    path closes the same race by repeating the predicate inside the mutation."""
    from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

    neo4j = _RecordingNeo4j(rows=[{"artifact_type": "plan", "status": "PROPOSED"}])
    repo = WorkArtifactRepository(neo4j=neo4j)
    repo.transition_status("a-1", "REVIEWED", namespace="tenant-a")

    assert len(neo4j.calls) >= 2
    for query, params in neo4j.calls[:2]:
        assert "a.namespace = $namespace" in query
        assert params.get("namespace") == "tenant-a"


def test_cf117_unscoped_transition_is_unchanged() -> None:
    from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

    neo4j = _RecordingNeo4j(rows=[{"artifact_type": "plan", "status": "PROPOSED"}])
    repo = WorkArtifactRepository(neo4j=neo4j)
    repo.transition_status("a-1", "REVIEWED")

    for query, params in neo4j.calls:
        assert "namespace" not in query
        assert "namespace" not in params
