"""AST census: every recall-visibility call site declares whether it hides retired memories.

`retire_memory` hides a memory from DEFAULT recall surfaces and leaves it visible (labelled) on
HISTORICAL ones. That split lives in one switch, `include_retired`, on
`default_recall_visibility_cypher` and `search_episode_embeddings`. This census classifies every
call to either in src/menhir, so a new call site has to choose, and `include_retired=True` can
only appear at the allow-listed HISTORICAL sites.

* DEFAULT: no `include_retired`, or a literal False -- retired memories are hidden.
* HISTORICAL: a literal `include_retired=True` -- retired memories stay, labelled.
* MAINTENANCE: a literal `include_retired=True` on a backfill listing, not a recall surface.
* DELEGATE: forwards a variable `include_retired` and decides nothing itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).parents[1] / "src" / "menhir"

VISIBILITY = "default_recall_visibility_cypher"
SEARCH = "search_episode_embeddings"
TARGETS = (VISIBILITY, SEARCH)

_I = "infrastructure/"
_EL = _I + "episode_lifecycle.py"
_MQ = _I + "memory_queries.py"
_MQR = "MemoryQueryRepository."
_SV = _I + "scalar_view_repository.py"
_SVM = "ScalarViewRepositoryMixin."

#: (module, enclosing function, target) -> classification.
CLASSIFIED: dict[tuple[str, str, str], str] = {
    # --- DEFAULT: retired memories are hidden ---------------------------------------------------
    (_EL, "EpisodeLifecycleRepository.fetch_episode_processing", VISIBILITY): "DEFAULT",
    (_MQ, "_flagged_pin_filter", VISIBILITY): "DEFAULT",
    (_MQ, _MQR + "fetch_recent_memories", VISIBILITY): "DEFAULT",
    (_MQ, _MQR + "fetch_memories_by_scope", VISIBILITY): "DEFAULT",
    (_MQ, _MQR + "fetch_memories_by_type", VISIBILITY): "DEFAULT",
    # Views never carry memory_retired, so these are unchanged by the marker.
    (_SV, _SVM + "_fetch_current", VISIBILITY): "DEFAULT",
    (_SV, _SVM + "fetch_current_scalar_view_for_slot", VISIBILITY): "DEFAULT",
    (_SV, _SVM + "list_recall_visible_scalar_history_views", VISIBILITY): "DEFAULT",
    (_SV, _SVM + "list_scalar_history_views_for_namespace", VISIBILITY): "DEFAULT",
    (_I + "view_query_repository.py", "ViewQueryRepositoryMixin.list_views", VISIBILITY): "DEFAULT",
    # The source-memory lane of ranked recall, and its same-session neighbor window (#1057).
    ("services/recall_pipeline.py", "_build_source_memories", SEARCH): "DEFAULT",
    (_MQ, _MQR + "get_session_episode_neighbors", VISIBILITY): "DEFAULT",
    # --- DELEGATE: threads the caller's choice through ------------------------------------------
    (_MQ, _MQR + "search_episode_embeddings", VISIBILITY): "DELEGATE",
    (_I + "memory_graph_adapter.py", "MemoryGraphAdapter.search_episode_embeddings", SEARCH): "DELEGATE",
    # --- MAINTENANCE: embedding backfill must cover retired episodes so history can find them ----
    (_EL, "EpisodeLifecycleRepository.list_episodes_missing_content_embedding", VISIBILITY):
        "MAINTENANCE",
    # --- HISTORICAL: retired memories stay, labelled `retired` ----------------------------------
    (_MQ, _MQR + "_timeline_episode_where", VISIBILITY): "HISTORICAL",
    (_MQ, _MQR + "timeline_anchor", VISIBILITY): "HISTORICAL",
    (_MQ, _MQR + "resolve_timeline_subject", VISIBILITY): "HISTORICAL",
    ("services/recall_pipeline.py", "run_recall_history", SEARCH): "HISTORICAL",
    ("services/timeline_service.py", "run_recall_timeline", SEARCH): "HISTORICAL",
}


#: Classification -> what the source shows: both HISTORICAL and MAINTENANCE pass a literal True.
_SOURCE_KIND = {"HISTORICAL": "TRUE", "MAINTENANCE": "TRUE"}


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _functions(tree: ast.AST):
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                if not isinstance(child, ast.ClassDef):
                    yield name, child
                yield from walk(child, name)
            else:
                yield from walk(child, prefix)
    yield from walk(tree, "")


def _own_nodes(func: ast.AST):
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _kind(call: ast.Call) -> str:
    for kw in call.keywords:
        if kw.arg == "include_retired":
            if isinstance(kw.value, ast.Constant):
                return "TRUE" if kw.value.value is True else "DEFAULT"
            return "DELEGATE"
    return "DEFAULT"


def find_sites(source: str, rel: str) -> dict[tuple[str, str, str], set[str]]:
    """(module, function, target) -> kinds seen. A target may be called or handed to to_thread."""
    out: dict[tuple[str, str, str], set[str]] = {}
    for qualname, func in _functions(ast.parse(source)):
        for node in _own_nodes(func):
            if not isinstance(node, ast.Call):
                continue
            names = {_name_of(node.func), *(_name_of(a) for a in node.args)}
            for target in TARGETS:
                if target in names:
                    out.setdefault((rel, qualname, target), set()).add(_kind(node))
    return out


def _all_sites() -> dict[tuple[str, str, str], set[str]]:
    sites: dict[tuple[str, str, str], set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for key, kinds in find_sites(path.read_text(encoding="utf-8"), rel).items():
            sites.setdefault(key, set()).update(kinds)
    return sites


def test_every_visibility_call_site_is_classified() -> None:
    sites = _all_sites()
    unclassified = sorted(set(sites) - set(CLASSIFIED))
    assert not unclassified, (
        "classify each new call as DEFAULT (hides retired) or HISTORICAL (include_retired=True): "
        f"{unclassified}"
    )
    stale = sorted(set(CLASSIFIED) - set(sites))
    assert not stale, f"classified call sites that no longer exist: {stale}"


def test_each_call_site_matches_its_classification() -> None:
    mismatched = [
        f"{key}: classified {CLASSIFIED[key]}, source is {sorted(kinds)}"
        for key, kinds in sorted(_all_sites().items())
        if key in CLASSIFIED and kinds != {_SOURCE_KIND.get(CLASSIFIED[key], CLASSIFIED[key])}
    ]
    assert not mismatched, mismatched


def test_include_retired_true_only_at_historical_sites() -> None:
    allowed = {key for key, kind in CLASSIFIED.items() if kind in ("HISTORICAL", "MAINTENANCE")}
    offenders = sorted(
        key for key, kinds in _all_sites().items() if "TRUE" in kinds and key not in allowed
    )
    assert not offenders, f"include_retired=True outside the HISTORICAL allow-list: {offenders}"


def test_default_surfaces_never_opt_in() -> None:
    # The recall pipeline's source lane is the one DEFAULT caller of the search; it must not
    # pass include_retired at all.
    sites = _all_sites()
    assert sites[("services/recall_pipeline.py", "_build_source_memories", SEARCH)] == {"DEFAULT"}
