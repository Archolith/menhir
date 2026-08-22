"""CF-127 -- one tenancy predicate, and a ratchet so the count of hand-written ones cannot grow.

CF-127 is a root cause, not a defect: nothing misbehaves because of it. What it explains is why
CF-104, CF-106, CF-63 and CF-126 are the same omission shipped by four different authors. Each had
to independently remember that scoping is needed, which of two keys applies to the label in hand,
and which of seven parameter spellings the surrounding file uses.

OWNER RULING 2026-08-21, and it shapes this file:

1. Structure entities **stay a single shared silo** keyed on `(structure_project, structure_path)`.
   Documented as a contract, not treated as a missed predicate.
2. **Builder plus a guard; convert nothing.** The 107 existing hand-written predicates are correct
   today and are left alone. Converting the tenancy boundary across 23 files is the risky half and
   buys nothing that is broken now.
3. The `''` -> `'default'` migration is untouched. Writes still spell the default `''`.

So the guard is a RATCHET, not a ban: it freezes the current per-file counts and fails when one
grows. New code has to use the builder; old code is undisturbed. A ratchet's whole value depends on
its detector actually detecting, so `test_the_detector_is_not_vacuous` is load-bearing here -- a
regex that silently stopped matching would turn this file into a permanent green light.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from menhir.domain.namespace import (
    DEFAULT_NAMESPACE,
    TenancyScheme,
    namespace_spellings,
    tenant_scope_cypher,
    tenant_scope_params,
)

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src/menhir"

#: A tenancy predicate written by hand: `<var>.namespace` or `<var>.group_id` compared to a
#: parameter, with or without a `coalesce(...)` wrapper.
HANDWRITTEN = re.compile(
    r"(?:coalesce\s*\(\s*)?\b\w+\.(?:namespace|group_id)\b[^\n]{0,80}?(?:=|\bIN\b|<>)\s*\$\w+"
)

#: Frozen 2026-08-21. Counts, not line numbers -- line numbers churn on every unrelated edit.
#: A file may DROP below its entry (that is progress); it may not exceed it, and a new file may
#: not appear. Lower a number here when you convert a site; never raise one.
BASELINE: dict[str, int] = {
    "explorer/extraction_lab.py": 1,
    "infrastructure/candidate_repository.py": 2,
    "infrastructure/consolidation_queries.py": 3,
    "infrastructure/episode_lifecycle.py": 9,
    "infrastructure/episode_maintenance.py": 3,
    "infrastructure/episode_stamping.py": 1,
    "infrastructure/memory_graph_adapter.py": 11,
    "infrastructure/memory_queries.py": 23,
    "infrastructure/scalar_view_repository.py": 10,
    "infrastructure/structure_queries.py": 2,
    "infrastructure/temporal_repository.py": 1,
    "infrastructure/todo_repository.py": 3,
    "infrastructure/turn_evidence_repository.py": 2,
    "infrastructure/typed_assertion_models.py": 2,
    "infrastructure/typed_assertion_reconciliation.py": 6,
    "infrastructure/typed_assertion_repair_repository.py": 2,
    "infrastructure/typed_assertion_write_repository.py": 6,
    "infrastructure/typed_event_repository.py": 3,
    "infrastructure/verifier_repository.py": 2,
    "infrastructure/view_query_repository.py": 5,
    "infrastructure/work_artifact_repository.py": 8,
    "services/lifecycle_conflicts.py": 1,
    "services/shadow_context_composition.py": 1,
}


def _census() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        n = len(HANDWRITTEN.findall(path.read_text(encoding="utf-8")))
        if n:
            found[path.relative_to(SRC).as_posix()] = n
    return found


# ---------------------------------------------------------------------------
# the ratchet
# ---------------------------------------------------------------------------


def test_the_detector_is_not_vacuous() -> None:
    """LOAD-BEARING. Every other assertion here is a comparison against this regex's output. If it
    stopped matching -- a Cypher restyle, a formatter, an over-eager edit to the pattern -- the
    ratchet would pass forever while hand-written predicates multiplied.

    Two independent checks: the total is in the right order of magnitude, and a specific known
    site is still seen."""
    census = _census()
    assert sum(census.values()) >= 90, f"detector found only {sum(census.values())}; expected ~107"

    known = (SRC / "infrastructure/memory_queries.py").read_text(encoding="utf-8")
    assert HANDWRITTEN.search(known), "the largest known offender no longer matches"


def test_no_file_gains_a_hand_written_tenancy_predicate() -> None:
    """THE GUARD. New code must go through `tenant_scope_cypher`; existing code is left alone.

    Failing here does not mean "revert your change" -- it means the query you added should build
    its tenancy predicate rather than spell one. If you genuinely converted a site and the count
    moved the other way, lower the number in BASELINE."""
    census = _census()

    grew = {f: (BASELINE[f], n) for f, n in census.items() if f in BASELINE and n > BASELINE[f]}
    assert not grew, (
        "hand-written tenancy predicates increased -- use tenant_scope_cypher() instead: "
        + ", ".join(f"{f} {was}->{now}" for f, (was, now) in sorted(grew.items()))
    )

    appeared = sorted(set(census) - set(BASELINE))
    assert not appeared, (
        "new file writes tenancy predicates by hand -- use tenant_scope_cypher(): "
        + ", ".join(appeared)
    )


def test_the_baseline_does_not_drift_stale() -> None:
    """A file that dropped to zero, or vanished, should be pruned from BASELINE. Not a failure of
    the codebase -- a failure to record progress, which is how a ratchet quietly loses its grip."""
    census = _census()
    stale = sorted(f for f in BASELINE if f not in census)
    assert not stale, f"BASELINE lists files with no hand-written predicates left; prune: {stale}"


# ---------------------------------------------------------------------------
# the builder: the two properties that make it worth converting to
# ---------------------------------------------------------------------------


def test_the_fragment_is_a_no_op_when_unscoped() -> None:
    """The reason the predicate is always included rather than conditionally appended. "Forgot the
    predicate" is the failure mode CF-104/106/63/126 all are, so the predicate must not be the
    thing you can forget."""
    assert tenant_scope_params(None) == {"tenant_namespaces": None}
    assert "IS NULL OR" in tenant_scope_cypher("n")


def test_it_matches_both_persisted_spellings_of_the_default_silo() -> None:
    """THE BUG IN THE EXISTING IDIOM, pinned so the builder cannot regress into it.

    `coalesce(n.namespace, n.group_id, 'default')` (correlation_queries.py:147) returns `''` for a
    legacy row with no `namespace` and `group_id = ''`, because `''` is not NULL and coalesce never
    reaches its default. Compared against `'default'` that is FALSE -- it misses every such row,
    and there are 33,442 of them on this deployment."""
    spellings = tenant_scope_params(DEFAULT_NAMESPACE)["tenant_namespaces"]
    assert spellings is not None
    assert "" in spellings and DEFAULT_NAMESPACE in spellings

    # The predicate coalesces to '' (not 'default') precisely so the legacy row's '' is a member
    # of the list rather than being compared against a value it can never equal.
    assert "coalesce(n.namespace, n.group_id, '')" in tenant_scope_cypher("n")


def test_a_named_silo_does_not_widen_to_the_default_bucket() -> None:
    """POSITIVE CONTROL. Accepting both default spellings must not mean accepting the default
    alongside every named silo -- that would turn a tenancy filter into a leak."""
    assert namespace_spellings("tenant-a") == ["tenant-a"]
    assert tenant_scope_params("tenant-a") == {"tenant_namespaces": ["tenant-a"]}


def test_there_is_exactly_one_parameter_name() -> None:
    """CF-127 counted seven spellings in use. A builder that let callers choose the parameter name
    would have made it eight."""
    assert tenant_scope_cypher("n").count("$tenant_namespaces") == 2
    assert set(tenant_scope_params("x")) == {"tenant_namespaces"}


# ---------------------------------------------------------------------------
# the structure contract -- owner ruling, with teeth
# ---------------------------------------------------------------------------


def test_the_structure_scheme_refuses_instead_of_returning_a_fragment() -> None:
    """Structure entities wear the same `:Entity` label but are a single shared silo by ruling.

    Returning a namespace predicate for them would silently match nothing. Returning `""` would
    read as "no scoping needed here" at the call site. Raising is the only option that puts the
    contract in front of the author at the moment they assume otherwise."""
    with pytest.raises(ValueError, match="single shared silo"):
        tenant_scope_cypher("n", scheme=TenancyScheme.STRUCTURE)


def test_the_scheme_enum_is_a_strenum() -> None:
    """Trap T20. `class X(str, Enum)` formats as `"TenancyScheme.MEMORY"` under str()/f-strings,
    and this value's neighbourhood is Cypher fragment interpolation."""
    assert f"{TenancyScheme.MEMORY}" == "memory"
    assert str(TenancyScheme.STRUCTURE) == "structure"


