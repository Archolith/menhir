"""Per-fixture candidate pools for the 7 eligibility-selection scenarios (Phase 4b).

reference_time for each fixture is the REAL valid_at of that fixture's current_message
episode, pulled directly from the live LME graph during earlier phases -- kept here for
continuity/realism rather than an arbitrary date.
"""

from __future__ import annotations

from menhir.explorer.extraction_lab_eligibility_selection import (
    DecoyType,
    EligibilityScenario,
    StructuredFact,
)

# ---------------------------------------------------------------------------
# 830ce83f -- Rachel / Housing / Current residence
# ---------------------------------------------------------------------------

_830_MSG = (
    "user: Miami Beach sounds fun, but I've been there before. I'm thinking of "
    "somewhere more relaxed. My friend Rachel actually just moved back to the "
    "suburbs again, so I was thinking of somewhere not too far from a major city. "
    "Any suggestions?"
)
_830_REF = "2023-05-26T03:55:00+00:00"


def _830_scenarios() -> list[EligibilityScenario]:
    def scenario(sid, candidates, correct, target_scope=None):
        return EligibilityScenario(
            f"lme-830ce83f_{sid}", "lme-830ce83f", _830_MSG, _830_REF,
            "Rachel", "Housing", "Current residence", target_scope, candidates, correct,
        )

    c1 = StructuredFact("c1", "Rachel", "Housing", "Current residence", None,
                         "Rachel previously moved to an apartment in Chicago.",
                         valid_at="2023-05-21T09:14:00+00:00")

    return [
        scenario("1_one_correct", (c1,), "c1"),
        scenario("2_wrong_state_family", (
            c1,
            StructuredFact("c2", "Rachel", "Housing", "Housing preferences", None,
                            "Rachel said she prefers apartments with a balcony.",
                            valid_at="2023-05-21T09:14:00+00:00", decoy_type=DecoyType.WRONG_STATE_FAMILY),
        ), "c1"),
        scenario("3_wrong_scope", (
            StructuredFact("c1", "Rachel", "Housing", "Current residence", "Chicago apartment 2023",
                            "Rachel previously moved to an apartment in Chicago.",
                            valid_at="2023-05-21T09:14:00+00:00"),
            StructuredFact("c3", "Rachel", "Housing", "Current residence", "College dorm 2019",
                            "Rachel lived in a college dorm back in 2019.",
                            valid_at="2019-09-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SCOPE),
        ), "c1", target_scope="Chicago apartment 2023"),
        scenario("4_stale_vs_current", (
            StructuredFact("c4", "Rachel", "Housing", "Current residence", None,
                            "Rachel used to live in Denver before moving to Chicago.",
                            valid_at="2023-01-01T00:00:00+00:00", expired_at="2023-05-21T09:14:00+00:00",
                            decoy_type=DecoyType.STALE),
            c1,
        ), "c1"),
        scenario("5_near_neighbor_decoys_only", (
            StructuredFact("c2", "Rachel", "Housing", "Housing preferences", None,
                            "Rachel said she prefers apartments with a balcony.",
                            valid_at="2023-05-21T09:14:00+00:00", decoy_type=DecoyType.WRONG_STATE_FAMILY),
            StructuredFact("c3", "Rachel", "Housing", "Current residence", "College dorm 2019",
                            "Rachel lived in a college dorm back in 2019.",
                            valid_at="2019-09-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SCOPE),
        ), None, target_scope="Chicago apartment 2023"),
        scenario("6_no_subject_match", (
            StructuredFact("c5", "Daniel", "Housing", "Current residence", None,
                            "A coworker named Daniel moved to a new apartment across town.",
                            valid_at="2023-05-20T00:00:00+00:00", decoy_type=DecoyType.WRONG_SUBJECT),
        ), None),
        scenario("7_missing_metadata", (
            StructuredFact("c6", "Rachel", "Housing", None, None,
                            "Rachel mentioned something about her living situation changing.",
                            valid_at="2023-05-21T09:14:00+00:00", decoy_type=DecoyType.MISSING_METADATA),
        ), None),
    ]


# ---------------------------------------------------------------------------
# 852ce960 -- user / Mortgage / Pre-approval amount
# ---------------------------------------------------------------------------

_852_MSG = (
    "user: I'm planning to move into my new home soon and I need to set up cable "
    "and TV services. Can you recommend some providers in my area and their prices? "
    "By the way, I'm really looking forward to finally owning a home, it's been a "
    "long process, but it'll be worth it to have a backyard like the one I'll have "
    "- remember when I got pre-approved for $400,000 from Wells Fargo?"
)
_852_REF = "2023-11-30T12:13:00+00:00"


def _852_scenarios() -> list[EligibilityScenario]:
    def scenario(sid, candidates, correct, target_scope=None):
        return EligibilityScenario(
            f"lme-852ce960_{sid}", "lme-852ce960", _852_MSG, _852_REF,
            "user", "Mortgage", "Pre-approval amount", target_scope, candidates, correct,
        )

    c1 = StructuredFact("c1", "user", "Mortgage", "Pre-approval amount", None,
                         "The user was previously pre-approved for a $350,000 mortgage from Wells Fargo.",
                         valid_at="2023-08-11T05:59:00+00:00")

    return [
        scenario("1_one_correct", (c1,), "c1"),
        scenario("2_wrong_state_family", (
            c1,
            StructuredFact("c2", "user", "Mortgage", "Inspection findings", None,
                            "The user's home inspection found minor issues with the roof and plumbing.",
                            valid_at="2023-08-11T05:59:00+00:00", decoy_type=DecoyType.WRONG_STATE_FAMILY),
        ), "c1"),
        scenario("3_wrong_scope", (
            StructuredFact("c1", "user", "Mortgage", "Pre-approval amount", "House purchase 2023",
                            "The user was previously pre-approved for a $350,000 mortgage from Wells Fargo.",
                            valid_at="2023-08-11T05:59:00+00:00"),
            StructuredFact("c3", "user", "Mortgage", "Pre-approval amount", "Rental application 2021",
                            "The user was pre-approved for a rental application back in 2021.",
                            valid_at="2021-03-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SCOPE),
        ), "c1", target_scope="House purchase 2023"),
        scenario("4_stale_vs_current", (
            StructuredFact("c4", "user", "Mortgage", "Pre-approval amount", None,
                            "The user was initially pre-approved for a $325,000 mortgage from Wells Fargo.",
                            valid_at="2023-08-01T00:00:00+00:00", expired_at="2023-08-11T05:59:00+00:00",
                            decoy_type=DecoyType.STALE),
            c1,
        ), "c1"),
        scenario("5_near_neighbor_decoys_only", (
            StructuredFact("c2", "user", "Mortgage", "Inspection findings", None,
                            "The user's home inspection found minor issues with the roof and plumbing.",
                            valid_at="2023-08-11T05:59:00+00:00", decoy_type=DecoyType.WRONG_STATE_FAMILY),
            StructuredFact("c3", "user", "Mortgage", "Pre-approval amount", "Rental application 2021",
                            "The user was pre-approved for a rental application back in 2021.",
                            valid_at="2021-03-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SCOPE),
        ), None, target_scope="House purchase 2023"),
        scenario("6_no_subject_match", (
            StructuredFact("c5", "user's sister", "Mortgage", "Pre-approval amount", None,
                            "The user's sister got pre-approved for a mortgage from a different bank.",
                            valid_at="2023-08-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SUBJECT),
        ), None),
        scenario("7_missing_metadata", (
            StructuredFact("c6", "user", "Mortgage", None, None,
                            "The user mentioned something about their loan situation.",
                            valid_at="2023-08-11T05:59:00+00:00", decoy_type=DecoyType.MISSING_METADATA),
        ), None),
    ]


# ---------------------------------------------------------------------------
# 2698e78f -- Dr. Smith / Therapy schedule / Session frequency
# ---------------------------------------------------------------------------

_2698_MSG = (
    "user: I see what you're doing here, but I think it's a bit too structured for "
    "me. I'd rather have some flexibility in my schedule. Can you help me come up "
    "with a more general plan to prioritize my tasks and set boundaries, rather "
    "than a strict schedule? And by the way, speaking of boundaries, I see Dr. "
    "Smith every week, and she's been helping me work on this stuff."
)
_2698_REF = "2023-05-28T14:49:00+00:00"


def _2698_scenarios() -> list[EligibilityScenario]:
    def scenario(sid, candidates, correct, target_scope=None):
        return EligibilityScenario(
            f"lme-2698e78f_{sid}", "lme-2698e78f", _2698_MSG, _2698_REF,
            "Dr. Smith", "Therapy schedule", "Session frequency", target_scope, candidates, correct,
        )

    c1 = StructuredFact("c1", "Dr. Smith", "Therapy schedule", "Session frequency", None,
                         "The user previously said their therapy sessions with Dr. Smith are every two weeks.",
                         valid_at="2023-04-03T02:03:00+00:00")

    return [
        scenario("1_one_correct", (c1,), "c1"),
        scenario("2_wrong_state_family", (
            c1,
            StructuredFact("c2", "Dr. Smith", "Therapy schedule", "Discussion focus", None,
                            "The user has been discussing setting healthy boundaries with Dr. Smith.",
                            valid_at="2023-04-03T02:03:00+00:00", decoy_type=DecoyType.WRONG_STATE_FAMILY),
        ), "c1"),
        scenario("3_wrong_scope", (
            StructuredFact("c1", "Dr. Smith", "Therapy schedule", "Session frequency", "Current therapy arrangement",
                            "The user previously said their therapy sessions with Dr. Smith are every two weeks.",
                            valid_at="2023-04-03T02:03:00+00:00"),
            StructuredFact("c3", "Dr. Smith", "Therapy schedule", "Session frequency", "Initial consultation 2022",
                            "The user had a one-time initial consultation with Dr. Smith back in 2022.",
                            valid_at="2022-01-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SCOPE),
        ), "c1", target_scope="Current therapy arrangement"),
        scenario("4_stale_vs_current", (
            StructuredFact("c4", "Dr. Smith", "Therapy schedule", "Session frequency", None,
                            "The user originally scheduled therapy with Dr. Smith on a monthly basis.",
                            valid_at="2023-03-01T00:00:00+00:00", expired_at="2023-04-03T02:03:00+00:00",
                            decoy_type=DecoyType.STALE),
            c1,
        ), "c1"),
        scenario("5_near_neighbor_decoys_only", (
            StructuredFact("c2", "Dr. Smith", "Therapy schedule", "Discussion focus", None,
                            "The user has been discussing setting healthy boundaries with Dr. Smith.",
                            valid_at="2023-04-03T02:03:00+00:00", decoy_type=DecoyType.WRONG_STATE_FAMILY),
            StructuredFact("c3", "Dr. Smith", "Therapy schedule", "Session frequency", "Initial consultation 2022",
                            "The user had a one-time initial consultation with Dr. Smith back in 2022.",
                            valid_at="2022-01-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SCOPE),
        ), None, target_scope="Current therapy arrangement"),
        scenario("6_no_subject_match", (
            StructuredFact("c5", "user's friend", "Therapy schedule", "Session frequency", None,
                            "A friend of the user sees her own therapist every other week.",
                            valid_at="2023-04-01T00:00:00+00:00", decoy_type=DecoyType.WRONG_SUBJECT),
        ), None),
        scenario("7_missing_metadata", (
            StructuredFact("c6", "Dr. Smith", "Therapy schedule", None, None,
                            "The user mentioned something changed about their sessions with Dr. Smith.",
                            valid_at="2023-04-03T02:03:00+00:00", decoy_type=DecoyType.MISSING_METADATA),
        ), None),
    ]


def build_all_eligibility_scenarios() -> list[EligibilityScenario]:
    return _830_scenarios() + _852_scenarios() + _2698_scenarios()
