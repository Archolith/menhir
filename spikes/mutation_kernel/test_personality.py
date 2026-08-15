from __future__ import annotations

from spikes.mutation_kernel.kernel import Abstention, View, current_assertions
from spikes.mutation_kernel.personality import (
    Incident,
    PolicyDecision,
    choose_scoped_policy,
    fold_policy,
    fold_trait,
    fold_value,
    policy_assertion,
    trait_assertion,
    value_assertion,
)


SUBJECT = "agent:reference"


def incident(
    incident_id: str,
    *,
    scope: str = "global",
    occurred_at: str,
    situation: str = "disagreement",
    reaction: str,
    outcome_score: float,
    reflection: str,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        subject_id=SUBJECT,
        scope=scope,
        occurred_at=occurred_at,
        situation=situation,
        reaction=reaction,
        outcome=f"outcome {outcome_score:+.1f}",
        outcome_score=outcome_score,
        reflection=reflection,
    )


def test_policy_fold_learns_from_reaction_outcomes_without_erasing_history() -> None:
    a = incident(
        "incident-a",
        occurred_at="2026-01-01T10:00:00Z",
        reaction="direct_confrontation",
        outcome_score=0.3,
        reflection="Direct confrontation resolved the factual issue, but added friction.",
    )
    b = incident(
        "incident-b",
        occurred_at="2026-01-02T10:00:00Z",
        reaction="direct_confrontation",
        outcome_score=-0.8,
        reflection="Escalation obscured the substance and damaged the interaction.",
    )
    c = incident(
        "incident-c",
        occurred_at="2026-01-03T10:00:00Z",
        reaction="calm_factual_pushback",
        outcome_score=0.9,
        reflection="Evidence-first disagreement corrected the claim without escalating.",
    )

    assertions = [policy_assertion(x) for x in (a, b, c)]
    view = fold_policy(assertions)

    assert isinstance(view, View)
    assert isinstance(view.value, PolicyDecision)
    assert view.value.action == "calm_factual_pushback"
    assert len(view.contributor_ids) == 3
    assert len(view.counterevidence_ids) == 2

    # Rebuilding a View does not mutate or collapse the incident/assertion history.
    assert len(assertions) == 3
    assert {ref.source_id for row in assertions for ref in row.evidence} == {
        "incident-a",
        "incident-b",
        "incident-c",
    }


def test_person_specific_behavior_can_override_global_behavior_without_changing_global_view() -> None:
    global_a = incident(
        "global-a",
        occurred_at="2026-02-01T10:00:00Z",
        reaction="calm_factual_pushback",
        outcome_score=0.8,
        reflection="Calm evidence-first disagreement worked well generally.",
    )
    global_b = incident(
        "global-b",
        occurred_at="2026-02-02T10:00:00Z",
        reaction="direct_confrontation",
        outcome_score=-0.4,
        reflection="Direct confrontation was usually counterproductive.",
    )
    alice_a = incident(
        "alice-a",
        scope="person:alice",
        occurred_at="2026-02-03T10:00:00Z",
        reaction="light_humor",
        outcome_score=0.9,
        reflection="Alice responded well to humor before the correction.",
    )
    alice_b = incident(
        "alice-b",
        scope="person:alice",
        occurred_at="2026-02-04T10:00:00Z",
        reaction="calm_factual_pushback",
        outcome_score=0.2,
        reflection="Factual pushback worked, but was less effective than humor with Alice.",
    )

    global_view = fold_policy([policy_assertion(global_a), policy_assertion(global_b)])
    alice_view = fold_policy([policy_assertion(alice_a), policy_assertion(alice_b)])

    assert isinstance(global_view, View)
    assert isinstance(alice_view, View)
    assert isinstance(global_view.value, PolicyDecision)
    assert isinstance(alice_view.value, PolicyDecision)
    assert global_view.value.action == "calm_factual_pushback"
    assert alice_view.value.action == "light_humor"

    chosen = choose_scoped_policy([global_view, alice_view], person_id="alice")
    assert chosen == alice_view
    assert global_view.scope == "global"


def test_group_policy_is_between_person_and_global_scope() -> None:
    global_incident = incident(
        "g0",
        occurred_at="2026-03-01T10:00:00Z",
        reaction="explain_fully",
        outcome_score=0.8,
        reflection="Full explanation generally works.",
    )
    maintainer_incident = incident(
        "g1",
        scope="group:maintainers",
        occurred_at="2026-03-02T10:00:00Z",
        reaction="lead_with_diff",
        outcome_score=0.9,
        reflection="Maintainers preferred seeing the concrete change first.",
    )
    alice_incident = incident(
        "g2",
        scope="person:alice",
        occurred_at="2026-03-03T10:00:00Z",
        reaction="ask_first",
        outcome_score=0.95,
        reflection="Alice preferred a question before a proposed change.",
    )

    global_view = fold_policy([policy_assertion(global_incident)])
    group_view = fold_policy([policy_assertion(maintainer_incident)])
    person_view = fold_policy([policy_assertion(alice_incident)])
    assert all(isinstance(v, View) for v in (global_view, group_view, person_view))

    assert choose_scoped_policy(
        [global_view, group_view], group_ids=["maintainers"]
    ) == group_view
    assert choose_scoped_policy(
        [global_view, group_view, person_view],
        person_id="alice",
        group_ids=["maintainers"],
    ) == person_view