def test_the_variable_must_be_an_identifier() -> None:
    """The fragment interpolates `variable` directly. Call sites pass their own Cypher variable
    names, never user input, but an f-string reaching a query deserves the guard anyway."""
    for bad in ("n) OR (1=1", "", "n.namespace"):
        with pytest.raises(ValueError):
            tenant_scope_cypher(bad)


# ---------------------------------------------------------------------------
# the same claim, proven in Cypher rather than in Python
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_the_predicate_matches_every_default_silo_shape_on_a_real_graph(test_neo4j_repo) -> None:
    """THE CLAIM ABOVE, EXECUTED. `test_it_matches_both_persisted_spellings_of_the_default_silo`
    reasons about `coalesce` in Python; Cypher's `coalesce` is a different implementation and the
    whole finding is that reasoning about tenancy without checking is how these bugs ship.

    Three shapes all mean "default silo" on this deployment:
      legacy_empty_gid  group_id='', no namespace   <- 33,442 rows, the one the old idiom misses
      stamped_default   group_id='', namespace='default'
      no_props          neither property            <- pre-namespacing rows

    Also runs the OLD idiom side by side, so the difference is demonstrated rather than asserted.
    """
    repo = test_neo4j_repo
    frag = tenant_scope_cypher("n")
    repo.execute("MATCH (n:CF127Probe) DETACH DELETE n", {})
    repo.execute(
        """
        CREATE (:CF127Probe {tag:'legacy_empty_gid', group_id:''})
        CREATE (:CF127Probe {tag:'stamped_default',  group_id:'', namespace:'default'})
        CREATE (:CF127Probe {tag:'named_tenant',     group_id:'tenant-a', namespace:'tenant-a'})
        CREATE (:CF127Probe {tag:'no_props'})
        """,
        {},
    )
    try:
        def hits(namespace):
            rows = repo.execute(
                f"MATCH (n:CF127Probe) WHERE {frag} RETURN n.tag AS tag ORDER BY tag",
                tenant_scope_params(namespace),
            )
            return [r["tag"] for r in rows]

        assert hits(DEFAULT_NAMESPACE) == ["legacy_empty_gid", "no_props", "stamped_default"]
        assert hits("tenant-a") == ["named_tenant"], "a named silo must not see the default bucket"
        assert len(hits(None)) == 4, "unscoped must filter nothing"

        old = repo.execute(
            "MATCH (n:CF127Probe) WHERE coalesce(n.namespace, n.group_id, 'default') = $namespace "
            "RETURN n.tag AS tag ORDER BY tag",
            {"namespace": DEFAULT_NAMESPACE},
        )
        assert [r["tag"] for r in old] == ["no_props", "stamped_default"], (
            "the old idiom is expected to MISS legacy_empty_gid; if it no longer does, this "
            "finding's premise changed and the docstrings that cite it need revisiting"
        )
    finally:
        repo.execute("MATCH (n:CF127Probe) DETACH DELETE n", {})
