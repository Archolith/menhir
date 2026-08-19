"""Counterexample tests for HIGH wave 8 (CF-47, CF-48, CF-55, CF-107).

Each test reproduces the scenario the register recorded, not the shape of the fix.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"

#: The format `toString(n.valid_at)` actually emits, which is what recall projects.
_SUFFIXED = "2026-07-21T19:13:04.572Z[UTC]"
_PIVOT = "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# CF-55 -- the bitemporal filters fail OPEN on the one format production emits
# ---------------------------------------------------------------------------


def test_cf55_a_fact_valid_only_in_the_future_is_not_valid_at_an_earlier_pivot() -> None:
    """An unparseable bound is skipped rather than raised (`if va is not None and va > pivot`),
    so a parser that returns None for the production format made every bound stop constraining
    and every filter return True. Fail-open by construction."""
    from menhir.domain.temporal import FactTemporal, is_valid_at

    assert is_valid_at(FactTemporal(valid_at=_SUFFIXED), _PIVOT) is False


def test_cf55_a_fact_learned_later_was_not_known_at_an_earlier_pivot() -> None:
    from menhir.domain.temporal import FactTemporal, was_known_at

    assert was_known_at(FactTemporal(created_at=_SUFFIXED), _PIVOT) is False


def test_cf55_an_anachronism_is_classified_as_such() -> None:
    from menhir.domain.temporal import FactTemporal, TemporalRole, temporal_role

    role = temporal_role(FactTemporal(created_at=_SUFFIXED), as_of=_PIVOT)
    assert role is TemporalRole.NOT_YET_KNOWN


def test_cf55_the_as_known_at_lens_filters_on_the_production_format() -> None:
    """`matches_query` reaches the defect indirectly through the two filters above, and it is the
    one with a production caller (shadow context composition, where reference_time IS passed)."""
    from menhir.domain.temporal import FactTemporal, TemporalQuery, matches_query

    fact = FactTemporal(created_at=_SUFFIXED)
    assert matches_query(fact, TemporalQuery.AS_KNOWN_AT, as_of=_PIVOT) is False


def test_cf55_the_unsuffixed_control_still_behaves_identically() -> None:
    """The suffix is the variable. Same instants without it must give the same answers, or the
    fix changed more than the parse."""
    from menhir.domain.temporal import FactTemporal, is_valid_at, was_known_at

    plain = "2026-07-21T19:13:04.572Z"
    assert is_valid_at(FactTemporal(valid_at=plain), _PIVOT) is False
    assert was_known_at(FactTemporal(created_at=plain), _PIVOT) is False


def test_cf55_a_naive_bound_does_not_raise_against_an_aware_pivot() -> None:
    """The second defect that rode along: the old parser returned NAIVE datetimes for unsuffixed
    input, and comparing naive to aware raises TypeError. `parse_iso8601` treats naive as UTC."""
    from menhir.domain.temporal import FactTemporal, is_valid_at

    assert is_valid_at(FactTemporal(valid_at="2026-07-21T19:13:04"), _PIVOT) is False


def test_cf55_the_filters_use_the_canonical_parser() -> None:
    """The invariant the canonical parser's own docstring states -- "Anything parsing a Neo4j
    timestamp MUST use this" -- with the violating copy ten lines below it."""
    from menhir.domain import temporal

    assert temporal._parse is temporal.parse_iso8601


# ---------------------------------------------------------------------------
# CF-47 -- two authorities for merge eligibility, normalizing differently
# ---------------------------------------------------------------------------


def _signals(**overrides):
    from menhir.domain.merge_eligibility import NodeSignals

    base = dict(
        uuid="a", exists=True, ineligible_role=False, namespace="default",
        freshness=None, scope=None, user_flagged=False, conflict_status=None,
    )
    base.update(overrides)
    return NodeSignals(**base)


def test_cf47_the_mutation_predicate_is_emitted_from_the_domain_constants() -> None:
    """The fix is not "make the two copies match today" -- it is that there is one copy. Editing
    the domain's sets now changes the mutation, which is the only version that stays fixed."""
    from menhir.domain import merge_eligibility as me

    fragment = me.mutable_eligibility_cypher()
    assert "$mergeable_freshness" in fragment
    assert "$protected_conflict_states" in fragment
    assert set(me.MUTABLE_PREDICATE_PARAMS) == {
        "mergeable_freshness",
        "protected_conflict_states",
    }
    assert me.MUTABLE_PREDICATE_PARAMS["mergeable_freshness"] == sorted(me._MERGEABLE_FRESHNESS)