def test_trait_fold_preserves_contrary_incidents_as_counterevidence() -> None:
    warm = incident(
        "trait-a",
        occurred_at="2026-04-01T10:00:00Z",
        reaction="empathetic_response",
        outcome_score=0.8,
        reflection="Warmth made the correction easier to receive.",
    )
    cold = incident(
        "trait-b",
        occurred_at="2026-04-02T10:00:00Z",
        reaction="terse_response",
        outcome_score=-0.4,
        reflection="Being overly terse reduced rapport.",
    )
    warm_again = incident(
        "trait-c",
        occurred_at="2026-04-03T10:00:00Z",
        reaction="empathetic_response",
        outcome_score=0.7,
        reflection="Warmth helped again.",
    )

    assertions = [
        trait_assertion(warm, trait="warmth", score=0.8),
        trait_assertion(cold, trait="warmth", score=-0.5),
        trait_assertion(warm_again, trait="warmth", score=0.7),
    ]
    view = fold_trait(assertions)

    assert isinstance(view, View)
    assert view.value > 0
    assert len(view.contributor_ids) == 3
    assert len(view.counterevidence_ids) == 1


def test_reflection_can_be_corrected_by_superseding_only_the_interpretation() -> None:
    event = incident(
        "reflection-correction",
        occurred_at="2026-05-01T10:00:00Z",
        reaction="direct_confrontation",
        outcome_score=0.6,
        reflection="Initial interpretation: confrontation showed useful assertiveness.",
    )
    old = trait_assertion(
        event,
        trait="confrontationality",
        score=0.9,
        interpreter_version="personality-v1",
    )
    corrected = trait_assertion(
        event,
        trait="confrontationality",
        score=-0.4,
        supersedes=(old.assertion_id,),
        interpreter_version="personality-v2",
    )
    corroborating = trait_assertion(
        incident(
            "reflection-corroboration",
            occurred_at="2026-05-02T10:00:00Z",
            reaction="calm_pushback",
            outcome_score=0.7,
            reflection="A later incident supports lower confrontationality.",
        ),
        trait="confrontationality",
        score=-0.5,
    )

    current = current_assertions([old, corrected, corroborating])
    assert old not in current
    assert corrected in current

    view = fold_trait([old, corrected, corroborating])
    assert isinstance(view, View)
    assert view.value < 0
    assert old.assertion_id not in view.contributor_ids

    # The correction reuses the same grounded source locator even though its interpretation changed.
    assert old.source_key == corrected.source_key
    assert old.assertion_id != corrected.assertion_id


def test_value_fold_can_materialize_an_evolving_belief_with_provenance() -> None:
    events = [
        incident(
            "value-a",
            occurred_at="2026-06-01T10:00:00Z",
            reaction="protect_fairness",
            outcome_score=0.7,
            reflection="Fair treatment mattered more than avoiding a small conflict.",
        ),
        incident(
            "value-b",
            occurred_at="2026-06-02T10:00:00Z",
            reaction="preserve_harmony",
            outcome_score=0.1,
            reflection="Harmony had some value in a low-stakes case.",
        ),
        incident(
            "value-c",
            occurred_at="2026-06-03T10:00:00Z",
            reaction="protect_fairness",
            outcome_score=0.8,
            reflection="A later outcome reinforced fairness over superficial agreement.",
        ),
    ]
    assertions = [
        value_assertion(events[0], value_name="fairness_over_harmony", score=0.8),
        value_assertion(events[1], value_name="fairness_over_harmony", score=-0.2),
        value_assertion(events[2], value_name="fairness_over_harmony", score=0.9),
    ]

    view = fold_value(assertions)
    assert isinstance(view, View)
    assert view.value > 0.4
    assert len(view.contributor_ids) == 3
    assert len(view.counterevidence_ids) == 1


def test_rebuild_is_input_order_invariant() -> None:
    events = [
        incident(
            "order-a",
            occurred_at="2026-07-01T10:00:00Z",
            reaction="ask_question",
            outcome_score=0.8,
            reflection="Question-first worked.",
        ),
        incident(
            "order-b",
            occurred_at="2026-07-02T10:00:00Z",
            reaction="state_conclusion",
            outcome_score=0.1,
            reflection="Leading with the conclusion worked less well.",
        ),
        incident(
            "order-c",
            occurred_at="2026-07-03T10:00:00Z",
            reaction="ask_question",
            outcome_score=0.7,
            reflection="Question-first worked again.",
        ),
    ]
    rows = [policy_assertion(e) for e in events]
    assert fold_policy(rows) == fold_policy(list(reversed(rows)))


def test_contested_value_abstains_instead_of_inventing_a_middle_belief() -> None:
    a = value_assertion(
        incident(
            "contest-a",
            occurred_at="2026-08-01T10:00:00Z",
            reaction="choose_autonomy",
            outcome_score=0.5,
            reflection="One experience favored autonomy.",
        ),
        value_name="autonomy_over_coordination",
        score=0.8,
    )
    b = value_assertion(
        incident(
            "contest-b",
            occurred_at="2026-08-02T10:00:00Z",
            reaction="choose_coordination",
            outcome_score=0.5,
            reflection="Another experience favored coordination.",
        ),
        value_name="autonomy_over_coordination",
        score=-0.8,
    )

    result = fold_value([a, b])
    assert isinstance(result, Abstention)
    assert result.reason == "contested"
