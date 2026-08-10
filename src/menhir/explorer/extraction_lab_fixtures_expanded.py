"""Expanded synthetic fixture set for Phase 1 statistical confidence.

20 additional hand-authored fixtures, same 7 ambiguity-pattern categories as
extraction_lab_fixtures.py's "required test messages" (direct named update, pronoun
update, informal location, corrected monetary value, reversal, generic non-fact,
unsupported implication), varied by subject/value so the baseline-vs-update_aware
comparison isn't riding on 7 templates alone. Combined with the original 10 fixtures
(extraction_lab_fixtures.ALL_FIXTURES), this gives 30 total.

Synthetic by design, same as the "required test messages" they extend -- these are
NOT real conversation excerpts (unlike the 3 RCA fixtures, which must stay real; see
extraction_lab_fixtures.py's module docstring). Fabricating evaluation probes for a
known ambiguity pattern is the intended use of this category.
"""

from menhir.explorer.extraction_lab import (
    ExtractionLabArm,
    ExtractionLabRequest,
    ExtractionLabTuning,
    GoldExtraction,
)

_PLACEHOLDER_ARMS = [
    ExtractionLabArm(id="baseline", label="Baseline", tuning=ExtractionLabTuning(prompt_variant="baseline")),
]

# ---------------------------------------------------------------------------
# Direct named update (5)
# ---------------------------------------------------------------------------

