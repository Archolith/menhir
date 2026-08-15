from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.kernel import (
    Abstention,
    EvidenceRef,
    Retirement,
    View,
    make_assertion,
)
from spikes.mutation_kernel.lifecycle import ProjectionSlot, rebuild_projection
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.personality import (
    Incident,
    TRAIT_ASSERTION,
    TRAIT_VIEW,
    fold_trait,
    trait_assertion,
)
from spikes.mutation_kernel.personality_codec import PERSONALITY_VALUE_CODEC


SUBJECT = "agent:lifecycle"


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


def _incident(
    incident_id: str,
    *,
    occurred_at: str,
    reflection: str,
) -> Incident:
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


def test_rebuild_repairs_stale_view_after_assertion_only_crash_and_honors_supersession() -> None:
    repo = _repo()
    store = Neo4jEnvelopeStore(
        repo,
        namespace="mutation-lifecycle-repair",
        codec=PERSONALITY_VALUE_CODEC,
    )
    slot = ProjectionSlot(
        assertion_type=TRAIT_ASSERTION,
        view_type=TRAIT_VIEW,
        subject_id=SUBJECT,
        scope="global",
        key="assertiveness",
    )

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        event = _incident(
            "repair-source",
            occurred_at="2026-01-01T10:00:00Z",
            reflection="Initial interpretation favored high assertiveness.",
        )
        old = trait_assertion(
            event,
            trait="assertiveness",
            score=0.9,
            interpreter_version="personality-v1",
        )
        support = trait_assertion(
            _incident(
                "repair-support",
                occurred_at="2026-01-02T10:00:00Z",
                reflection="A second event mildly supported assertiveness.",
            ),
            trait="assertiveness",
            score=0.3,
        )
        for assertion in (old, support):
            store.record_assertion(assertion)

        initial = rebuild_projection(store, slot, fold_trait)
        assert isinstance(initial.outcome, View)
        assert initial.outcome.value > 0
        assert initial.changed is True
        assert initial.assertion_count == 2
        assert initial.current_assertion_count == 2

        corrected = trait_assertion(
            event,
            trait="assertiveness",
            score=-0.9,
            supersedes=(old.assertion_id,),
            interpreter_version="personality-v2",
        )
        store.record_assertion(corrected)

        # Simulate a process dying after the assertion commit but before projection refresh.
        stale = store.load_views(
            subject_id=SUBJECT,
            view_type=TRAIT_VIEW,
            scope="global",
            key="assertiveness",
        )
        assert stale == [initial.outcome]

        repaired = rebuild_projection(store, slot, fold_trait)
        assert isinstance(repaired.outcome, View)
        assert repaired.outcome.value < 0
        assert old.assertion_id not in repaired.outcome.contributor_ids
        assert corrected.assertion_id in repaired.outcome.contributor_ids
        assert repaired.changed is True
        assert repaired.assertion_count == 3
        assert repaired.current_assertion_count == 2
        assert store.count_assertions() == 3
        assert store.count_projections() == 1
        assert store.load_views(
            subject_id=SUBJECT,
            view_type=TRAIT_VIEW,
            scope="global",
            key="assertiveness",
        ) == [repaired.outcome]

        replay = rebuild_projection(store, slot, fold_trait)
        assert replay.outcome == repaired.outcome
        assert replay.changed is False
        assert replay.projection_hash == repaired.projection_hash
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_abstention_replaces_stale_active_view_instead_of_leaving_old_answer_current() -> None:
    repo = _repo()
    store = Neo4jEnvelopeStore(
        repo,
        namespace="mutation-lifecycle-abstain",
        codec=PERSONALITY_VALUE_CODEC,
    )
    slot = ProjectionSlot(
        assertion_type=TRAIT_ASSERTION,
        view_type=TRAIT_VIEW,
        subject_id=SUBJECT,
        scope="global",
        key="warmth",
    )

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        positive = trait_assertion(
            _incident(
                "abstain-positive",
                occurred_at="2026-02-01T10:00:00Z",
                reflection="One event favored warmth.",
            ),
            trait="warmth",
            score=0.8,
        )
        store.record_assertion(positive)
        active = rebuild_projection(store, slot, fold_trait)
        assert isinstance(active.outcome, View)
        assert store.count_views() == 1

        negative = trait_assertion(
            _incident(
                "abstain-negative",
                occurred_at="2026-02-02T10:00:00Z",
                reflection="An equally strong event opposed the earlier interpretation.",
            ),
            trait="warmth",
            score=-0.8,
        )
        store.record_assertion(negative)

        contested = rebuild_projection(store, slot, fold_trait)
        assert isinstance(contested.outcome, Abstention)
        assert contested.outcome.reason == "contested"
        assert contested.changed is True
        assert store.count_projections() == 1
        assert store.count_views() == 0
        assert store.load_views(
            subject_id=SUBJECT,
            view_type=TRAIT_VIEW,
            scope="global",
            key="warmth",
        ) == []
        assert store.load_outcomes(
            subject_id=SUBJECT,
            view_type=TRAIT_VIEW,
            scope="global",
            key="warmth",
        ) == [contested.outcome]

        replay = rebuild_projection(store, slot, fold_trait)
        assert replay.changed is False
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_retirement_is_durable_current_state_and_can_later_reactivate_same_slot() -> None:
    repo = _repo()
    store = Neo4jEnvelopeStore(repo, namespace="mutation-lifecycle-retire")
    dimensions = (("unit", "items"),)

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        active = View(
            view_type="example.inventory_view",
            subject_id="entity:warehouse",
            scope="global",
            key="widget_count",
            value={"count": 10},
            confidence=1.0,
            contributor_ids=("assertion-a",),
            dimensions=dimensions,
        )
        first = store.record_outcome(active)
        assert first["changed"] is True
        assert store.load_views(
            subject_id="entity:warehouse",
            view_type="example.inventory_view",
            key="widget_count",
            dimensions=dimensions,
        ) == [active]

        retired = Retirement(
            view_type=active.view_type,
            subject_id=active.subject_id,
            scope=active.scope,
            key=active.key,
            reason="explicit_expiry",
            effective_at="2026-03-02T00:00:00Z",
            contributor_ids=("assertion-expire",),
            retired_value={"count": 10},
            dimensions=dimensions,
        )
        second = store.record_outcome(retired)
        assert second["changed"] is True
        assert store.count_projections() == 1
        assert store.count_views() == 0
        assert store.load_views(
            subject_id=active.subject_id,
            view_type=active.view_type,
            key=active.key,
            dimensions=dimensions,
        ) == []
        assert store.load_outcomes(
            subject_id=active.subject_id,
            view_type=active.view_type,
            key=active.key,
            dimensions=dimensions,
        ) == [retired]

        reactivated = View(
            view_type=active.view_type,
            subject_id=active.subject_id,
            scope=active.scope,
            key=active.key,
            value={"count": 4},
            confidence=0.9,
            contributor_ids=("assertion-new",),
            dimensions=dimensions,
        )
        third = store.record_outcome(reactivated)
        assert third["changed"] is True
        assert store.count_projections() == 1
        assert store.count_views() == 1
        assert store.load_outcomes(
            subject_id=active.subject_id,
            view_type=active.view_type,
            key=active.key,
            dimensions=dimensions,
        ) == [reactivated]
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_dimension_filter_keeps_rebuild_slots_from_mixing_extension_owned_axes() -> None:
    repo = _repo()
    store = Neo4jEnvelopeStore(repo, namespace="mutation-lifecycle-dimensions")

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        items = make_assertion(
            source=EvidenceRef("dimension-items", "fixture"),
            assertion_type="example.metric",
            subject_id="entity:warehouse",
            scope="global",
            key="count",
            value={"n": 5},
            valid_at="2026-04-01T00:00:00Z",
            dimensions=(("unit", "items"),),
        )
        boxes = make_assertion(
            source=EvidenceRef("dimension-boxes", "fixture"),
            assertion_type="example.metric",
            subject_id="entity:warehouse",
            scope="global",
            key="count",
            value={"n": 2},
            valid_at="2026-04-01T00:00:00Z",
            dimensions=(("unit", "boxes"),),
        )
        store.record_assertion(items)
        store.record_assertion(boxes)

        assert store.load_assertions(
            subject_id="entity:warehouse",
            assertion_type="example.metric",
            scope="global",
            key="count",
            dimensions=(("unit", "items"),),
        ) == [items]
        assert store.load_assertions(
            subject_id="entity:warehouse",
            assertion_type="example.metric",
            scope="global",
            key="count",
            dimensions=(("unit", "boxes"),),
        ) == [boxes]
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