def test_cf47_the_repository_no_longer_hand_writes_the_predicates() -> None:
    """Every literal the old block spelled out is gone from the repository."""
    source = (_SRC / "infrastructure/correlation_queries.py").read_text(encoding="utf-8")
    assert "coalesce(survivor.freshness, 'ACTIVE') IN ['ACTIVE']" not in source
    assert "coalesce(survivor.scope, '') <> 'PROMOTED'" not in source
    assert "mutable_eligibility_cypher()" in source
    assert "MUTABLE_PREDICATE_PARAMS" in source


def test_cf47_the_emitted_predicate_binds_every_parameter_it_names() -> None:
    """A fragment naming a parameter the caller does not supply is a runtime Cypher error, and
    it would only appear on the merge path."""
    import re

    from menhir.domain import merge_eligibility as me

    named = set(re.findall(r"\$([a-z_]+)", me.mutable_eligibility_cypher()))
    assert named <= set(me.MUTABLE_PREDICATE_PARAMS)


@pytest.mark.parametrize("scope", ["PROMOTED", "promoted", " Promoted "])
def test_cf47_promoted_scope_is_case_insensitive_on_both_sides(scope: str) -> None:
    """The divergence ran both ways. Here the Cypher was the LOOSER side: it matched
    `<> 'PROMOTED'` exactly, so a lowercase `promoted` would have been merged. A PROMOTED node is
    operator-curated and merge-immune by policy; case is not consent."""
    from menhir.domain.merge_eligibility import PROMOTED_SCOPE, evaluate, mutable_eligibility_cypher

    verdict = evaluate(_signals(uuid="a", scope=scope), _signals(uuid="b"))
    assert verdict.allowed is False
    assert verdict.reason_code == PROMOTED_SCOPE
    assert "toUpper(trim(coalesce(survivor.scope, ''))) <> 'PROMOTED'" in mutable_eligibility_cypher()


def test_cf47_conflict_state_normalizes_on_both_sides() -> None:
    from menhir.domain.merge_eligibility import (
        PROTECTED_CONFLICT,
        evaluate,
        mutable_eligibility_cypher,
    )

    verdict = evaluate(_signals(uuid="a", conflict_status="  Unresolved "), _signals(uuid="b"))
    assert verdict.reason_code == PROTECTED_CONFLICT
    assert "toLower(trim(coalesce(survivor.conflict_status" in mutable_eligibility_cypher()


def test_cf47_freshness_is_an_allowlist_so_a_new_state_fails_closed() -> None:
    """Merging DETACH-DELETEs the absorbed node, so an unrecognised state must refuse rather
    than fall through. Stated as "is it ACTIVE or unstamped" rather than "is it one of the two
    bad ones", which is what a fourth FreshnessState value would otherwise walk straight past."""
    from menhir.domain.merge_eligibility import NON_ACTIVE_FRESHNESS, evaluate

    for freshness in ("COMPRESSED", "GONE", "STALE", "ARCHIVED"):
        verdict = evaluate(_signals(uuid="a", freshness=freshness), _signals(uuid="b"))
        assert verdict.allowed is False, freshness
        assert verdict.reason_code == NON_ACTIVE_FRESHNESS, freshness


def test_cf47_the_ordinary_mergeable_pair_is_still_allowed() -> None:
    """Option A: unstamped freshness counts as ACTIVE, and SESSION scope merges. Tightening the
    gate must not veto the case correlation-time auto-merge exists for."""
    from menhir.domain.merge_eligibility import ELIGIBLE, evaluate

    for freshness in (None, "ACTIVE", "active"):
        verdict = evaluate(
            _signals(uuid="a", freshness=freshness, scope="SESSION"),
            _signals(uuid="b", freshness=freshness, scope="SESSION"),
        )
        assert verdict.allowed is True, freshness
        assert verdict.reason_code == ELIGIBLE


# ---------------------------------------------------------------------------
# CF-48 -- aggregate invariants owned by the repository
# ---------------------------------------------------------------------------


