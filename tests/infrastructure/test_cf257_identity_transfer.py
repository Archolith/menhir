"""CF-257 -- identity TRANSFER: one active binding per directory, per host.

The property under test, stated once:

    For any host H and normalized root R, at most one :ProjectIdentity may be `bound` with
    (bound_host=H, root_key=R) -- and changing which identity that is requires operator tier.

Three earlier versions failed it in three different ways, and each is pinned below:

* `new` minted a fresh id without retiring the binding that already claimed the directory, so
  the root ended with two active owners and a later lookup resolved to whichever came back first;
* `adopt` retired every binding naming that path REGARDLESS OF HOST, so transferring `/srv/app`
  here superseded another machine's `/srv/app`;
* the tier check was `tier and tier != OPERATOR`, so an unbound tier -- the value
  `get_request_tier()` returns whenever auth is not configured -- transferred freely.

The constraint, not the Python check in front of it, is the enforcement. See
`test_cf257_identity_binding_online.py` for that half; here the fake enforces the same rule so
these tests cannot pass while the property is absent.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from menhir.infrastructure.project_identity_binding import (
    IdentityRootContested,
    bind_project_identity,
    binding_for_root,
    root_key_for,
)


def _bind(graph, pid, root, *, host, rebind=False, monkeypatch=None):
    """Bind as if running on *host*. The host is a process fact, so it is patched, not passed."""
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: host
    )
    return bind_project_identity(graph, project_id=pid, root_path=root, rebind=rebind)


def _active(graph, *, host, root):
    key = root_key_for(root)
    return sorted(
        pid
        for pid, node in graph.nodes.items()
        if node.get("state", "bound") == "bound"
        and node.get("bound_host") == host
        and node.get("root_key") == key
    )


@pytest.mark.unit
def test_root_keys_respect_the_spelled_path_flavor():
    assert root_key_for("/srv/Foo") != root_key_for("/srv/foo")
    assert root_key_for("/srv/Foo/") == root_key_for("/srv/Foo")
    assert root_key_for(r"/srv/a\b") != root_key_for("/srv/a/b")
    assert root_key_for("") == ""
    assert root_key_for("/") == "/"
    assert root_key_for(r"C:\srv\App\\") == root_key_for("c:/srv/app")
    assert root_key_for(r"\\Server\Share\App\\") == root_key_for("//server/share/app")


# ---------------------------------------------------------------------------
# One active binding per (host, root)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_new_leaves_exactly_one_active_binding_for_this_host_and_root(
    fake_identity_graph, monkeypatch
):
    """`new` is a TRANSFER. Minting without retiring left two ids owning one directory."""
    g = fake_identity_graph
    _bind(g, "old-id", "/srv/app", host="h1", monkeypatch=monkeypatch)
    _bind(g, "fresh-id", "/srv/app", host="h1", rebind=True, monkeypatch=monkeypatch)

    assert _active(g, host="h1", root="/srv/app") == ["fresh-id"]
    assert g.nodes["old-id"]["state"] == "superseded"
    assert g.nodes["old-id"]["superseded_by"] == "fresh-id"


@pytest.mark.unit
def test_adopt_leaves_exactly_one_active_local_binding(fake_identity_graph, monkeypatch):
    g = fake_identity_graph
    _bind(g, "incumbent", "/srv/app", host="h1", monkeypatch=monkeypatch)
    _bind(g, "adopted", "/elsewhere/app", host="h1", monkeypatch=monkeypatch)
    _bind(g, "adopted", "/srv/app", host="h1", rebind=True, monkeypatch=monkeypatch)

    assert _active(g, host="h1", root="/srv/app") == ["adopted"]
    assert _active(g, host="h1", root="/elsewhere/app") == []


@pytest.mark.unit
def test_the_same_path_on_another_host_is_untouched(fake_identity_graph, monkeypatch):
    """Two machines can carry the same folder layout -- that is the whole reason identity is a
    minted id rather than a path. Retiring by path alone made a local transfer reach across."""
    g = fake_identity_graph
    _bind(g, "here", "/srv/app", host="h1", monkeypatch=monkeypatch)
    _bind(g, "there", "/srv/app", host="h2", monkeypatch=monkeypatch)

    _bind(g, "new-here", "/srv/app", host="h1", rebind=True, monkeypatch=monkeypatch)

    assert _active(g, host="h1", root="/srv/app") == ["new-here"]
    assert _active(g, host="h2", root="/srv/app") == ["there"], "another host was superseded"
    assert g.nodes["there"]["state"] == "bound"


@pytest.mark.unit
def test_a_second_identity_cannot_claim_a_bound_directory_without_a_transfer(
    fake_identity_graph, monkeypatch
):
    """The non-transfer path must REFUSE, not silently become the second owner."""
    g = fake_identity_graph
    _bind(g, "incumbent", "/srv/app", host="h1", monkeypatch=monkeypatch)
    with pytest.raises(IdentityRootContested, match="incumbent"):
        _bind(g, "interloper", "/srv/app", host="h1", monkeypatch=monkeypatch)

    assert _active(g, host="h1", root="/srv/app") == ["incumbent"], "incumbent was disturbed"


@pytest.mark.unit
def test_the_incumbent_is_not_poisoned_by_a_rejected_claim(fake_identity_graph, monkeypatch):
    """One id in two directories poisons BOTH (the silo is ambiguous). Two ids on one directory
    poisons NEITHER -- only the newcomer is wrong, and disabling the incumbent would break a
    working project on the strength of a stray identity file."""
    g = fake_identity_graph
    _bind(g, "incumbent", "/srv/app", host="h1", monkeypatch=monkeypatch)
    with pytest.raises(IdentityRootContested):
        _bind(g, "interloper", "/srv/app", host="h1", monkeypatch=monkeypatch)

    assert g.nodes["incumbent"]["state"] == "bound"
    assert _bind(g, "incumbent", "/srv/app", host="h1", monkeypatch=monkeypatch).state == "bound"


@pytest.mark.unit
def test_a_binding_with_no_bound_host_never_matches(fake_identity_graph, monkeypatch):
    """`coalesce(p.bound_host, $host) = $host` made every unstamped binding match EVERY host, so
    the host scoping was inert on all 60 production rows. Strict equality is the fix; a row that
    carries no host is not a row about this host."""
    g = fake_identity_graph
    g.nodes["legacy"] = {
        "canonical_root_path": "/srv/app",
        "state": "bound",
        "bound_host": None,
        "root_key": None,
    }
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )
    assert binding_for_root(g, "/srv/app") is None


@pytest.mark.unit
def test_an_unstamped_rival_on_this_host_is_still_detected(fake_identity_graph, monkeypatch):
    """A binding written before the root constraint carries no `root_key`, so the CONSTRAINT
    cannot see it. The Python rival check matches on the recorded path as well, which is what
    covers the window between deploying the constraint and backfilling under it."""
    g = fake_identity_graph
    g.nodes["legacy"] = {
        "canonical_root_path": r"C:\srv\App",
        "state": "bound",
        "bound_host": "h1",
        "root_key": None,
    }
    with pytest.raises(IdentityRootContested, match="legacy"):
        _bind(g, "newcomer", "C:/srv/app", host="h1", monkeypatch=monkeypatch)


@pytest.mark.unit
def test_unstamped_posix_roots_preserve_case(fake_identity_graph, monkeypatch):
    """Legacy canonical-root fallbacks obey the same case rules as newly stamped root keys."""
    g = fake_identity_graph
    g.nodes["legacy"] = {
        "canonical_root_path": "/srv/Foo",
        "state": "bound",
        "bound_host": "h1",
        "root_key": None,
    }
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )

    assert binding_for_root(g, "/srv/Foo/") == "legacy"
    assert binding_for_root(g, "/srv/foo") is None
    _bind(g, "lowercase", "/srv/foo", host="h1", monkeypatch=monkeypatch)
    assert _active(g, host="h1", root="/srv/foo") == ["lowercase"]


@pytest.mark.unit
def test_binding_stamps_a_legacy_row_so_the_constraint_can_see_it(
    fake_identity_graph, monkeypatch
):
    g = fake_identity_graph
    g.nodes["legacy"] = {
        "canonical_root_path": "/srv/app",
        "state": "bound",
        "bound_host": None,
        "root_key": None,
    }
    _bind(g, "legacy", "/srv/app", host="h1", monkeypatch=monkeypatch)
    assert g.nodes["legacy"]["bound_host"] == "h1"
    assert g.nodes["legacy"]["root_key"] == root_key_for("/srv/app")


@pytest.mark.unit
def test_a_superseded_identity_cannot_quietly_resume_writing(fake_identity_graph, monkeypatch):
    """After a transfer the old checkout may still hold the old identity file. Resolving it back
    to `bound` would resurrect the abandoned silo; it is refused so the operator sees it."""
    g = fake_identity_graph
    _bind(g, "old-id", "/srv/app", host="h1", monkeypatch=monkeypatch)
    _bind(g, "new-id", "/srv/app", host="h1", rebind=True, monkeypatch=monkeypatch)

    from menhir.infrastructure.project_identity_binding import IdentityBindingConflict

    with pytest.raises(IdentityBindingConflict, match="SUPERSEDED"):
        _bind(g, "old-id", "/srv/app", host="h1", monkeypatch=monkeypatch)


# ---------------------------------------------------------------------------
# The writer census
# ---------------------------------------------------------------------------

#: Every place in `src/` that reaches the graph adapter's structure writer, as
#: ``<module>::<enclosing function>``. Each one must settle or validate an identity before
#: writing; the choke point refuses an id-less scan, so an unlisted writer is a scan that either
#: fails at runtime or -- if it settles wrongly -- writes into the wrong silo.
#:
#: This list is a REVIEW GATE, not documentation. Adding a writer without deciding how it obtains
#: an identity should fail here, which is what happened when `_background_symbol_rescan` produced
#: its own scan and carried no id at all.
EXPECTED_STRUCTURE_WRITERS = {
    # settles identity before scanning; re-checks inside the detached write
    "menhir/core/backend_runtime_data_ops.py::_do_write",
    # DEPRECATED bridge: validates a caller-supplied id against the authoritative binding
    "menhir/core/backend_runtime_data_ops.py::write_project_structure",
    # produces its own scan, so it settles its own identity (no action: it cannot hold authority)
    "menhir/core/backend_runtime_data_ops.py::_background_symbol_rescan",
    # the unattended watcher; settles and reports rather than writing id-less nodes
    "menhir/services/scheduler_tasks.py::refresh_structure_graphs",
}
# Deliberately NOT listed: the choke point's own definition
# (`memory_graph_adapter.write_project_structure`) and the protocol/client declarations that
# describe it. Those are the rule, not paths subject to it -- and the walk below finds attribute
# REFERENCES, so a `def` of the same name is not a writer.

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "menhir"


def _enclosing_function(tree: ast.AST):
    """Return a lookup from line number to the INNERMOST enclosing function name.

    Innermost matters: `_do_write` is a closure inside `scan_and_write_project`, and attributing
    its write to the outer function would let a new nested writer hide behind an approved name.
    """
    funcs = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def name_for(line: int) -> str:
        best = None
        for fn in funcs:
            if fn.lineno <= line <= (fn.end_lineno or fn.lineno):
                if best is None or fn.lineno > best.lineno:
                    best = fn
        return best.name if best else "<module>"

    return name_for


@pytest.mark.unit
def test_every_structure_writer_is_accounted_for():
    found = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name_for = _enclosing_function(tree)
        for node in ast.walk(tree):
            # An ATTRIBUTE, not a Call: `asyncio.to_thread(adapter.write_project_structure, ...)`
            # passes the writer as a value, and matching only calls missed three of four sites.
            if isinstance(node, ast.Attribute) and node.attr == "write_project_structure":
                rel = path.relative_to(_SRC.parent).as_posix()
                found.add(f"{rel}::{name_for(node.lineno)}")

    new = found - EXPECTED_STRUCTURE_WRITERS
    assert not new, (
        "New structure writer(s) with no recorded identity decision: "
        + ", ".join(sorted(new))
        + ". Decide how each obtains a project_id (settle, or validate against the binding), "
        "then add it to EXPECTED_STRUCTURE_WRITERS."
    )
    gone = EXPECTED_STRUCTURE_WRITERS - found
    assert not gone, f"Recorded writer(s) no longer present -- prune the list: {sorted(gone)}"


# ---------------------------------------------------------------------------
# The graph is written before the file, and a refusal costs the checkout nothing
# ---------------------------------------------------------------------------

def _settle(tmp_path, graph, **kw):
    from menhir.services.project_identity_service import settle_project_identity
    from types import SimpleNamespace

    return settle_project_identity(
        SimpleNamespace(neo4j=graph), root_path=str(tmp_path), display_name="proj", **kw
    )


@pytest.mark.unit
def test_a_refused_binding_leaves_the_existing_identity_file_intact(
    fake_identity_graph, monkeypatch, tmp_path
):
    """Publishing first meant a REFUSED transfer had already unlinked the old file, destroying
    the only local record of the id whose silo the project owns. The graph is written first, so a
    refusal leaves the checkout exactly as it was."""
    from menhir.domain.project_id_file import ensure_ignore_rule, mint_identity, read_identity

    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )
    ensure_ignore_rule(tmp_path)
    original = mint_identity(tmp_path, project_id="original-id", display_name="proj").project_id

    g = fake_identity_graph
    # Another identity already owns this directory, so the transfer is refused.
    g.nodes["incumbent"] = {
        "canonical_root_path": str(tmp_path),
        "state": "bound",
        "bound_host": "h1",
        "root_key": root_key_for(str(tmp_path)),
    }

    with pytest.raises(IdentityRootContested):
        _settle(tmp_path, g)

    assert read_identity(tmp_path).project_id == original, "the file was replaced before the bind"


@pytest.mark.unit
def test_a_lost_identity_file_is_republished_from_the_binding(
    fake_identity_graph, monkeypatch, tmp_path
):
    """The recovery path for a publication that failed after the binding committed: the graph is
    authoritative for (host, root), so the next scan re-publishes rather than needing a decision."""
    from menhir.domain.project_id_file import read_identity

    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )
    g = fake_identity_graph
    g.nodes["bound-id"] = {
        "canonical_root_path": str(tmp_path),
        "state": "bound",
        "bound_host": "h1",
        "root_key": root_key_for(str(tmp_path)),
    }

    claim, resolution = _settle(tmp_path, g)

    assert claim.project_id == "bound-id"
    assert claim.root_key == root_key_for(str(tmp_path))
    assert resolution.resolved
    assert read_identity(tmp_path).project_id == "bound-id"


@pytest.mark.unit
def test_settling_with_action_new_supersedes_the_binding_that_held_the_directory(
    fake_identity_graph, monkeypatch, tmp_path
):
    """The WIRING, not the binding. `test_new_leaves_exactly_one_active_binding...` above calls
    `bind_project_identity(rebind=True)` directly, so it proves the binding transfers when asked
    -- it says nothing about whether `identity_action='new'` asks. Reverting the service to
    `transferring = action == ADOPT` passes every binding-level test and still leaves two active
    owners here, which is how the original defect survived its own test.
    """
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )
    g = fake_identity_graph
    g.nodes["previous"] = {
        "canonical_root_path": str(tmp_path),
        "state": "bound",
        "bound_host": "h1",
        "root_key": root_key_for(str(tmp_path)),
    }

    claim, _ = _settle(tmp_path, g, identity_action="new")
    minted = claim.project_id

    assert minted and minted != "previous"
    assert _active(g, host="h1", root=str(tmp_path)) == [minted]
    assert g.nodes["previous"]["state"] == "superseded"


@pytest.mark.unit
def test_settling_with_action_adopt_supersedes_the_binding_that_held_the_directory(
    fake_identity_graph, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: "h1"
    )
    g = fake_identity_graph
    g.nodes["previous"] = {
        "canonical_root_path": str(tmp_path),
        "state": "bound",
        "bound_host": "h1",
        "root_key": root_key_for(str(tmp_path)),
    }
    g.nodes["adopted"] = {
        "canonical_root_path": "/somewhere/else",
        "state": "bound",
        "bound_host": "h1",
        "root_key": root_key_for("/somewhere/else"),
    }

    claim, _ = _settle(tmp_path, g, identity_action="adopt", adopt_project_id="adopted")

    assert claim.project_id == "adopted"
    assert _active(g, host="h1", root=str(tmp_path)) == ["adopted"]
    assert g.nodes["previous"]["state"] == "superseded"
