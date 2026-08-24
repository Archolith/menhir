"""CF-257 phase 0 -- the identity decision must be re-taken inside the detached writer.

`scan_and_write_project` decides ownership, scans (minutes, on a large tree), and then schedules
`_do_write` as a DETACHED task. The write happens later still. In that window another root can
claim the project name, or the directory can become a worktree -- and the write carries the
per-project stale prune, so landing it under an identity that is no longer this root's deletes
the other copy's files.

A detached task inherits a decision's VALUE, never its freshness. These tests simulate the
interleaving rather than asserting that a function is called: the stub reports one state when the
entry guard asks and a different state when the detached writer asks, which is exactly what a
concurrent claim looks like from inside this process.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.fixture
def ops(monkeypatch, tmp_path):
    """A data-ops mixin wired to stubs, scanning a real (plain) directory."""
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
    import menhir.core.backend_runtime_data_ops as mod
    import menhir.infrastructure.project_scanner as scanner_module

    root = tmp_path / "proj"
    root.mkdir()

    written: list[str] = []
    errors: list[str] = []

    class _Scan:
        name = "proj"
        root_path = str(root)
        scan_fingerprint = "fp-new"
        directories = files = dependencies = endpoints = imports = []
        test_edges = cross_project_refs = symbols = call_edges = []

    class _Scanner:
        def scan(self, path, name):
            return _Scan()

    monkeypatch.setattr(scanner_module, "ProjectScanner", _Scanner)
    # SEC-02's containment is a separate guard with its own tests; allow the temp root so this
    # module exercises the identity decision rather than re-testing path containment.
    from menhir.core import ingest_guard
    monkeypatch.setattr(ingest_guard, "allowed_ingest_roots", lambda: [tmp_path])
    monkeypatch.setattr(mod, "build_project_narrative", lambda scan: "narrative")
    monkeypatch.setattr(mod, "get_request_tier", lambda: "agent")
    monkeypatch.setattr(
        mod, "_push_background_error", lambda session_id, msg: errors.append(msg)
    )

    class _Neo4j:
        """Enough of the graph for identity settling: no candidate, binding always accepted."""
        def execute(self, cypher, params=None):
            if "MERGE (p:ProjectIdentity" in cypher:
                return [{"bound_root": (params or {}).get("root_path"), "state": "bound"}]
            return []

    instance = RuntimeProviderDataOpsMixin()
    # The mixin normally gets `_off_loop` from the class it is mixed into; here it just has to
    # run the callable, since the stubs do no blocking I/O.
    async def _off_loop(fn, *a, **kw):
        return fn(*a, **kw)
    instance._off_loop = _off_loop
    instance.built = SimpleNamespace(
        graph_adapter=SimpleNamespace(
            get_scan_fingerprint=lambda name: None,
            get_project_root_path=lambda name: None,
            write_project_structure=lambda scan, s, u: written.append(scan.name) or {},
            neo4j=_Neo4j(),
        )
    )
    return SimpleNamespace(
        ops=instance, root=root, written=written, errors=errors,
        adapter=instance.built.graph_adapter,
    )


async def _run_and_drain(ops_bundle, **kwargs):
    # `identity_action="new"` models a first scan of a fresh checkout: CF-257 phase 1 never mints
    # silently, so without an action every call here would return needs_decision and no test below
    # would reach the write it is actually about.
    kwargs.setdefault("identity_action", "new")
    result = await ops_bundle.ops.scan_and_write_project(
        str(ops_bundle.root), name="proj", force=True,
        session_id="s", user_id="u", **kwargs,
    )
    # The write is a detached task; let it run to completion before asserting.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return result


@pytest.mark.unit
def test_an_unchanged_identity_still_writes(ops):
    asyncio.run(_run_and_drain(ops))
    assert ops.written == ["proj"], "the ordinary path must still write"
    assert ops.errors == []


@pytest.mark.unit
def test_a_claim_landing_during_the_scan_stops_the_detached_write(ops):
    """THE COUNTEREXAMPLE this guard exists for.

    The entry guard sees an unclaimed name and admits the scan. While the scan runs, another root
    claims `proj`. The detached writer must notice and refuse -- if it writes, it overwrites that
    other root's structure and stale-prunes the files this copy does not have.
    """
    answers = iter([None, "C:/somewhere/else/proj"])

    def racing_lookup(name):
        try:
            return next(answers)
        except StopIteration:
            return "C:/somewhere/else/proj"

    ops.adapter.get_project_root_path = racing_lookup

    asyncio.run(_run_and_drain(ops))

    assert ops.written == [], "the detached write must not land after the name was claimed"
    assert ops.errors and "refused" in ops.errors[0]


@pytest.mark.unit
def test_the_directory_becoming_a_worktree_during_the_scan_stops_the_write(ops, monkeypatch):
    """The other half of the same window: shape changes rather than ownership.

    A directory replaced by a worktree checkout between the decision and the write is the case
    the entry guard cannot see, because it already ran.
    """
    from menhir.infrastructure.repo_topology import RootKind, RootTopology
    import menhir.infrastructure.repo_topology as topo

    real = topo.classify_root
    calls = {"n": 0}

    def flaky(root):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(root)          # entry guard: still a plain directory
        return RootTopology(           # detached write: now a worktree
            kind=RootKind.WORKTREE, root=ops.root,
            primary_worktree=ops.root.parent / "canonical",
            detail="worktree",
        )

    monkeypatch.setattr(topo, "classify_root", flaky)

    asyncio.run(_run_and_drain(ops))

    assert calls["n"] >= 2, "the detached writer must classify again, not reuse the entry verdict"
    assert ops.written == []
    assert ops.errors and "refused" in ops.errors[0]


@pytest.mark.unit
def test_the_operator_override_still_reaches_the_detached_write(ops, monkeypatch):
    """The override must survive into the detached task, or forcing would work at the entry and
    then silently fail minutes later -- the worst of both."""
    import menhir.core.backend_runtime_data_ops as mod
    monkeypatch.setattr(mod, "get_request_tier", lambda: "operator")
    ops.adapter.get_project_root_path = lambda name: "C:/somewhere/else/proj"

    asyncio.run(_run_and_drain(ops, force_identity=True))

    assert ops.written == ["proj"]


@pytest.mark.unit
def test_the_raw_structure_writer_requires_operator_tier():
    """`write_project_structure` accepts a payload the caller produced AND the `root_path` used to
    judge it, so the server classifies a path string rather than the directory that produced the
    structure. A remote worktree reporting the server's canonical path is classified as the
    canonical clone and accepted -- and the write stale-prunes. No metadata check closes that,
    because every input to it is caller-controlled.
    """
    from menhir.api.routes_support import _OP_TIER_AGENT, _OP_TIER_OPERATOR

    assert "write_project_structure" in _OP_TIER_OPERATOR
    assert "write_project_structure" not in _OP_TIER_AGENT
    # The server-side scan remains available at agent tier: it produces the structure itself, so
    # the payload is not caller-controlled and the guard judges a directory it actually read.
    assert "scan_and_write_project" in _OP_TIER_AGENT


# ---------------------------------------------------------------------------
# Deprecation bridge: loud refusal, measured use, evidence-gated removal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_raw_writer_is_marked_deprecated_with_an_actionable_message():
    """A refusal that only says 'wrong tier' teaches a client nothing.

    The operator gate is a migration bridge, so the failure has to carry the migration target --
    a caller learns it from the error itself rather than from a changelog they may never read.
    """
    from menhir.api.routes_support import DEPRECATED_OPERATIONS, deprecated_operation_notice

    notice = deprecated_operation_notice("write_project_structure")
    assert notice is not None
    assert "scan_and_write_project" in notice, "must name the replacement"
    assert "operator" in notice, "must name the other way through"
    assert "write_project_structure" in DEPRECATED_OPERATIONS
    # A current operation must not be flagged, or the notice means nothing.
    assert deprecated_operation_notice("scan_and_write_project") is None


@pytest.mark.unit
def test_both_admitted_and_refused_calls_are_recorded(monkeypatch):
    """Removal is gated on evidence of no LEGITIMATE use, which refusals alone cannot show.

    A window with zero refusals proves only that nobody under-privileged tried. If the admitted
    count were not also recorded, an endpoint an operator still runs could be deleted on the
    strength of a silence that was never about them.
    """
    import menhir.api.routes_support as rs

    seen: list[dict] = []
    # Patch the FUNCTION on the real module: `from pkg import mod` resolves the package
    # attribute, so swapping sys.modules leaves the already-imported package untouched and the
    # test silently observes nothing.
    monkeypatch.setattr(
        "menhir.infrastructure.telemetry.recorders.record_mcp_event",
        lambda **kwargs: seen.append(kwargs),
    )
    rs.record_deprecated_operation_call("write_project_structure", admitted=False)
    rs.record_deprecated_operation_call("write_project_structure", admitted=True)

    assert [e["success"] for e in seen] == [False, True]
    assert {e["operation"] for e in seen} == {"write_project_structure"}
    assert all(e["kind"] == "deprecated_operation" for e in seen)


@pytest.mark.unit
def test_measurement_failure_never_breaks_the_request(monkeypatch):
    """This runs on the path of an already-failing call. Telemetry must not add a second fault."""
    import menhir.api.routes_support as rs

    def _boom(**kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(
        "menhir.infrastructure.telemetry.recorders.record_mcp_event", _boom
    )
    rs.record_deprecated_operation_call("write_project_structure", admitted=False)  # must not raise
