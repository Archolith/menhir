from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.kernel import View
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.personality import (
    Incident,
    POLICY_ASSERTION,
    POLICY_VIEW,
    PolicyDecision,
    fold_policy,
    policy_assertion,
)
from spikes.mutation_kernel.personality_codec import PERSONALITY_VALUE_CODEC


SUBJECT = "agent:reference"
NAMESPACE = "mutation-personality"


def _incident(
    incident_id: str,
    *,
    occurred_at: str,
    reaction: str,
    outcome_score: float,
    reflection: str,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        subject_id=SUBJECT,
        scope="global",
        occurred_at=occurred_at,
        situation="disagreement",
        reaction=reaction,
        outcome=f"outcome {outcome_score:+.1f}",
        outcome_score=outcome_score,
        reflection=reflection,
    )


def test_personality_round_trips_through_generic_neo4j_store() -> None:
    """Personality semantics stay outside persistence while envelopes survive a real round trip."""
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

    store = Neo4jEnvelopeStore(
        repo,
        namespace=NAMESPACE,
        codec=PERSONALITY_VALUE_CODEC,
    )

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        initial_incidents = [
            _incident(
                "persist-a",
                occurred_at="2026-01-01T10:00:00Z",
                reaction="direct_confrontation",
                outcome_score=0.3,
                reflection="Direct confrontation solved the fact but added friction.",
            ),
            _incident(
                "persist-b",
                occurred_at="2026-01-02T10:00:00Z",
                reaction="direct_confrontation",
                outcome_score=-0.8,
                reflection="Escalation obscured the substance.",
            ),
            _incident(
                "persist-c",
                occurred_at="2026-01-03T10:00:00Z",
                reaction="calm_factual_pushback",
                outcome_score=0.9,
                reflection="Evidence-first disagreement worked well.",
            ),
        ]
        initial_assertions = [policy_assertion(item) for item in initial_incidents]

        for assertion in initial_assertions:
            store.record_assertion(assertion)

        reloaded = store.load_assertions(
            subject_id=SUBJECT,
            assertion_type=POLICY_ASSERTION,
            scope="global",
            key="disagreement",
        )
        assert reloaded == initial_assertions

        initial_view = fold_policy(reloaded)
        assert isinstance(initial_view, View)
        assert isinstance(initial_view.value, PolicyDecision)
        assert initial_view.value.action == "calm_factual_pushback"

        store.record_view(initial_view)
        persisted_views = store.load_views(
            subject_id=SUBJECT,
            view_type=POLICY_VIEW,
            scope="global",
            key="disagreement",
        )
        assert persisted_views == [initial_view]

        for assertion in initial_assertions:
            store.record_assertion(assertion)
        store.record_view(initial_view)
        assert store.count_assertions() == 3
        assert store.count_views() == 1

        later_assertions = [
            policy_assertion(
                _incident(
                    "persist-d",
                    occurred_at="2026-01-04T10:00:00Z",
                    reaction="direct_confrontation",
                    outcome_score=1.0,
                    reflection="A later high-stakes case rewarded directness.",
                )
            ),
            policy_assertion(
                _incident(
                    "persist-e",
                    occurred_at="2026-01-05T10:00:00Z",
                    reaction="direct_confrontation",
                    outcome_score=1.0,
                    reflection="A second later case also rewarded directness.",
                )
            ),
        ]
        for assertion in later_assertions:
            store.record_assertion(assertion)

        all_reloaded = store.load_assertions(
            subject_id=SUBJECT,
            assertion_type=POLICY_ASSERTION,
            scope="global",
            key="disagreement",
        )
        assert all_reloaded == initial_assertions + later_assertions
        updated_view = fold_policy(all_reloaded)
        assert isinstance(updated_view, View)
        assert isinstance(updated_view.value, PolicyDecision)
        assert updated_view.value.action == "direct_confrontation"

        store.record_view(updated_view)
        assert store.count_assertions() == 5
        assert store.count_views() == 1
        assert store.load_views(
            subject_id=SUBJECT,
            view_type=POLICY_VIEW,
            scope="global",
            key="disagreement",
        ) == [updated_view]

        other = Neo4jEnvelopeStore(
            repo,
            namespace="mutation-personality-other",
            codec=PERSONALITY_VALUE_CODEC,
        )
        other.record_assertion(initial_assertions[0])
        assert store.count_assertions() == 5
        assert other.count_assertions() == 1
        assert other.load_assertions(subject_id=SUBJECT) == [initial_assertions[0]]

        typed_count = repo.execute("MATCH (a:TypedAssertion) RETURN count(a) AS n")[0]["n"]
        scalar_view_count = repo.execute(
            "MATCH (v:Entity {view_kind:'scalar_state'}) RETURN count(v) AS n"
        )[0]["n"]
        assert typed_count == 0
        assert scalar_view_count == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