FIXTURE_E1 = ExtractionLabRequest(
    current_message="Marcus actually just switched back to the night shift again.",
    gold=GoldExtraction(
        mentions=["Marcus", "night shift"],
        propositions=["Marcus works or is scheduled on the night shift"],
        update_language=["actually", "switched back", "again"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E2 = ExtractionLabRequest(
    current_message="Priya actually just moved back in with her parents again.",
    gold=GoldExtraction(
        mentions=["Priya", "her parents"],
        propositions=["Priya moved back in with her parents"],
        update_language=["actually", "moved back", "again"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E3 = ExtractionLabRequest(
    current_message="Devon actually just switched back to the day shift again.",
    gold=GoldExtraction(
        mentions=["Devon", "day shift"],
        propositions=["Devon works or is scheduled on the day shift"],
        update_language=["actually", "switched back", "again"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E4 = ExtractionLabRequest(
    current_message="Elena actually just went back to freelancing again.",
    gold=GoldExtraction(
        mentions=["Elena", "freelancing"],
        propositions=["Elena currently works as a freelancer"],
        update_language=["actually", "went back", "again"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E5 = ExtractionLabRequest(
    current_message="Tom actually just switched back to the old apartment again.",
    gold=GoldExtraction(
        mentions=["Tom", "the old apartment"],
        propositions=["Tom currently lives in the old apartment"],
        update_language=["actually", "switched back", "again"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

# ---------------------------------------------------------------------------
# Pronoun update (3)
# ---------------------------------------------------------------------------

FIXTURE_E6 = ExtractionLabRequest(
    current_message="He actually just switched back to the night shift again.",
    gold=GoldExtraction(mentions=[], propositions=[], update_language=["actually", "switched back", "again"]),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E7 = ExtractionLabRequest(
    current_message="They actually just moved back in with their parents again.",
    gold=GoldExtraction(mentions=[], propositions=[], update_language=["actually", "moved back", "again"]),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E8 = ExtractionLabRequest(
    current_message="She actually just went back to freelancing again.",
    gold=GoldExtraction(mentions=[], propositions=[], update_language=["actually", "went back", "again"]),
    arms=_PLACEHOLDER_ARMS,
)

# ---------------------------------------------------------------------------
# Informal location (3)
# ---------------------------------------------------------------------------

FIXTURE_E9 = ExtractionLabRequest(
    current_message="Sofia is living uptown now.",
    gold=GoldExtraction(mentions=["Sofia", "uptown"], propositions=["Sofia currently lives uptown"], update_language=["now"]),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E10 = ExtractionLabRequest(
    current_message="Andre is working out of the west side office now.",
    gold=GoldExtraction(
        mentions=["Andre", "the west side office"],
        propositions=["Andre currently works out of the west side office"],
        update_language=["now"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E11 = ExtractionLabRequest(
    current_message="Lena is staying at her sister's place now.",
    gold=GoldExtraction(
        mentions=["Lena", "her sister's place"],
        propositions=["Lena is currently staying at her sister's place"],
        update_language=["now"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

# ---------------------------------------------------------------------------
# Corrected monetary value (3)
# ---------------------------------------------------------------------------

FIXTURE_E12 = ExtractionLabRequest(
    current_message="Actually, I paid $2,800 for the repair, not $2,200.",
    gold=GoldExtraction(
        mentions=["user", "$2,800", "$2,200"],
        propositions=["current repair cost is $2,800", "rejected prior value $2,200"],
        update_language=["Actually", "not"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E13 = ExtractionLabRequest(
    current_message="Actually, my rent is $1,950, not $1,700.",
    gold=GoldExtraction(
        mentions=["user", "$1,950", "$1,700"],
        propositions=["current rent is $1,950", "rejected prior value $1,700"],
        update_language=["Actually", "not"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E14 = ExtractionLabRequest(
    current_message="Actually, the quote came in at $6,500, not $5,000.",
    gold=GoldExtraction(
        mentions=["user", "$6,500", "$5,000"],
        propositions=["current quote is $6,500", "rejected prior value $5,000"],
        update_language=["Actually", "not"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

# ---------------------------------------------------------------------------
# Reversal (3)
# ---------------------------------------------------------------------------

FIXTURE_E15 = ExtractionLabRequest(
    current_message="Priya no longer works at Meta; she joined Anthropic.",
    gold=GoldExtraction(
        mentions=["Priya", "Meta", "Anthropic"],
        propositions=["Priya no longer works at Meta", "Priya works at Anthropic"],
        update_language=["no longer", "joined"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E16 = ExtractionLabRequest(
    current_message="Devon no longer trains at Gold's Gym; he joined the community rec center.",
    gold=GoldExtraction(
        mentions=["Devon", "Gold's Gym", "the community rec center"],
        propositions=["Devon no longer trains at Gold's Gym", "Devon trains at the community rec center"],
        update_language=["no longer", "joined"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E17 = ExtractionLabRequest(
    current_message="Elena no longer banks with Chase; she switched to a local credit union.",
    gold=GoldExtraction(
        mentions=["Elena", "Chase", "a local credit union"],
        propositions=["Elena no longer banks with Chase", "Elena banks with a local credit union"],
        update_language=["no longer", "switched"],
    ),
    arms=_PLACEHOLDER_ARMS,
)

# ---------------------------------------------------------------------------
# Generic non-fact (2)
# ---------------------------------------------------------------------------

FIXTURE_E18 = ExtractionLabRequest(
    current_message="Job hunting is exhausting and demoralizing.",
    gold=GoldExtraction(mentions=[], propositions=[], update_language=[]),
    arms=_PLACEHOLDER_ARMS,
)

FIXTURE_E19 = ExtractionLabRequest(
    current_message="Renovating a house is a lot more stressful than people expect.",
    gold=GoldExtraction(mentions=[], propositions=[], update_language=[]),
    arms=_PLACEHOLDER_ARMS,
)

# ---------------------------------------------------------------------------
# Unsupported implication (1)
# ---------------------------------------------------------------------------

FIXTURE_E20 = ExtractionLabRequest(
    current_message="Marcus has been filling out job applications all week.",
    gold=GoldExtraction(mentions=["Marcus"], propositions=[], update_language=[]),
    arms=_PLACEHOLDER_ARMS,
)

EXPANDED_FIXTURES = [
    FIXTURE_E1, FIXTURE_E2, FIXTURE_E3, FIXTURE_E4, FIXTURE_E5,
    FIXTURE_E6, FIXTURE_E7, FIXTURE_E8,
    FIXTURE_E9, FIXTURE_E10, FIXTURE_E11,
    FIXTURE_E12, FIXTURE_E13, FIXTURE_E14,
    FIXTURE_E15, FIXTURE_E16, FIXTURE_E17,
    FIXTURE_E18, FIXTURE_E19,
    FIXTURE_E20,
]
