"""AST census: every automatic merge or delete path in src/menhir sits behind its switch.

Two lists are enforced against the source rather than trusted:

* every call that can merge or delete a memory must live in an allowlisted function that is gated
  (the function itself calls auto_merge_mode / auto_delete_mode), a pure delegate, or routed
  through a gated function. A new ungated caller fails here.
* every function or module/class constant holding a Cypher node delete must be classified, so a
  new delete path has to say whether it is automatic, operator-requested, derived or replay-only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).parents[1] / "src" / "menhir"

MERGE_GATE = "auto_merge_mode"
DELETE_GATE = "auto_delete_mode"

#: call target -> which switch must govern it
TARGETS = {
    "merge_entity": "merge",
    "merge": "merge",  # only `*.merge(survivor_uuid=...)`, see _call_targets
    "delete_entities_returning_uuids": "delete",
    "bridge_and_delete": "delete",
    "delete_session_nodes": "delete",
    "cleanup_orphan_episodes": "delete",
    "_run": "delete",  # only _run(kind="SESSION_TTL_DELETE"), see _call_targets
}

_MC = "services/merge_coordinator.py::MergeCoordinator"
_DC = "services/delete_coordinator.py::DeleteCoordinator"
_ADAPTER = "infrastructure/memory_graph_adapter.py::MemoryGraphAdapter"

#: (target, call-site function) -> (how it is safe, why). "gated" = the function calls the
#: switch; "delegate" = the body only forwards; "via:<fn>" = routed through that gated function;
#: "internal" = covered by test_delete_coordinator_run_graph.
ALLOWED = {
    ("merge_entity", f"{_MC}._apply_owned"): (
        "gated", "re-reads the merge switch immediately before the mutation"),
    ("merge_entity", f"{_ADAPTER}.merge_entity"): (
        "delegate", "adapter forwards to the repository; its only caller is _apply_owned"),
    ("merge", "services/correlation_service.py::CorrelationService.classify_pair"): (
        f"via:{_MC}.merge", "hands the confirmed pair to the coordinator, which gates it"),
    ("delete_entities_returning_uuids", f"{_DC}._mutate_and_verify"): (
        "internal", "only reached from _run: delete_entity (operator) or the gated TTL entry"),
    ("delete_entities_returning_uuids", f"{_ADAPTER}.delete_entities_returning_uuids"): (
        "delegate", "adapter forwards to the repository; its only caller is the coordinator"),
    ("_run", f"{_DC}.delete_expired_session_nodes"): (
        "gated", "off/propose return before any graph read or journal write"),
    ("bridge_and_delete", f"{_DC}._mutate_and_verify"): (
        "internal", "only reached from _run via the DECAY_DELETE kind; _run_decay gates delete_decayed_node"),
    ("bridge_and_delete", f"{_ADAPTER}.bridge_and_delete"): (
        "delegate", "adapter forwards to the repository; its only caller is _run_decay"),
    ("delete_session_nodes", f"{_ADAPTER}.delete_session_nodes"): (
        "delegate", "adapter forwards to the repository; nothing else calls it"),
    ("cleanup_orphan_episodes",
     "services/lifecycle_consolidation.py::LifecycleConsolidationMixin._cleanup_orphan_episodes"): (
        "gated", "the one helper both consolidation call sites use"),
    ("cleanup_orphan_episodes", f"{_ADAPTER}.cleanup_orphan_episodes"): (
        "delegate", "adapter forwards to the repository"),
    # The dry-run preview in lifecycle_consolidation passes dry_run=True and deletes nothing; it is
    # a read-only count (see test_cleanup_orphan_episodes_call_sites).
    ("cleanup_orphan_episodes",
     "services/lifecycle_consolidation.py::LifecycleConsolidationMixin.preview_orphan_recovery"): (
        "dry_run", "passes dry_run=True: a count, never a delete"),
}

GATED_FUNCTIONS = {
    f"{_MC}.merge": "merge",
    f"{_MC}._apply_owned": "merge",
    f"{_DC}.delete_expired_session_nodes": "delete",
    "services/lifecycle_decay.py::LifecycleDecayMixin._run_decay": "delete",
    "services/lifecycle_consolidation.py::LifecycleConsolidationMixin._cleanup_orphan_episodes": "delete",
}

#: Functions (or module/class constants) holding a Cypher node delete -> (class, reason).
#: automatic: runs on policy, so its callers must be in ALLOWED. operator: a person or an explicit
#: request asked for it. derived: rebuilds data that is computed from other data. replay-only:
#: reachable only from a journal replay or an unmerge.
_I = "infrastructure/"
CLASSIFIED = {
    _I + "consolidation_queries.py::ConsolidationRepository.bridge_and_delete":
        ("automatic", "decay deletion; gated in _run_decay"),
    _I + "consolidation_queries.py::ConsolidationRepository.delete_entities_returning_uuids":
        ("automatic", "TTL sweep and delete_entity; TTL entry gated"),
    _I + "consolidation_queries.py::ConsolidationRepository.delete_session_nodes":
        ("automatic", "session purge; no caller beyond the adapter delegate"),
    _I + "correlation_queries.py::CorrelationRepository.merge_entity":
        ("automatic", "entity merge; gated in MergeCoordinator"),
    _I + "episode_maintenance.py::EpisodeMaintenanceRepository.cleanup_orphan_episodes":
        ("automatic", "orphan episode cleanup; gated helper"),
    _I + "correlation_queries.py::CorrelationRepository.restore_merge_snapshot":
        ("replay-only", "unmerge restores a prior merge"),
    _I + "candidate_repository.py::CandidateRepository.reject_candidate":
        ("operator", "candidate rejection"),
    _I + "memory_queries.py::MemoryQueryRepository._delete_memory_with_scalar_cascade_in_namespace":
        ("operator", "operator-requested erasure"),
    _I + "memory_queries.py::MemoryQueryRepository.delete_namespace_with_scalar_cascade":
        ("operator", "namespace delete"),
    _I + "turn_evidence_repository.py::TurnEvidenceRepository.purge_namespace":
        ("operator", "namespace purge"),
    _I + "todo_repository.py::TodoRepository.delete_todo": ("operator", "explicit todo delete"),
    _I + "work_artifact_repository.py::WorkArtifactRepository.delete_artifact":
        ("operator", "explicit artifact delete"),
    _I + "cypher.py::Cypher._render_op": ("derived", "query builder rendering, not a call site"),
    _I + "episode_lifecycle.py::EpisodeLifecycleRepository.clear_uncompleted_twin_link":
        ("derived", "drops an uncited empty twin before re-extraction (#905)"),
    _I + "evidence_lifecycle_schema.py::_REPOINT_PUBLICATIONS_CYPHER":
        ("derived", "repoints evidence publications"),
    _I + "memory_queries.py::MemoryQueryRepository._DERIVED_EDGE_BODY":
        ("derived", "derived-edge rebuild"),
    _I + "memory_queries.py::MemoryQueryRepository._DERIVED_ENTITY_WRITE":
        ("derived", "derived-entity rebuild"),
    _I + "memory_queries.py::MemoryQueryRepository.apply_derived_rederive":
        ("derived", "derived data rebuild"),
    _I + "reversion_repository.py::_RETIRE_SET": ("derived", "retires superseded projections"),
    _I + "scalar_view_repository.py::ScalarViewRepositoryMixin.draw_scalar_history_entries":
        ("derived", "scalar projection rebuild"),
    _I + "scalar_view_repository.py::ScalarViewRepositoryMixin.draw_scalar_state_provenance_edges":
        ("derived", "scalar projection rebuild"),
    _I + "schema.py::get_scalar_state_activation_queries":
        ("derived", "scalar state activation"),
    _I + "structure_queries.py::StructureGraphWriter._delete_file_entities":
        ("derived", "code index rebuild"),
    _I + "structure_queries.py::StructureGraphWriter._delete_stale_contains_repo_edges":
        ("derived", "code index rebuild"),
    _I + "structure_queries.py::StructureGraphWriter._delete_stale_derived_edges":
        ("derived", "code index rebuild"),
    _I + "structure_queries.py::StructureGraphWriter._delete_stale_directories":
        ("derived", "code index rebuild"),
    _I + "structure_queries.py::StructureGraphWriter._delete_stale_role_entities":
        ("derived", "code index rebuild"),
    _I + "structure_queries.py::StructureGraphWriter._delete_stale_role_entities_multi":
        ("derived", "code index rebuild"),
    _I + "structure_queries.py::StructureGraphWriter._write_symbols":
        ("derived", "code index rebuild"),
    _I + "turn_evidence_repository.py::TurnEvidenceRepository.retire_prior_version_occurrences":
        ("derived", "evidence version retirement"),
    _I + "typed_assertion_models.py::_RECORD_CYPHER": ("derived", "assertion record rebuild"),
    _I + "typed_assertion_reconciliation.py::TypedAssertionReconciliationMixin.purge_scalar_state_nodes":
        ("operator", "explicit scalar-state migration; no caller in src"),
    _I + "typed_assertion_reconciliation.py::TypedAssertionReconciliationMixin.restore_rebound_assertions":
        ("derived", "assertion rebind"),
    _I + "view_write_repository.py::ViewWriteRepositoryMixin._replace_fact_provenance":
        ("derived", "view provenance rewrite"),
    "snapshot/view_root.py::purge_root": ("derived", "view root rebuild"),
}

_DELETE_RE = re.compile(r"DETACH\s+DELETE|(?<![A-Za-z_])DELETE\s+(?!FROM\b)[a-z_(]")


# --------------------------------------------------------------------------- AST helpers
def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _without_comments(text: str) -> str:
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and _is_str(body[0].value):
                ids.add(id(body[0].value))
    return ids


def _functions(tree: ast.AST):
    """Yield (qualname, node) for every function, qualified by enclosing classes/functions."""
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
    """Nodes of a function body excluding nested function/class bodies."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _attr_or_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _call_targets(call: ast.Call) -> set[str]:
    """Which census targets this call reaches, directly or as a callable handed to to_thread."""
    found = set()
    keywords = {k.arg: k.value for k in call.keywords}
    handed = [_attr_or_name(a) for a in call.args]
    for name in [_attr_or_name(call.func), *handed]:
        if name in ("merge_entity", "delete_entities_returning_uuids", "bridge_and_delete",
                    "delete_session_nodes", "cleanup_orphan_episodes"):
            found.add(name)
        if name == "merge" and "survivor_uuid" in keywords:
            found.add("merge")
    if _attr_or_name(call.func) == "_run":
        kind = keywords.get("kind")
        if isinstance(kind, ast.Constant) and kind.value == "SESSION_TTL_DELETE":
            found.add("_run")
    return found


