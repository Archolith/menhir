"""CF-230's shape, as a standing check: a namespace-accepting mutation called without one.

**The recurring failure this guards.** Four times now the callee has been correct and the CALLER
has composed it wrongly, with helper-level tests green throughout:

* CF-79   -- the budget raised correctly; the emitter swallowed the raise.
* CF-227  -- the control signal was right; exception isolation discarded it.
* CF-229  -- in-process pairing existed; nothing tested the path production runs.
* CF-230  -- `link_episode_admission` filtered correctly; the caller passed `None`, which
             means "do not filter".

CF-230 is the sharpest because the guard was not merely bypassed -- it was **handed the one
argument value that disables it**. `namespace=None` is the opt-in isolation contract working as
designed, and a caller that omits the argument gets unscoped behaviour with no error anywhere.

**So this is an AST census, not a behavioural test.** It cannot prove any particular call is
safe; what it does is refuse to let a NEW omission appear unnoticed. Every entry in the allowlist
below was checked by hand and carries the reason it is safe, so adding a call site means either
passing the namespace or writing down why you did not.

**All 11 current sites are defended, at a different layer than the argument** -- which is exactly
why an AST sweep alone would have been misleading, and why the allowlist records the mechanism
rather than just the location.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.unit]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "menhir"

#: Mutations that accept an opt-in `namespace`. Absent/None means "do not filter", so a caller
#: that omits it silently opts out of isolation.
NAMESPACE_MUTATORS = frozenset({
    "flag_memory", "unflag_memory", "promote_memory", "delete_memory", "erase_memory",
    "link_episode_admission", "create_evidence_projection", "relocate_artifact_source",
    "supersede_artifact", "link_artifacts", "transition_artifact_status", "close_todo",
    "delete_namespace",
})

#: (file suffix, callee) -> why omitting the namespace is correct HERE.
#:
#: Each was verified by reading the call site, not inferred from its shape. "The tool guards
#: first" is a real answer only where a CF-64 two-lookup ownership check runs on the same uuid --
#: that check already established ownership, so re-passing the namespace would be redundant, and
#: the refusal is what makes the unscoped call safe rather than the argument.
#:
#: The guard has two spellings: the factored `mcp.ownership.foreign_object_refusal`, and the
#: older inline form in the ingest tools (scoped fetch, fallback fetch, "exists but is outside
#: namespace"). They are the same guarantee, and a check that recognised only one reported a
#: guarded tool as unguarded -- which this file did on its first run.
ALLOWED: dict[tuple[str, str], str] = {
    ("mcp/tools/ingest/delete_memory.py", "erase_memory"):
        "CF-64 two-lookup ownership guard on node_uuid runs first (inline spelling)",
    ("mcp/tools/ingest/flag_memory.py", "flag_memory"):
        "CF-64 two-lookup ownership guard on node_uuid runs first",
    ("mcp/tools/ingest/promote_memory.py", "promote_memory"):
        "CF-64 two-lookup ownership guard on node_uuid runs first",
    ("mcp/tools/ingest/unflag_memory.py", "unflag_memory"):
        "CF-64 two-lookup ownership guard on node_uuid runs first",
    ("mcp/tools/ops/close_todo.py", "close_todo"):
        "foreign_object_refusal ownership guard on the todo uuid runs first",
    ("mcp/tools/ops/link_artifacts.py", "link_artifacts"):
        "foreign_object_refusal ownership guard on BOTH uuids runs first",
    ("mcp/tools/ops/relocate_artifact_source.py", "relocate_artifact_source"):
        "foreign_object_refusal ownership guard on artifact_uuid runs first",
    ("mcp/tools/ops/supersede_artifact.py", "supersede_artifact"):
        "foreign_object_refusal ownership guard on BOTH uuids runs first",
    ("mcp/tools/ops/delete_namespace.py", "delete_namespace"):
        "the namespace IS the target; the backend refuses a pinned caller naming another silo",
    ("api/routes.py", "delete_namespace"):
        "same: target, not filter -- guarded in backend_runtime_data_ops.delete_namespace",
    ("api/routes_handlers.py", "delete_namespace"):
        "same: phase3_reset target, guarded in the backend",
}


def _offending_call_sites() -> list[tuple[str, int, str, str]]:
    """Every mutation call that omits `namespace` while the caller HAS one in scope.

    The "in scope" condition is what makes this a signal rather than noise: a function with no
    namespace available cannot pass one, and 140 such sites exist. CF-230's shape is specifically
    a caller that HAD the value and did not forward it.
    """
    found: list[tuple[str, int, str, str]] = []
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            in_scope = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            in_scope |= {
                t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)
            }
            if not ({"namespace", "ns"} & in_scope):
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                    continue
                if call.func.attr not in NAMESPACE_MUTATORS:
                    continue
                kwargs = {k.arg for k in call.keywords if k.arg}
                if "namespace" in kwargs:
                    continue
                if any(k.arg is None for k in call.keywords):
                    continue  # **kwargs passthrough: the namespace may be inside it
                rel = path.relative_to(SRC).as_posix()
                found.append((rel, call.lineno, fn.name, call.func.attr))
    return found


def test_no_unreviewed_mutation_omits_its_namespace() -> None:
    """A new call site must either pass the namespace or be added to ALLOWED with a reason.

    Failure here is not automatically a bug -- it is an unreviewed composition. The question to
    answer is the CF-230 question: does something OTHER than this argument establish that the
    caller owns the object it is about to mutate?
    """
    offenders = _offending_call_sites()
    unreviewed = [
        (f, ln, caller, callee)
        for f, ln, caller, callee in offenders
        if (f, callee) not in ALLOWED
    ]
    assert not unreviewed, (
        "namespace-accepting mutations called without a namespace, and not reviewed:\n"
        + "\n".join(
            f"  {f}:{ln}  {caller}() -> {callee}()  "
            f"-- pass namespace=, or add ({f!r}, {callee!r}) to ALLOWED with a reason"
            for f, ln, caller, callee in unreviewed
        )
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """An allowlist that outlives its call sites stops describing the code.

    Kept strict deliberately: a stale entry is how an exemption written for one call silently
    covers a different one added later at the same location.
    """
    live = {(f, callee) for f, _ln, _caller, callee in _offending_call_sites()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, f"ALLOWED entries with no matching call site: {stale}"


def test_every_allowlisted_tool_actually_guards_before_the_call() -> None:
    """The allowlist claims 'the tool refuses first'. This checks the claim rather than trusting
    the comment -- the exact failure mode that produced ten confirmed comment-vs-code findings in
    this codebase.
    """
    #: CF-64's two-lookup guard exists in TWO spellings, and checking for only one is a false
    #: negative dressed as rigour. `mcp.ownership.foreign_object_refusal` is the factored form;
    #: the older ingest tools still carry it inline as a scoped fetch, a fallback fetch, and a
    #: "Refused: ... outside namespace" message. This test originally looked for the helper name
    #: alone and failed on `delete_memory`, which has the guard and not the import.
    GUARD_MARKERS = ("foreign_object_refusal", "exists but is outside namespace")

    for (rel, _callee), reason in ALLOWED.items():
        if "foreign_object_refusal" not in reason:
            continue
        source = (SRC / rel).read_text(encoding="utf-8")
        assert any(m in source for m in GUARD_MARKERS), (
            f"{rel} is allowlisted because it guards ownership before the call, but neither "
            f"spelling of the guard is present any more"
        )


def test_the_census_can_find_a_planted_omission() -> None:
    """Vacuity guard. An AST sweep that silently matched nothing would pass every assertion above
    for any codebase at all, including one full of the defect."""
    planted = ast.parse(
        "def handler(self, uuid, namespace=''):\n"
        "    self.graph_adapter.flag_memory(uuid)\n"
    )
    fn = planted.body[0]
    in_scope = {a.arg for a in fn.args.args}
    assert "namespace" in in_scope
    call = next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    )
    assert call.func.attr in NAMESPACE_MUTATORS
    assert "namespace" not in {k.arg for k in call.keywords if k.arg}, (
        "the detector's own matching logic no longer recognises the CF-230 shape"
    )
