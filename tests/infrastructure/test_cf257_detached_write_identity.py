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
    # Operator tier because `_run_and_drain` supplies identity_action="new", and identity
    # TRANSFER is operator-gated -- an agent submitting an arbitrary adopt_project_id could
    # otherwise rebind a project it has no relationship to. These tests are about the
    # detached write, not about tiering; the tier rules have their own tests.
    monkeypatch.setattr(mod, "get_request_tier", lambda: "operator")
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


# ---------------------------------------------------------------------------
# The compatibility writer validates identity; it never settles one
# ---------------------------------------------------------------------------

def _compat_ops(monkeypatch, *, bound_id, root):
    """A data-ops mixin whose graph reports `bound_id` as the binding for `root`, or none."""
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
    import menhir.core.backend_runtime_data_ops as mod
    from menhir.core import ingest_guard

    monkeypatch.setattr(ingest_guard, "allowed_ingest_roots", lambda: [root.parent])
    monkeypatch.setattr(mod, "get_request_tier", lambda: "operator")
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )

    written: list[object] = []

    class _Neo4j:
        """Reports `bound_id` as the binding for `root`, and models the re-bind that follows.

        The compat path now re-binds the id it read, because that is what yields the claim
        generation the write boundary validates -- so the fake has to answer the bind, not just
        the lookup.
        """

        def __init__(self):
            self.node = (
                {
                    "canonical_root_path": str(root),
                    "state": "bound",
                    "bound_host": "h1",
                    "root_key": None,
                    "claim_generation": 0,
                }
                if bound_id
                else None
            )

        def execute(self, cypher, params=None):
            params = params or {}
            if "RETURN p.project_id AS id" in cypher and bound_id:
                # `p.project_id <> $project_id` excludes the id being bound, so the rival scan
                # sees nothing while the plain lookup sees the binding.
                if params.get("project_id") == bound_id:
                    return []
                return [{"id": bound_id, "root": str(root), "root_key": self.node["root_key"]}]
            if "MERGE (p:ProjectIdentity" in cypher and self.node:
                return [
                    {
                        "bound_root": self.node["canonical_root_path"],
                        "state": self.node["state"],
                        "bound_host": self.node["bound_host"],
                        "root_key": self.node["root_key"],
                        "claim_generation": self.node["claim_generation"],
                    }
                ]
            if "SET p.bound_host = $host, p.root_key = $root_key" in cypher and self.node:
                self.node["bound_host"] = params["host"]
                self.node["root_key"] = params["root_key"]
                return []
            return []

    instance = RuntimeProviderDataOpsMixin()

    async def _off_loop(fn, *a, **kw):
        return fn(*a, **kw)

    instance._off_loop = _off_loop
    instance.built = SimpleNamespace(
        graph_adapter=SimpleNamespace(
            get_project_root_path=lambda name: None,
            write_project_structure=lambda scan, s, u: written.append(scan) or {},
            neo4j=_Neo4j(),
        )
    )
    return instance, written


def _payload(root, **extra):
    base = {
        "name": "proj",
        "root_path": str(root),
        "directories": [], "files": [], "dependencies": [], "endpoints": [],
        "imports": [], "test_edges": [], "cross_project_refs": [],
        "symbols": [], "call_edges": [], "scan_fingerprint": "fp",
    }
    base.update(extra)
    return base


@pytest.mark.unit
def test_an_unbound_directory_is_refused_rather_than_given_an_identity(monkeypatch, tmp_path):
    """THE BYPASS. This endpoint used to call `settle_project_identity` on the payload's
    `root_path`, so a caller-supplied path string could MINT an identity -- or resolve to an
    existing project's id and write a caller-authored payload, stale prune included, into its
    silo. Identity here is read from the graph or the request is refused."""
    from menhir.domain.project_identity import ProjectIdentityRefused

    root = tmp_path / "proj"
    root.mkdir()
    ops_, written = _compat_ops(monkeypatch, bound_id=None, root=root)

    with pytest.raises(ProjectIdentityRefused, match="no active project binding"):
        asyncio.run(
            ops_.write_project_structure(_payload(root), session_id="s", user_id="u")
        )
    assert written == [], "a refused request must not reach the writer"


