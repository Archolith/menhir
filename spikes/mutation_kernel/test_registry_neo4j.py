from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.kernel import (
    EvidenceRef,
    View,
    effective_authority,
    make_assertion,
)
from spikes.mutation_kernel.personality import (
    ContinuousSignal,
    Incident,
    TRAIT_ASSERTION,
    TRAIT_VIEW,
    fold_trait,
    trait_assertion,
)
from spikes.mutation_kernel.personality_codec import PERSONALITY_VALUE_CODEC
from spikes.mutation_kernel.registry import (
    ProjectionDefinition,
    ProjectionRegistry,
    ProjectionTarget,
    RegisteredProjectionNeo4jStore,
    backfill_definition,
    reconcile_registered_batch,
    route_and_record_assertion,
)


SUBJECT = "agent:registry"
PREFERENCE_ASSERTION = "personality.explicit_preference"
CONFLICT_VIEW = "personality.conflict_style_view"


def _repo() -> Neo4jRepository:
    uri = os.getenv("MENHIR_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("requires explicit MENHIR_TEST_NEO4J_URI scratch instance")
    repo = Neo4jRepository(
        uri=uri,
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
    )
    if not repo.ping():
        repo.close()
        pytest.fail(f"configured test Neo4j is unreachable: {uri}")
    return repo


def _incident(incident_id: str, *, occurred_at: str, reflection: str) -> Incident:
    return Incident(
        incident_id=incident_id,
        subject_id=SUBJECT,
        scope="global",
        occurred_at=occurred_at,
        situation="disagreement",
        reaction="calm_factual_pushback",
        outcome="resolved",
        outcome_score=0.5,
        reflection=reflection,
    )


def _trait_target(assertion):
    if assertion.assertion_type != TRAIT_ASSERTION:
        return ()
    return (
        ProjectionTarget(
            view_type=TRAIT_VIEW,
            subject_id=assertion.subject_id,
            scope=assertion.scope,
            key=assertion.key,
            dimensions=assertion.dimensions,
        ),
    )


def _trait_definition(*, version: int = 1, fold=fold_trait) -> ProjectionDefinition:
    return ProjectionDefinition(
        definition_id="personality.traits",
        version=version,
        input_types=(TRAIT_ASSERTION,),
        view_type=TRAIT_VIEW,
        mapper=_trait_target,
        fold=fold,
    )


def test_new_definition_backfills_old_history_once() -> None:
    repo = _repo()
    store = RegisteredProjectionNeo4jStore(
        repo,
        namespace="mutation-registry-backfill",
        codec=PERSONALITY_VALUE_CODEC,
    )
    definition = _trait_definition()
    registry = ProjectionRegistry((definition,))

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        warmth = trait_assertion(
            _incident(
                "backfill-warmth",
                occurred_at="2026-01-01T00:00:00Z",
                reflection="Warmth helped.",
            ),
            trait="warmth",
            score=0.8,
        )
        skepticism = trait_assertion(
            _incident(
                "backfill-skepticism",
                occurred_at="2026-01-02T00:00:00Z",
                reflection="Skepticism caught a bad premise.",
            ),
            trait="skepticism",
            score=0.6,
        )

        # These assertions predate the projection definition, so no dirty work exists yet.
        store.record_assertion(warmth)
        store.record_assertion(skepticism)
        assert store.count_registered_dirty() == 0

        backfill = backfill_definition(store, definition)
        assert backfill.scanned_assertion_count == 2
        assert backfill.target_count == 2
        assert backfill.dirtied_target_count == 2
        assert store.count_registered_dirty() == 2

        rebuilt = reconcile_registered_batch(store, registry)
        assert len(rebuilt) == 2
        assert all(item.applied for item in rebuilt)
        assert all(isinstance(item.outcome, View) for item in rebuilt)
        assert store.count_registered_dirty() == 0
        assert store.count_projections() == 2

        # Backfill is target/version idempotent; rerunning it does not manufacture new work.
        replay = backfill_definition(store, definition)
        assert replay.scanned_assertion_count == 2
        assert replay.target_count == 2
        assert replay.dirtied_target_count == 0
        assert store.count_registered_dirty() == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_registry_routes_new_assertions_and_replay_does_not_advance_generation() -> None:
    repo = _repo()
    store = RegisteredProjectionNeo4jStore(
        repo,
        namespace="mutation-registry-route",
        codec=PERSONALITY_VALUE_CODEC,
    )
    definition = _trait_definition()
    registry = ProjectionRegistry((definition,))
    assertion = trait_assertion(
        _incident(
            "route-warmth",
            occurred_at="2026-02-01T00:00:00Z",
            reflection="Warmth improved the exchange.",
        ),
        trait="warmth",
        score=0.7,
    )

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        first = route_and_record_assertion(store, registry, assertion)
        assert first["created"] is True
        assert first["target_count"] == 1
        dirty = store.load_registered_dirty()
        assert len(dirty) == 1
        assert dirty[0].generation == 1

        replay = route_and_record_assertion(store, registry, assertion)
        assert replay["created"] is False
        dirty_after_replay = store.load_registered_dirty()
        assert len(dirty_after_replay) == 1
        assert dirty_after_replay[0].generation == 1

        rebuilt = reconcile_registered_batch(store, registry)
        assert len(rebuilt) == 1
        assert rebuilt[0].persisted_definition_version == 1
        assert rebuilt[0].persisted_generation == 1
        assert store.count_registered_dirty() == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def _conflict_target(assertion):
    accepted = (
        assertion.assertion_type == TRAIT_ASSERTION
        and assertion.key == "assertiveness"
    ) or (
        assertion.assertion_type == PREFERENCE_ASSERTION
        and assertion.key == "conflict_directness"
    )
    if not accepted:
        return ()
    return (
        ProjectionTarget(
            view_type=CONFLICT_VIEW,
            subject_id=assertion.subject_id,
            scope=assertion.scope,
            key="conflict_style",
        ),
    )


def _fold_conflict_style(assertions):
    rows = list(assertions)
    if not rows:
        raise ValueError("conflict-style fold requires evidence")
    weighted = []
    for row in rows:
        if not isinstance(row.value, ContinuousSignal):
            raise TypeError("conflict-style inputs must carry ContinuousSignal")
        weight = row.value.weight * row.confidence
        weighted.append((row, row.value, weight))
    mass = sum(weight for _, _, weight in weighted)
    score = sum(signal.score * weight for _, signal, weight in weighted) / mass
    return View(
        view_type=CONFLICT_VIEW,
        subject_id=rows[0].subject_id,
        scope=rows[0].scope,
        key="conflict_style",
        value=round(score, 6),
        confidence=min(1.0, mass / 3.0),
        contributor_ids=tuple(row.assertion_id for row in rows),
        effective_authority=effective_authority(rows),
        rationale=tuple(
            signal.explanation for _, signal, _ in weighted if signal.explanation
        ),
    )


def _conflict_definition(*, version: int = 1) -> ProjectionDefinition:
    return ProjectionDefinition(
        definition_id="personality.conflict-style",
        version=version,
        input_types=tuple(sorted((TRAIT_ASSERTION, PREFERENCE_ASSERTION))),
        view_type=CONFLICT_VIEW,
        mapper=_conflict_target,
        fold=_fold_conflict_style,
    )


def _preference(
    source_id: str,
    *,
    score: float,
    weight: float,
    occurred_at: str,
):
    return make_assertion(
        source=EvidenceRef(source_id, "personality.manual_config"),
        assertion_type=PREFERENCE_ASSERTION,
        subject_id=SUBJECT,
        scope="global",
        key="conflict_directness",
        value=ContinuousSignal(
            score=score,
            weight=weight,
            explanation="Explicit preference about how directly to handle conflict.",
        ),
        valid_at=occurred_at,
        authority="user",
    )


def test_one_registered_view_can_fold_multiple_assertion_types() -> None:
    repo = _repo()
    store = RegisteredProjectionNeo4jStore(
        repo,
        namespace="mutation-registry-multi-input",
        codec=PERSONALITY_VALUE_CODEC,
    )
    definition = _conflict_definition()
    registry = ProjectionRegistry((definition,))

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        observed_trait = trait_assertion(
            _incident(
                "multi-trait",
                occurred_at="2026-03-01T00:00:00Z",
                reflection="Observed behavior suggested only mild directness.",
            ),
            trait="assertiveness",
            score=0.2,
        )
        explicit_preference = _preference(
            "multi-preference",
            score=0.9,
            weight=2.0,
            occurred_at="2026-03-02T00:00:00Z",
        )
        store.record_assertion(observed_trait)
        store.record_assertion(explicit_preference)

        backfill = backfill_definition(store, definition)
        assert backfill.scanned_assertion_count == 2
        assert backfill.target_count == 1
        assert backfill.dirtied_target_count == 1

        first = reconcile_registered_batch(store, registry)
        assert len(first) == 1
        assert isinstance(first[0].outcome, View)
        assert first[0].contributing_assertion_count == 2
        assert first[0].outcome.value > 0.6
        assert set(first[0].outcome.contributor_ids) == {
            observed_trait.assertion_id,
            explicit_preference.assertion_id,
        }

        # A later explicit preference routes to the same output slot even though its assertion type
        # differs from the trait evidence already feeding that View.
        later_preference = _preference(
            "multi-preference-later",
            score=-0.8,
            weight=3.0,
            occurred_at="2026-03-03T00:00:00Z",
        )
        route_and_record_assertion(store, registry, later_preference)
        dirty = store.load_registered_dirty()
        assert len(dirty) == 1
        assert dirty[0].target.view_type == CONFLICT_VIEW
        assert dirty[0].generation == 2

        second = reconcile_registered_batch(store, registry)
        assert len(second) == 1
        assert isinstance(second[0].outcome, View)
        assert second[0].contributing_assertion_count == 3
        assert second[0].outcome.value < first[0].outcome.value
        assert store.count_registered_dirty() == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def _fold_trait_conservative(assertions):
    base = fold_trait(assertions)
    if not isinstance(base, View):
        return base
    return View(
        view_type=base.view_type,
        subject_id=base.subject_id,
        scope=base.scope,
        key=base.key,
        value=round(float(base.value) * 0.5, 6),
        confidence=base.confidence,
        contributor_ids=base.contributor_ids,
        counterevidence_ids=base.counterevidence_ids,
        effective_authority=base.effective_authority,
        rationale=base.rationale + ("projection-v2 applies a conservative trait scale",),
        dimensions=base.dimensions,
    )


def test_definition_version_backfill_rebuilds_history_and_old_worker_cannot_overwrite() -> None:
    repo = _repo()
    store = RegisteredProjectionNeo4jStore(
        repo,
        namespace="mutation-registry-version",
        codec=PERSONALITY_VALUE_CODEC,
    )
    v1 = _trait_definition(version=1)
    registry_v1 = ProjectionRegistry((v1,))

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        assertion = trait_assertion(
            _incident(
                "version-warmth",
                occurred_at="2026-04-01T00:00:00Z",
                reflection="Warm behavior was effective.",
            ),
            trait="warmth",
            score=0.8,
        )
        store.record_assertion(assertion)
        backfill_definition(store, v1)
        old_dirty = store.load_registered_dirty()[0]
        old_result = reconcile_registered_batch(store, registry_v1)[0]
        assert isinstance(old_result.outcome, View)
        assert old_result.persisted_definition_version == 1
        assert old_result.persisted_generation == 1

        v2 = _trait_definition(version=2, fold=_fold_trait_conservative)
        registry_v2 = ProjectionRegistry((v2,))
        upgraded = backfill_definition(store, v2)
        assert upgraded.scanned_assertion_count == 1
        assert upgraded.target_count == 1
        assert upgraded.dirtied_target_count == 1

        dirty_v2 = store.load_registered_dirty()
        assert len(dirty_v2) == 1
        assert dirty_v2[0].definition_version == 2
        assert dirty_v2[0].generation == 2
        new_result = reconcile_registered_batch(store, registry_v2)[0]
        assert isinstance(new_result.outcome, View)
        assert new_result.persisted_definition_version == 2
        assert new_result.persisted_generation == 2
        assert new_result.outcome.value != old_result.outcome.value

        # A worker that started from v1 before the definition upgrade is no longer allowed to write.
        late = store.record_registered_outcome(old_result.outcome, old_dirty)
        assert late["applied"] is False
        assert late["persisted_definition_version"] == 2
        assert late["persisted_generation"] == 2

        repeated_v2 = backfill_definition(store, v2)
        assert repeated_v2.dirtied_target_count == 0
        assert store.count_registered_dirty() == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
