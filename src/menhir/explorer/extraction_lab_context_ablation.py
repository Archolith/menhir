"""Phase 2 context-form ablation (menhir-extraction-context-ablation-handoff.md).

For each of the 3 real RCA fixtures, defines 8 distinct context-form conditions
(collapsed from the handoff's 10 -- see the note on H/I below) that vary WHAT context
the extractor sees for the identical current_message, isolating the "does context
content/form matter" question from Phase 1's "does model sampling variance matter"
question.

Condition catalog (per the handoff's spec):
  A - no context at all
  B - entity-name signal only (approximates the Phase 2 candidate lookup, but with a
      deterministic forced name instead of a live DB query -- see
      ExtractionLabTuning.forced_known_entities)
  C - the real, full establishing episode(s) from the actual conversation, delivered as
      an ordinary previous_episode (inside the simulated recency window)
  D - a compact, single-sentence version of the same fact
  E - an entity description with NO fact attached (tests whether mere "this entity
      exists" framing helps at all, independent of what it says about the entity)
  F - an unrelated episode of comparable length (required negative control)
  G - a lexically-similar-but-semantically-unrelated episode (tests false grounding
      from shared vocabulary, not shared facts)
  J - the SAME content as C, but delivered via the RETRIEVED CONTEXT block instead of
      as an ordinary previous_episode -- isolates delivery format from content

Conditions H and I (native Graphiti episode window vs. reconstructed raw-turn window)
are NOT implemented as distinct conditions here. Checked directly: the RCA fixtures'
previous_episodes were built from real :Episodic nodes queried out of the live LME
Neo4j graph (see extraction_lab_fixtures.py), and that query returned 23-48 nodes per
fixture -- roughly 1:1 with raw conversational turns, NOT the "~3 sub-episodes per raw
turn / 70+ total" expansion the original RCA (rca-lme-stale-fact-retention-2026-07-15.md)
described for a different, earlier ingest of the same conversations. With the data
actually available, H and I would produce IDENTICAL prompts, so implementing both would
silently fabricate a distinction the harness cannot actually test. Flagged here rather
than faked; H (production behavior, i.e. baseline arm with default context_episode_count)
is already covered by every earlier Extraction Lab phase, so nothing is lost by omitting
it as a ablation condition specifically -- only I's distinct "raw turn reconstruction"
claim is left genuinely untested, and should stay that way until fixture data with real
sub-episode expansion is available.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.explorer.extraction_lab import EpisodeFixture, ExtractionLabTuning
from menhir.explorer.extraction_lab_fixtures import (
    FIXTURE_RCA_830CE83F,
    FIXTURE_RCA_852CE960,
    FIXTURE_RCA_2698E78F,
)


@dataclass(frozen=True)
class AblationFixtureSpec:
    """Per-fixture content needed to build all 8 conditions."""

    qid: str
    current_message: str
    gold: object  # GoldExtraction, kept opaque here to avoid import cycles in typing
    target_entity_name: str  # for condition B (forced_known_entities)
    establishing_episode_indices: tuple[int, ...]  # into the fixture's previous_episodes, for C/J
    compact_fact: str  # condition D
    entity_description_no_fact: str  # condition E
    unrelated_episode_text: str  # condition F
    lexically_similar_episode_text: str  # condition G


_ABLATION_SPECS: dict[str, AblationFixtureSpec] = {
    "lme-830ce83f": AblationFixtureSpec(
        qid="lme-830ce83f",
        current_message=FIXTURE_RCA_830CE83F.current_message,
        gold=FIXTURE_RCA_830CE83F.gold,
        target_entity_name="Rachel",
        establishing_episode_indices=(2, 4),  # apartment-in-the-city, then Rachel-in-Chicago
        compact_fact="Rachel previously moved to Chicago.",
        entity_description_no_fact="Rachel is a person previously mentioned in the conversation.",
        unrelated_episode_text=(
            "assistant: For international travel, don't forget to check passport expiration "
            "dates and any visa requirements well in advance of your trip."
        ),
        lexically_similar_episode_text=(
            "user: My coworker Daniel is moving to a new apartment across the city next month, "
            "and he asked me to help him pack."
        ),
    ),
    "lme-852ce960": AblationFixtureSpec(
        qid="lme-852ce960",
        current_message=FIXTURE_RCA_852CE960.current_message,
        gold=FIXTURE_RCA_852CE960.gold,
        target_entity_name="Wells Fargo",
        establishing_episode_indices=(2,),
        compact_fact="The user was previously pre-approved for a $350,000 mortgage from Wells Fargo.",
        entity_description_no_fact=(
            "The user's mortgage pre-approval was previously mentioned in the conversation."
        ),
        unrelated_episode_text=(
            "assistant: When choosing a moving company, always check their reviews and confirm "
            "they are properly licensed and insured before signing a contract."
        ),
        lexically_similar_episode_text=(
            "user: My sister is also going through the process of buying a house right now and "
            "got pre-approved for a loan from a different bank."
        ),
    ),
    "lme-2698e78f": AblationFixtureSpec(
        qid="lme-2698e78f",
        current_message=FIXTURE_RCA_2698E78F.current_message,
        gold=FIXTURE_RCA_2698E78F.gold,
        target_entity_name="Dr. Smith",
        establishing_episode_indices=(2,),
        compact_fact="The user previously said their therapy sessions with Dr. Smith are every two weeks.",
        entity_description_no_fact=(
            "Dr. Smith was previously mentioned in the conversation as the user's therapist."
        ),
        unrelated_episode_text=(
            "assistant: If you're looking to improve your diet, consider meal prepping on "
            "Sundays to save time during the work week."
        ),
        lexically_similar_episode_text=(
            "user: My friend sees her therapist every other week and finds it really helpful "
            "for managing stress."
        ),
    ),
}


@dataclass(frozen=True)
class AblationCondition:
    """One (fixture, condition) test case: the resolved previous_episodes/tuning to run."""

    fixture_qid: str
    condition_id: str
    condition_label: str
    current_message: str
    gold: object
    previous_episodes: list[EpisodeFixture]
    tuning: ExtractionLabTuning


def _establishing_episode_fixtures(qid: str) -> list[EpisodeFixture]:
    """Real EpisodeFixture objects for the spec's establishing_episode_indices, pulled
    from the actual RCA fixture's previous_episodes (not re-authored text) -- condition
    C must be genuinely real content, not a paraphrase."""
    source = {
        "lme-830ce83f": FIXTURE_RCA_830CE83F,
        "lme-852ce960": FIXTURE_RCA_852CE960,
        "lme-2698e78f": FIXTURE_RCA_2698E78F,
    }[qid]
    spec = _ABLATION_SPECS[qid]
    return [source.previous_episodes[i] for i in spec.establishing_episode_indices]


def _establishing_episode_text(qid: str) -> str:
    """Concatenated real text of the establishing episode(s), for condition J's
    RETRIEVED CONTEXT block (same content as C, different delivery)."""
    return "\n\n".join(ep.text for ep in _establishing_episode_fixtures(qid))


CONTEXT_EPISODE_COUNT = 20  # generous headroom; conditions never supply more than 2 episodes


def build_conditions_for_fixture(qid: str) -> list[AblationCondition]:
    """Build all 8 ablation conditions for one RCA fixture qid (e.g. 'lme-830ce83f')."""
    spec = _ABLATION_SPECS[qid]
    base_tuning_kwargs = dict(prompt_variant="baseline", context_episode_count=CONTEXT_EPISODE_COUNT)

    conditions: list[AblationCondition] = []

    def add(condition_id: str, label: str, previous_episodes: list[EpisodeFixture], **tuning_overrides) -> None:
        conditions.append(
            AblationCondition(
                fixture_qid=qid,
                condition_id=condition_id,
                condition_label=label,
                current_message=spec.current_message,
                gold=spec.gold,
                previous_episodes=previous_episodes,
                tuning=ExtractionLabTuning(**{**base_tuning_kwargs, **tuning_overrides}),
            )
        )

    add("A_no_context", "A: no context", [])
    add(
        "B_entity_name",
        "B: entity-name signal only",
        [],
        forced_known_entities=[spec.target_entity_name],
    )
    add(
        "C_full_episode",
        "C: full relevant source episode",
        _establishing_episode_fixtures(qid),
    )
    add(
        "D_compact_fact",
        "D: compact relevant fact",
        [EpisodeFixture(text=spec.compact_fact)],
    )
    add(
        "E_entity_description",
        "E: entity description, no fact",
        [EpisodeFixture(text=spec.entity_description_no_fact)],
    )
    add(
        "F_unrelated",
        "F: unrelated episode (control)",
        [EpisodeFixture(text=spec.unrelated_episode_text)],
    )
    add(
        "G_lexically_similar",
        "G: lexically similar, semantically unrelated",
        [EpisodeFixture(text=spec.lexically_similar_episode_text)],
    )
    add(
        "J_retrieved_context",
        "J: retrieved context block (same content as C, different delivery)",
        [],
        retrieved_context=_establishing_episode_text(qid),
    )

    return conditions


def build_all_conditions() -> list[AblationCondition]:
    """All 8 conditions x all 3 real RCA fixtures = 24 (fixture, condition) cells."""
    all_conditions: list[AblationCondition] = []
    for qid in _ABLATION_SPECS:
        all_conditions.extend(build_conditions_for_fixture(qid))
    return all_conditions