@pytest.mark.unit
def test_a_supplied_id_that_contradicts_the_binding_is_refused(monkeypatch, tmp_path):
    """Non-null was the whole of the old check. A populated field is not a fact about the sender."""
    from menhir.domain.project_identity import ProjectIdentityRefused

    root = tmp_path / "proj"
    root.mkdir()
    ops_, written = _compat_ops(monkeypatch, bound_id="real-id", root=root)

    with pytest.raises(ProjectIdentityRefused, match="bound to 'real-id'"):
        asyncio.run(
            ops_.write_project_structure(
                _payload(root, project_id="forged-id"), session_id="s", user_id="u"
            )
        )
    assert written == []


@pytest.mark.unit
def test_a_correctly_bound_id_still_writes_under_the_authoritative_identity(monkeypatch, tmp_path):
    """The bridge stays usable for its one legitimate caller, so the observation window measures
    real use rather than an endpoint that was quietly broken before it was measured."""
    root = tmp_path / "proj"
    root.mkdir()
    ops_, written = _compat_ops(monkeypatch, bound_id="real-id", root=root)

    asyncio.run(
        ops_.write_project_structure(
            _payload(root, project_id="real-id"), session_id="s", user_id="u"
        )
    )
    assert [s.project_id for s in written] == ["real-id"]


@pytest.mark.unit
def test_an_absent_id_resolves_from_the_binding_not_from_the_payload(monkeypatch, tmp_path):
    """An older client sends no id. It is filled from server-side state keyed on (host, root) --
    never from anything the caller said about which project this is."""
    root = tmp_path / "proj"
    root.mkdir()
    ops_, written = _compat_ops(monkeypatch, bound_id="real-id", root=root)

    asyncio.run(ops_.write_project_structure(_payload(root), session_id="s", user_id="u"))
    assert [s.project_id for s in written] == ["real-id"]


# ---------------------------------------------------------------------------
# Transfer authorization fails closed
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("tier", ["", None, "readonly", "agent"])
def test_an_identity_transfer_is_refused_for_every_non_operator_tier(ops, monkeypatch, tier):
    """`tier and tier != OPERATOR` let an UNBOUND tier straight through, and `get_request_tier()`
    returns "" whenever auth is not configured -- so the gate was open on exactly the deployments
    least able to notice. Presence of the tier is now part of the check."""
    import menhir.core.backend_runtime_data_ops as mod
    from menhir.domain.project_identity import ProjectIdentityRefused

    monkeypatch.setattr(mod, "get_request_tier", lambda: tier)
    with pytest.raises(ProjectIdentityRefused, match="requires operator tier"):
        asyncio.run(_run_and_drain(ops, identity_action="new"))
    assert ops.written == []


# ---------------------------------------------------------------------------
# Document writers settle first and cross the durable fence
# ---------------------------------------------------------------------------