def find_mutation_calls(source: str, rel: str) -> list[tuple[str, str, ast.AST]]:
    """(target, 'rel::qualname', function node) for each census call in this source."""
    out = []
    for qualname, func in _functions(ast.parse(source)):
        for node in _own_nodes(func):
            if isinstance(node, ast.Call):
                for target in _call_targets(node):
                    out.append((target, f"{rel}::{qualname}", func))
    return out


def calls_gate(func: ast.AST, kind: str) -> bool:
    gate = MERGE_GATE if kind == "merge" else DELETE_GATE
    return any(isinstance(n, ast.Call) and _attr_or_name(n.func) == gate for n in _own_nodes(func))


def is_pure_delegate(func: ast.AST) -> bool:
    body = list(func.body)
    if body and isinstance(body[0], ast.Expr) and _is_str(body[0].value):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)):
        return False
    call = body[0].value
    if not isinstance(call, ast.Call):
        return False
    simple = (ast.Name, ast.Attribute, ast.Constant)
    return all(isinstance(a, simple) for a in call.args) and all(
        isinstance(k.value, simple) for k in call.keywords
    ) and isinstance(call.func, ast.Attribute)


def find_delete_scopes(source: str, rel: str) -> set[str]:
    """'rel::scope' for each function or module/class constant with a Cypher node delete."""
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    hits: set[str] = set()

    def has_delete(node: ast.AST, own_only: bool) -> bool:
        nodes = _own_nodes(node) if own_only else ast.walk(node)
        for n in nodes:
            if _is_str(n) and id(n) not in docs and _DELETE_RE.search(_without_comments(n.value)):
                return True
            if isinstance(n, ast.Call) and _attr_or_name(n.func) == "detach_delete":
                return True
        return False

    for qualname, func in _functions(tree):
        if has_delete(func, own_only=True):
            hits.add(f"{rel}::{qualname}")

    def constants(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                constants(child, f"{prefix}.{child.name}" if prefix else child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if has_delete(child, own_only=False):
                    for t in targets:
                        if isinstance(t, ast.Name):
                            hits.add(f"{rel}::{prefix + '.' if prefix else ''}{t.id}")
    constants(tree, "")
    return hits


def _sources():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8")


def _all_calls():
    calls = []
    for rel, source in _sources():
        calls.extend(find_mutation_calls(source, rel))
    return calls


def _function_nodes():
    nodes = {}
    for rel, source in _sources():
        for qualname, func in _functions(ast.parse(source)):
            nodes[f"{rel}::{qualname}"] = func
    return nodes


# --------------------------------------------------------------------------- the census
def test_every_mutation_call_is_gated_or_delegated():
    nodes = _function_nodes()
    problems = []
    seen = set()
    for target, site, func in _all_calls():
        seen.add((target, site))
        entry = ALLOWED.get((target, site))
        if entry is None:
            problems.append(f"ungated call to {target} in {site}")
            continue
        how = entry[0]
        kind = TARGETS[target]
        if how == "gated" and not calls_gate(func, kind):
            problems.append(f"{site} is listed gated but never calls the {kind} switch")
        elif how == "delegate" and not is_pure_delegate(func):
            problems.append(f"{site} is listed as a pure delegate but does more than forward")
        elif how.startswith("via:"):
            via = how[4:]
            if via not in nodes or not calls_gate(nodes[via], kind):
                problems.append(f"{site} routes through {via}, which is not gated")
        elif how not in ("gated", "delegate", "internal", "dry_run") and not how.startswith("via:"):
            problems.append(f"{site}: unknown allowlist kind {how!r}")
        if not entry[1]:
            problems.append(f"{site}: allowlist entry has no reason")
    stale = sorted(set(ALLOWED) - seen)
    assert problems == [], problems
    assert stale == [], f"allowlist entries with no matching call: {stale}"


def test_gated_functions_really_call_their_switch():
    nodes = _function_nodes()
    for name, kind in GATED_FUNCTIONS.items():
        assert name in nodes, name
        assert calls_gate(nodes[name], kind), f"{name} no longer calls the {kind} switch"


def test_delete_session_nodes_has_no_callers_beyond_its_adapter_delegate():
    sites = [site for target, site, _ in _all_calls() if target == "delete_session_nodes"]
    assert sites == [f"{_ADAPTER}.delete_session_nodes"]


def test_cleanup_orphan_episodes_call_sites():
    # Live deletion goes through the one gated helper; the only other call is the dry-run preview.
    rel = "services/lifecycle_consolidation.py"
    source = dict(_sources())[rel]
    tree = ast.parse(source)
    for qualname, func in _functions(tree):
        for node in _own_nodes(func):
            if isinstance(node, ast.Call) and "cleanup_orphan_episodes" in _call_targets(node):
                if qualname.endswith("_cleanup_orphan_episodes"):
                    continue
                assert qualname.endswith("preview_orphan_recovery"), qualname
                assert any(isinstance(a, ast.Constant) and a.value is True
                           for a in node.keywords and [k.value for k in node.keywords]), qualname


def test_delete_coordinator_run_graph():
    """_run is reached only from delete_entity (operator) and the gated TTL entry."""
    rel = "services/delete_coordinator.py"
    tree = ast.parse(dict(_sources())[rel])
    run_sites, mutate_sites = set(), set()
    for qualname, func in _functions(tree):
        for node in _own_nodes(func):
            if isinstance(node, ast.Call):
                name = _attr_or_name(node.func)
                if name == "_run":
                    kind = {k.arg: k.value for k in node.keywords}["kind"].value
                    run_sites.add((qualname, kind))
                if name == "_mutate_and_verify":
                    mutate_sites.add(qualname)
    assert run_sites == {
        ("DeleteCoordinator.delete_entity", "ENTITY_DELETE"),
        ("DeleteCoordinator.delete_expired_session_nodes", "SESSION_TTL_DELETE"),
        ("DeleteCoordinator.delete_decayed_node", "DECAY_DELETE"),
    }
    assert mutate_sites == {"DeleteCoordinator._run"}


def test_merge_coordinator_merge_is_the_only_caller_of_prepare_for_entity_merge():
    nodes = _function_nodes()
    assert calls_gate(nodes[f"{_MC}.merge"], "merge")
    sites = []
    for rel, source in _sources():
        for qualname, func in _functions(ast.parse(source)):
            for node in _own_nodes(func):
                if isinstance(node, ast.Call) and _attr_or_name(node.func) == "prepare":
                    for kw in node.keywords:
                        if kw.arg == "operation_kind" and getattr(kw.value, "value", "") == "ENTITY_MERGE":
                            sites.append(f"{rel}::{qualname}")
    assert sites == [f"{_MC}.merge"]


# --------------------------------------------------------------------------- delete classification
def test_every_cypher_node_delete_is_classified():
    found: set[str] = set()
    for rel, source in _sources():
        found |= find_delete_scopes(source, rel)
    unclassified = sorted(found - CLASSIFIED.keys())
    stale = sorted(CLASSIFIED.keys() - found)
    assert unclassified == [], f"classify these deletes (automatic/operator/derived/replay-only): {unclassified}"
    assert stale == [], f"classified but no longer deleting: {stale}"


def test_classifications_are_well_formed_and_automatic_ones_are_census_targets():
    for key, (category, reason) in CLASSIFIED.items():
        assert category in {"automatic", "operator", "derived", "replay-only"}, key
        assert reason.strip(), key
        if category == "automatic":
            assert key.rsplit(".", 1)[-1] in TARGETS, f"{key}: automatic deletes must be census targets"


# --------------------------------------------------------------------------- self-tests
_UNGATED = '''
class Sweeper:
    def sweep(self):
        self.log("sweeping")
        return self.graph_adapter.bridge_and_delete("x")
'''
_GATED = '''
class Sweeper:
    def sweep(self):
        if auto_delete_mode() != "on":
            return 0
        return self.graph_adapter.bridge_and_delete("x")
'''
_DELEGATE = '''
class Adapter:
    def bridge_and_delete(self, node_uuid):
        """Forward."""
        return self._consolidation.bridge_and_delete(node_uuid)
'''
_BUSY_DELEGATE = '''
class Adapter:
    def bridge_and_delete(self, node_uuid):
        self.log(node_uuid)
        return self._consolidation.bridge_and_delete(node_uuid)
'''


def test_selftest_ungated_caller_is_detected():
    ((target, site, func),) = find_mutation_calls(_UNGATED, "x.py")
    assert (target, site) == ("bridge_and_delete", "x.py::Sweeper.sweep")
    assert not calls_gate(func, "delete") and not is_pure_delegate(func)
    assert ("bridge_and_delete", site) not in ALLOWED


def test_selftest_gate_and_delegate_are_recognised():
    ((_, _, gated),) = find_mutation_calls(_GATED, "x.py")
    assert calls_gate(gated, "delete") and not calls_gate(gated, "merge")
    ((_, _, delegate),) = find_mutation_calls(_DELEGATE, "x.py")
    assert is_pure_delegate(delegate)
    ((_, _, busy),) = find_mutation_calls(_BUSY_DELEGATE, "x.py")
    assert not is_pure_delegate(busy)


def test_selftest_to_thread_form_and_run_kind_are_seen():
    src = '''
async def f(self):
    await asyncio.to_thread(self.merge_coordinator.merge, survivor_uuid="s", absorbed_uuid="a")
    self._run(kind="SESSION_TTL_DELETE", targets=[])
    self._run(kind="ENTITY_DELETE", targets=[])
'''
    assert sorted(t for t, _, _ in find_mutation_calls(src, "x.py")) == ["_run", "merge"]


def test_selftest_delete_scan_ignores_docstrings_comments_and_sql():
    src = '''
"""Module doc: DETACH DELETE mentioned here."""
Q = "MATCH (n) DETACH DELETE n"
SQL = "DELETE FROM pending_actions WHERE x = 1"
class C:
    CONST = "MATCH ()-[r]-() DELETE r"
    def doc_only(self):
        """Uses DETACH DELETE in prose."""
        return "// DETACH DELETE below is a comment"
    def builder(self):
        return Cypher().match("(n)").detach_delete("n")
    def literal(self):
        return "MATCH (n) DETACH DELETE n"
'''
    assert find_delete_scopes(src, "x.py") == {
        "x.py::Q", "x.py::C.CONST", "x.py::C.builder", "x.py::C.literal"}
