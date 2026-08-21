"""CF-192: both grounding guards on synthesized content were defeated by ordinary text.

These two guards stand between model-hallucinated content and a materialized KG entity or a durable
edge. Both admitted content the user never wrote, and the receipt reported `0` suppressed either
way -- so the failure reads as clean.

**(a) `_is_synthesizable_endpoint` matched bare substrings.** Its docstring requires the name to
"appear literally" in the episode; `normalized in text.casefold()` is not that. Executed against
`"user: I joined the channel yesterday"`:

    Ann   -> True   (inside "ch-ann-el")
    Chan  -> True   (inside "chan-nel")
    Ester -> True   (inside "y-ester-day")
    Yes   -> True   (inside "yes-terday")

and because previous-episode texts join the grounding set, the guard got WEAKER the more context the
extractor was given.

**(b) `_edge_has_current_message_anchor` counted `relation_type` as evidence.** That field is
model-supplied boilerplate -- the repair prompt instructs the model to emit relation labels. Against
`"user: Thanks, that helps me understand more."` an edge whose endpoints and fact were copied
entirely from prior context was admitted, matching on `more` from its own `WANTS_TO_KNOW_MORE_ABOUT`
label. An acknowledgement turn could persist a durable interest edge about an entity the user never
mentioned.

One thing worth recording about the (a) fix: it does not merely tighten the guard, it **repairs the
docstring's own worked example**. `Rachel` for "She moved to Chicago" -- the case the docstring says
must be admitted -- returned False under substring matching whenever the antecedent lived in a
previous episode. Token matching admits it.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.graphiti_extraction_patches import (
    _EDGE_ANCHOR_EVIDENCE_FIELDS,
    _edge_has_current_message_anchor,
    _is_synthesizable_endpoint,
)

pytestmark = pytest.mark.unit

EPISODE = "user: I joined the channel yesterday"


# ---------------------------------------------------------------------------
# (a) endpoint synthesis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "hidden_inside"),
    [
        ("Ann", "ch-ann-el"),
        ("Chan", "chan-nel"),
        ("Ester", "y-ester-day"),
        ("Yes", "yes-terday"),
        ("Anne", "ch-anne-l"),
        ("day", "yester-day"),
    ],
)
def test_a_name_hidden_inside_a_longer_word_is_not_grounding(name: str, hidden_inside: str) -> None:
    """The finding. Each of these was True and materialized a KG entity."""
    assert _is_synthesizable_endpoint(name, EPISODE) is False, (
        f"{name!r} admitted because it sits inside {hidden_inside!r}"
    )


@pytest.mark.parametrize("name", ["channel", "Channel", "CHANNEL", "yesterday", "joined"])
def test_a_whole_word_present_in_the_turn_is_still_grounding(name: str) -> None:
    """POSITIVE CONTROL, load-bearing: the guard must still ADMIT names the user actually wrote,
    case-insensitively. A guard that rejected everything would pass every test above."""
    assert _is_synthesizable_endpoint(name, EPISODE) is True


def test_the_docstrings_own_worked_example_now_works() -> None:
    """`Rachel` for "She moved to Chicago", resolved from a previous episode -- the case the
    docstring names as the one that must be admitted. Substring matching rejected it."""
    assert _is_synthesizable_endpoint(
        "Rachel", "user: She moved to Chicago", ("user: Rachel is my sister",)
    ) is True


def test_a_multi_word_name_matches_as_an_ordered_phrase() -> None:
    text = "user: we discussed the service mesh today"

    assert _is_synthesizable_endpoint("Service Mesh", text) is True
    # Not a bag of words: the tokens must appear contiguously and in order.
    assert _is_synthesizable_endpoint("Mesh Service", text) is False


def test_a_name_absent_from_every_grounding_text_is_refused() -> None:
    assert _is_synthesizable_endpoint("Zebra", EPISODE) is False
    assert _is_synthesizable_endpoint("Zebra", EPISODE, ("user: unrelated context",)) is False


def test_previous_episode_text_still_grounds() -> None:
    """The feature the grounding set exists for -- an antecedent resolved from earlier context.
    Tightening the match must not remove it."""
    assert _is_synthesizable_endpoint(
        "Chicago", "user: She moved there", ("user: I visited Chicago last year",)
    ) is True


def test_with_no_grounding_text_at_all_the_guard_stays_permissive() -> None:
    """Unchanged pre-existing behaviour: when the extractor supplied no grounding text there is
    nothing to check against, and the function returns True. Pinned so the fix is visibly scoped to
    the case where grounding EXISTS."""
    assert _is_synthesizable_endpoint("Anything", "") is True


# ---------------------------------------------------------------------------
# (b) edge anchoring
# ---------------------------------------------------------------------------

ACK_TURN = "user: Thanks, that helps me understand more."


def _edge(**over: str) -> dict[str, str]:
    base = {
        "source_entity_name": "Kubernetes",
        "target_entity_name": "Service Mesh",
        "relation_type": "WANTS_TO_KNOW_MORE_ABOUT",
        "fact": "user is interested in service mesh",
    }
    base.update(over)
    return base


def test_an_edge_grounded_only_by_its_own_relation_type_is_refused() -> None:
    """The finding. Every content field is copied from prior context; the ONLY overlap with the
    current turn is `more`, inside the model's own relation label."""
    assert _edge_has_current_message_anchor(_edge(), ACK_TURN) is False


def test_relation_type_is_not_in_the_evidence_fields() -> None:
    """Structural pin. The exclusion is the fix; a future edit re-adding the field would restore
    the defect while the behavioural test above might still pass on other wording."""
    assert "relation_type" not in _EDGE_ANCHOR_EVIDENCE_FIELDS
    assert set(_EDGE_ANCHOR_EVIDENCE_FIELDS) == {
        "source_entity_name", "target_entity_name", "fact",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fact", "the docs helps the user"),
        ("source_entity_name", "understand"),
        ("target_entity_name", "understand"),
    ],
)
def test_an_edge_grounded_by_real_content_is_still_admitted(field: str, value: str) -> None:
    """POSITIVE CONTROL: the guard must still pass edges that genuinely share a token with the
    turn, through any of the three CONTENT fields. Without this, dropping every field would pass."""
    assert _edge_has_current_message_anchor(_edge(**{field: value}), ACK_TURN) is True


def test_a_turn_with_no_anchor_tokens_grounds_nothing() -> None:
    """Unchanged: a turn that yields no meaningful tokens cannot ground any edge."""
    assert _edge_has_current_message_anchor(_edge(fact="the docs helps"), "user: ok") is False


def test_matching_is_exact_not_stemmed() -> None:
    """Recorded rather than assumed: `understands` does not match the anchor `understand`. That is
    the pre-existing contract of this token set, and this fix does not change it -- but it is the
    kind of thing a reader will otherwise trip over when reading the positive controls."""
    assert _edge_has_current_message_anchor(
        _edge(fact="user now understands kubernetes"), ACK_TURN
    ) is False
