from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.dirty import (
    GenerationalNeo4jEnvelopeStore,
    reconcile_dirty_batch,
)
from spikes.mutation_kernel.kernel import View
from spikes.mutation_kernel.lifecycle import ProjectionSlot, derive_projection
from spikes.mutation_kernel.personality import (
    Incident,
    TRAIT_ASSERTION,
    TRAIT_VIEW,
    fold_trait,
    trait_assertion,
)
from spikes.mutation_kernel.personality_codec import PERSONALITY_VALUE_CODEC


SUBJECT = "agent:dirty"


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


def _slot(trait: str) -> ProjectionSlot:
    return ProjectionSlot(
        assertion_type=TRAIT_ASSERTION,
        view_type=TRAIT_VIEW,
        subject_id=SUBJECT,
        scope="global",
        key=trait,
    )


def _resolver(slot: ProjectionSlot):
    assert slot.assertion_type == TRAIT_ASSERTION
    assert slot.view_type == TRAIT_VIEW
    return fold_trait


def test_new_assertion_marks_exact_slot_dirty_atomically_and_replay_does_not_advance() -> None:
    repo = _repo()
    store = GenerationalNeo4jEnvelopeStore(
        repo,
        namespace="mutation-dirty-replay",
        codec=PERSONALITY_VALUE_CODEC,
    )
    slot = _slot("warmth")
    assertion = trait_assertion(
        _incident(
            "dirty-replay-a",
            occurred_at="2026-01-01T00:00:00Z",
            reflection="Warmth improved the interaction.",
        ),
        trait="warmth",
        score=0.8,
    )

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        first = store.record_assertion_for_slots(assertion, (slot,))
        assert first["created"] is True
        assert store.count_assertions() == 1
        dirty = store.load_dirty_slots()
        assert len(dirty) == 1
        assert dirty[0].slot == slot
        assert dirty[0].generation == 1
        assert dirty[0].projected_generation == 0

        replay = store.record_assertion_for_slots(assertion, (slot,))
        assert replay["created"] is False
        assert store.count_assertions() == 1
        dirty_after_replay = store.load_dirty_slots()
        assert len(dirty_after_replay) == 1
        assert dirty_after_replay[0].generation == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_dirty_batch_rebuilds_only_registered_work_and_generation_advances_on_new_evidence() -> None:
    repo = _repo()
    store = GenerationalNeo4jEnvelopeStore(
        repo,
        namespace="mutation-dirty-batch",
        codec=PERSONALITY_VALUE_CODEC,
    )
    slot = _slot("assertiveness")

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        first_assertion = trait_assertion(
            _incident(
                "dirty-batch-a",
                occurred_at="2026-02-01T00:00:00Z",
                reflection="Calm directness worked well.",
            ),
            trait="assertiveness",
            score=0.8,
        )
        store.record_assertion_for_slots(first_assertion, (slot,))
        assert store.count_dirty_slots() == 1

        first_batch = reconcile_dirty_batch(store, _resolver)
        assert len(first_batch) == 1
        assert first_batch[0].dirty.generation == 1
        assert first_batch[0].applied is True
        assert first_batch[0].persisted_generation == 1
        assert isinstance(first_batch[0].outcome, View)
        assert store.count_dirty_slots() == 0

        second_assertion = trait_assertion(
            _incident(
                "dirty-batch-b",
                occurred_at="2026-02-02T00:00:00Z",
                reflection="A second event moderated the initial reading.",
            ),
            trait="assertiveness",
            score=0.2,
        )
        store.record_assertion_for_slots(second_assertion, (slot,))
        dirty = store.load_dirty_slots()
        assert len(dirty) == 1
        assert dirty[0].generation == 2
        assert dirty[0].projected_generation == 1

        second_batch = reconcile_dirty_batch(store, _resolver)
        assert len(second_batch) == 1
        assert second_batch[0].dirty.generation == 2
        assert second_batch[0].persisted_generation == 2
        assert second_batch[0].current_generation == 2
        assert store.count_dirty_slots() == 0
        assert store.count_assertions() == 2
        assert store.count_projections() == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_late_old_generation_cannot_overwrite_newer_projection() -> None:
    repo = _repo()
    store = GenerationalNeo4jEnvelopeStore(
        repo,
        namespace="mutation-dirty-stale-worker",
        codec=PERSONALITY_VALUE_CODEC,
    )
    slot = _slot("warmth")

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        first = trait_assertion(
            _incident(
                "dirty-stale-a",
                occurred_at="2026-03-01T00:00:00Z",
                reflection="The first event strongly favored warmth.",
            ),
            trait="warmth",
            score=0.8,
        )
        store.record_assertion_for_slots(first, (slot,))
        observed_generation_one = store.load_dirty_slots()[0]
        stale_derivation = derive_projection(store, slot, fold_trait)
        assert isinstance(stale_derivation.outcome, View)

        second = trait_assertion(
            _incident(
                "dirty-stale-b",
                occurred_at="2026-03-02T00:00:00Z",
                reflection="A later event added contrary evidence.",
            ),
            trait="warmth",
            score=-0.2,
        )
        store.record_assertion_for_slots(second, (slot,))
        current_dirty = store.load_dirty_slots()
        assert len(current_dirty) == 1
        assert current_dirty[0].generation == 2

        current = reconcile_dirty_batch(store, _resolver)
        assert len(current) == 1
        assert current[0].persisted_generation == 2
        current_outcome = store.load_outcomes(
            subject_id=SUBJECT,
            view_type=TRAIT_VIEW,
            scope="global",
            key="warmth",
        )[0]
        assert current_outcome == current[0].outcome
        assert current_outcome != stale_derivation.outcome

        late = store.record_outcome_at_generation(
            stale_derivation.outcome,
            slot=slot,
            generation=observed_generation_one.generation,
        )
        assert late["applied"] is False
        assert late["changed"] is False
        assert late["persisted_generation"] == 2
        assert store.load_outcomes(
            subject_id=SUBJECT,
            view_type=TRAIT_VIEW,
            scope="global",
            key="warmth",
        ) == [current_outcome]
        assert store.count_dirty_slots() == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_bounded_dirty_discovery_leaves_unprocessed_slots_dirty() -> None:
    repo = _repo()
    store = GenerationalNeo4jEnvelopeStore(
        repo,
        namespace="mutation-dirty-limit",
        codec=PERSONALITY_VALUE_CODEC,
    )
    warmth = _slot("warmth")
    skepticism = _slot("skepticism")

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        store.record_assertion_for_slots(
            trait_assertion(
                _incident(
                    "dirty-limit-warmth",
                    occurred_at="2026-04-01T00:00:00Z",
                    reflection="Warm response worked.",
                ),
                trait="warmth",
                score=0.7,
            ),
            (warmth,),
        )
        store.record_assertion_for_slots(
            trait_assertion(
                _incident(
                    "dirty-limit-skepticism",
                    occurred_at="2026-04-02T00:00:00Z",
                    reflection="Skepticism caught a bad premise.",
                ),
                trait="skepticism",
                score=0.6,
            ),
            (skepticism,),
        )
        assert store.count_dirty_slots() == 2
        assert len(store.load_dirty_slots(limit=1)) == 1

        first_batch = reconcile_dirty_batch(store, _resolver, limit=1)
        assert len(first_batch) == 1
        assert store.count_dirty_slots() == 1

        second_batch = reconcile_dirty_batch(store, _resolver, limit=1)
        assert len(second_batch) == 1
        assert first_batch[0].dirty.slot != second_batch[0].dirty.slot
        assert store.count_dirty_slots() == 0
        assert store.count_projections() == 2
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