def _document_ops(monkeypatch, tmp_path, *, recorded_root, claim, resolution):
    from menhir.core import ingest_guard
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
    import menhir.core.backend_runtime_data_ops as mod
    import menhir.services.project_identity_service as identity_service

    monkeypatch.setattr(ingest_guard, "allowed_ingest_roots", lambda: [tmp_path])
    monkeypatch.setattr(mod, "get_request_tier", lambda: "operator")

    settled: list[dict] = []
    written: list[dict] = []

    def _settle(adapter, **kwargs):
        settled.append(kwargs)
        return claim, resolution

    monkeypatch.setattr(identity_service, "settle_project_identity", _settle)

    class _Adapter:
        neo4j = object()

        def get_project_root_path(self, project):
            return recorded_root

        def write_document(self, file_path, content, **kwargs):
            written.append({"file_path": file_path, "content": content, **kwargs})

    instance = RuntimeProviderDataOpsMixin()

    async def _off_loop(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    instance._off_loop = _off_loop
    instance.built = SimpleNamespace(graph_adapter=_Adapter())
    return SimpleNamespace(ops=instance, settled=settled, written=written)


@pytest.mark.unit
def test_document_needs_decision_has_zero_structural_calls(monkeypatch, tmp_path):
    doc = tmp_path / "ambiguous.md"
    doc.write_text("must not be written", encoding="utf-8")
    decision = {
        "status": "needs_decision",
        "decision_type": "project_identity",
        "actions": ["adopt", "new"],
    }
    resolution = SimpleNamespace(as_dict=lambda: decision)
    bundle = _document_ops(
        monkeypatch, tmp_path, recorded_root=None, claim=None, resolution=resolution
    )

    result = asyncio.run(
        bundle.ops.ingest_document(
            str(doc), project="proj", session_id="s", user_id="u"
        )
    )

    assert result == decision
    assert bundle.written == []
    assert bundle.settled == [{
        "root_path": str(tmp_path.resolve()),
        "display_name": "proj",
        "identity_action": None,
        "adopt_project_id": None,
    }]


@pytest.mark.unit
def test_document_runtime_uses_recorded_root_and_forwards_claim(monkeypatch, tmp_path):
    doc = tmp_path / "settled.md"
    doc.write_text("settled content", encoding="utf-8")
    recorded_root = str(tmp_path / "recorded-project-root")
    claim = SimpleNamespace(project_id="project-id-1", generation=7)
    bundle = _document_ops(
        monkeypatch, tmp_path, recorded_root=recorded_root, claim=claim, resolution=None
    )

    result = asyncio.run(
        bundle.ops.ingest_document(
            str(doc), project="proj", session_id="s", user_id="u",
            document_type="reference_article",
        )
    )

    assert bundle.settled[0]["root_path"] == recorded_root
    assert len(bundle.written) == 1
    write = bundle.written[0]
    assert write["project_id"] == "project-id-1"
    assert write["identity_generation"] == 7
    assert write["identity_root"] == recorded_root
    assert result["entity_written"] is True
    assert result["narrative"].endswith("settled content")


def _bare_memory_adapter(writer):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = object.__new__(MemoryGraphAdapter)
    adapter.neo4j = object()
    adapter._structure = SimpleNamespace(write_document=writer)
    return adapter


@pytest.mark.unit
@pytest.mark.parametrize(
    ("project_id", "identity_root", "identity_generation", "message"),
    [
        ("", "C:/repo", 1, "no structure_project_id"),
        ("project-id-1", "", 1, "no identity root"),
        ("project-id-1", "C:/repo", None, "no identity generation"),
    ],
)
def test_document_adapter_refuses_absent_claim_context(
    project_id, identity_root, identity_generation, message
):
    written: list[object] = []
    adapter = _bare_memory_adapter(lambda *args, **kwargs: written.append((args, kwargs)))

    with pytest.raises(ValueError, match=message):
        adapter.write_document(
            "C:/repo/doc.md", "content", project="proj",
            structure_path="C:/repo/doc.md", project_id=project_id,
            identity_generation=identity_generation, identity_root=identity_root,
            session_id="s", user_id="u",
        )

    assert written == []


@pytest.mark.unit
def test_document_adapter_releases_fence_when_writer_fails(monkeypatch):
    import menhir.infrastructure.project_identity_binding as binding
    import menhir.infrastructure.structure_write_fence as fence

    events: list[object] = []

    def _claim(**kwargs):
        events.append(("claim", kwargs))
        return SimpleNamespace(**kwargs)

    def _admit(neo4j, *, label, claim):
        events.append(("admit", label, claim))
        return "document-handle"

    def _release(neo4j, handle):
        events.append(("release", handle))

    def _write(*args, **kwargs):
        events.append(("write", kwargs))
        raise RuntimeError("writer failed")

    monkeypatch.setattr(binding, "binding_host", lambda: "host-1")
    monkeypatch.setattr(binding, "root_key_for", lambda root: f"key:{root}")
    monkeypatch.setattr(fence, "IdentityClaim", _claim)
    monkeypatch.setattr(fence, "admit_structure_writer", _admit)
    monkeypatch.setattr(fence, "release_structure_writer", _release)
    adapter = _bare_memory_adapter(_write)

    with pytest.raises(RuntimeError, match="writer failed"):
        adapter.write_document(
            "C:/repo/doc.md", "content", project="proj",
            structure_path="C:/repo/doc.md", project_id="project-id-1",
            identity_generation=9, identity_root="C:/repo",
            session_id="s", user_id="u", document_type="reference_article",
        )

    assert events[0] == ("claim", {
        "project_id": "project-id-1",
        "root_key": "key:C:/repo",
        "generation": 9,
        "host": "host-1",
    })
    assert events[1][0:2] == ("admit", "proj")
    assert events[2][0] == "write"
    assert events[2][1]["structure_project_id"] == "project-id-1"
    assert events[3] == ("release", "document-handle")