def test_cf48_registration_legality_lives_in_the_domain() -> None:
    """The report supplies its own counterexample, which is why it is credible: ordinary status
    transitions ARE delegated via `can_transition`. This is the same delegation for the richer
    operations that diverged from that pattern."""
    from menhir.domain.work_artifact import ArtifactStatus, resolve_registration

    assert resolve_registration("plan", None) == ArtifactStatus.PROPOSED
    with pytest.raises(ValueError, match="unknown artifact_type"):
        resolve_registration("nonsense", None)
    with pytest.raises(ValueError, match="is not valid for"):
        resolve_registration("plan", ArtifactStatus.COMPLETE)


def test_cf48_medium_membership_lives_in_the_domain() -> None:
    from menhir.domain.work_artifact import require_known_medium

    assert require_known_medium("markdown") == "markdown"
    with pytest.raises(ValueError, match="unknown medium"):
        require_known_medium("papyrus")


def test_cf48_supersession_legality_is_emitted_from_the_domain() -> None:
    """Atomicity is a legitimate reason for the MUTATION to be one Cypher statement. It was never
    a reason for the RULE to live there: `TERMINAL_ANY` is the domain's set, and adding a terminal
    status should not need a second edit in infrastructure that nobody remembers to make."""
    from menhir.domain.work_artifact import SUPERSESSION_PARAMS, TERMINAL_ANY, supersession_cypher

    fragment = supersession_cypher()
    assert "new.artifact_type = old.artifact_type" in fragment
    assert "NOT old.status IN $terminal" in fragment
    assert SUPERSESSION_PARAMS["terminal"] == sorted(TERMINAL_ANY)


def test_cf48_the_repository_delegates_rather_than_deciding() -> None:
    source = (_SRC / "infrastructure/work_artifact_repository.py").read_text(encoding="utf-8")
    assert "resolve_registration(" in source
    assert "require_known_medium(" in source
    assert "supersession_cypher()" in source
    assert "if artifact_type not in ARTIFACT_TYPES:" not in source


def test_cf48_the_composed_supersession_query_still_interpolates_its_edge_type() -> None:
    """Splitting an f-string to concatenate the domain fragment silently drops interpolation in
    the trailing segment, and `MERGE (new)-[:{SUPERSEDES_EDGE}]->(old)` is not valid Cypher. The
    existing supersede tests caught it; this pins the property directly."""
    source = (_SRC / "infrastructure/work_artifact_repository.py").read_text(encoding="utf-8")
    assert '+ f"""\n            MERGE (new)-[:{SUPERSEDES_EDGE}]->(old)' in source


# ---------------------------------------------------------------------------
# CF-107 -- the explorer parks the shared event loop
# ---------------------------------------------------------------------------


def _explorer_unthreaded_db_calls() -> list[tuple[str, int, str]]:
    tree = ast.parse((_SRC / "explorer/app.py").read_text(encoding="utf-8"))
    helpers = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(c, ast.Call) and "repo.execute" in ast.unparse(c.func)
            for c in ast.walk(node)
        )
    }
    found: list[tuple[str, int, str]] = []

    def walk(node: ast.AST, on_loop: bool, fn: str | None = None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True, child.name)
                continue
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False, getattr(child, "name", fn))
                continue
            if (
                on_loop
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in helpers
            ):
                found.append((fn or "?", child.lineno, child.func.id))
            walk(child, on_loop, fn)

    walk(tree, False)
    return found


def test_cf107_no_explorer_handler_runs_cypher_on_the_event_loop() -> None:
    """The explorer shares its loop with the API and MCP surfaces, so one page load degraded all
    three. `explorer_home` alone made nine of these calls in sequence -- a count the register
    marked as the lane's and unverified, and which a scope-correct walk confirms."""
    assert _explorer_unthreaded_db_calls() == []


def test_cf107_to_thread_receives_the_callable_not_its_result() -> None:
    """`to_thread(f(x))` reads like the fix and still runs f on the event loop."""
    for name in ("explorer/app.py", "explorer/extraction_lab.py"):
        tree = ast.parse((_SRC / name).read_text(encoding="utf-8"))
        bad = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Call)
        ]
        assert bad == [], f"{name}: {bad}"


def test_cf107_the_extraction_arm_no_longer_resolves_endpoints_inline() -> None:
    """`from_settings` makes two blocking urlopen calls per arm, up to 16 arms per request."""
    tree = ast.parse((_SRC / "explorer/extraction_lab.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_extraction_arm"
    )
    body = ast.unparse(fn)
    assert "to_thread(GraphitiClient.from_settings" in body
    assert "\n        graphiti_client = GraphitiClient.from_settings(settings)" not in body
