from __future__ import annotations

import pytest

from menhir.domain import merge_delta as md
from menhir.domain.utils import effective_authority, source_confidence_for

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_UNSET = object()


def _node(
    *,
    summary: str | None = None,
    content: str | None = None,
    source: str | None = None,
    source_confidence: float | None = None,
    sources: object = _UNSET,
) -> dict:
    """A pre-merge node. ``sources`` is OMITTED unless given -- a node that never had the list is a
    different case from one whose list is explicitly empty, and the derivation must tell them apart.
    """
    node = {
        "summary": summary,
        "content": content,
        "source": source,
        "source_confidence": source_confidence,
    }
    if sources is not _UNSET:
        node["sources"] = sources
    return node


# ---------------------------------------------------------------------------
# replay_survivor_merge
# ---------------------------------------------------------------------------

class TestReplaySurvivorMerge:
    """replay_survivor_merge(survivor, absorbed)"""

    def test_richer_absorbed_summary_wins(self):
        survivor = _node(
            summary="short",
            content="c",
            source="a",
            source_confidence=0.5,
        )
        absorbed = _node(
            summary="a very much longer absorbed summary indeed",
            content="x",
            source="b",
            source_confidence=0.9,
        )
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["summary"] == "a very much longer absorbed summary indeed"

    def test_survivor_summary_kept_when_absorbed_is_not_richer(self):
        survivor = _node(summary="a reasonably long survivor summary")
        absorbed = _node(summary="tiny")
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["summary"] == survivor["summary"]

    def test_content_only_replaced_when_20_percent_longer(self):
        survivor = _node(content="short", source_confidence=0.5)
        # (a) absorbed content much longer -> absorbed wins
        absorbed_long = _node(content="this is a substantially longer piece of content indeed")
        result = md.replay_survivor_merge(survivor, absorbed_long)
        assert result["content"] == absorbed_long["content"]

        # (b) absorbed content shorter or similar -> survivor keeps own
        absorbed_short = _node(content="tiny")
        result = md.replay_survivor_merge(survivor, absorbed_short)
        assert result["content"] == survivor["content"]

    def test_confidence_is_the_lowest_contributor_tier_not_an_increment(self):
        """REPLACES test_source_confidence_bumps_by_one_tenth, which asserted the defect: the merge
        added 0.1 to the survivor's confidence per absorption, so trust rose with the number of
        duplicates a node happened to attract. Confidence is now read off the contributors."""
        survivor = _node(source="claude-code")          # tier 0.7
        absorbed = _node(source="codex")                # tier 0.5
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source_confidence"] == 0.5       # the LOWEST, not 0.8

    def test_repeated_merges_never_raise_confidence(self):
        """The defect's real teeth: four absorptions took a claude-code entity (0.7) to 1.0, the tier
        reserved for the user's own statements. min() cannot do that at any merge count."""
        survivor = _node(source="claude-code")
        for _ in range(10):
            result = md.replay_survivor_merge(survivor, _node(source="claude-code"))
            survivor = {**survivor, **result}
        assert survivor["source_confidence"] == 0.7
        assert survivor["corroboration"] == 1           # one writer repeating itself

    def test_source_confidence_null_becomes_agent_tier(self):
        survivor = _node(source=None, source_confidence=None)
        absorbed = _node(source=None)
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source_confidence"] == 0.5

    def test_sources_are_unioned_into_a_list(self):
        """`source` is no longer an append log. The full contributor set moves to `sources`, and
        `source` holds the single label that carries the authority."""
        survivor = _node(source="claude-code")
        absorbed = _node(source="codex")
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["sources"] == ["claude-code", "codex"]
        assert result["source"] == "codex"              # lowest tier carries it
        assert result["source_confidence"] == source_confidence_for(result["source"])

    def test_corroboration_counts_families_not_labels(self):
        """`claude-code` and `claude-chat` are one writer wearing two hats. Counting labels would let
        a single source corroborate itself by being renamed."""
        survivor = _node(source="claude-code")
        absorbed = _node(source="claude-chat")
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["sources"] == ["claude-code", "claude-chat"]
        assert result["corroboration"] == 1

        result = md.replay_survivor_merge(_node(source="claude-code"), _node(source="codex"))
        assert result["corroboration"] == 2

    def test_the_authority_invariant_holds_after_merge(self):
        """`source` names the CEILING that binds the merged node, so the confidence never exceeds it.

        Deliberately an inequality, not the equality this once asserted. A node may be stamped BELOW
        its label's tier on purpose (structure_queries writes an inferred import target as
        `project-scan` at agent tier), and a merge that "restored" it to the ceiling would promote an
        inference to scanned ground truth. See test_an_explicit_downgrade_survives_a_merge.
        """
        for a, b in [("claude-code", "codex"), ("project-scan", "claude-code"),
                     ("user", "opencode"), ("codex", "codex")]:
            result = md.replay_survivor_merge(_node(source=a), _node(source=b))
            assert result["source_confidence"] <= source_confidence_for(result["source"])
            # With no observed confidence on either input, the ceiling IS the answer.
            assert result["source_confidence"] == source_confidence_for(result["source"])

    def test_a_prior_sources_list_is_carried_forward(self):
        survivor = _node(source="claude-code")
        survivor["sources"] = ["claude-code", "project-scan"]
        result = md.replay_survivor_merge(survivor, _node(source="codex"))
        assert result["sources"] == ["claude-code", "project-scan", "codex"]
        assert result["corroboration"] == 3

    def test_same_source_is_not_duplicated(self):
        survivor = _node(source="codex")
        absorbed = _node(source="codex")
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source"] == "codex"

    def test_empty_survivor_source_takes_absorbed(self):
        survivor = _node(source=None)
        absorbed = _node(source="codex")
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source"] == "codex"

        survivor = _node(source="")
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source"] == "codex"

    def test_empty_both_sources_becomes_merged(self):
        survivor = _node(source=None)
        absorbed = _node(source=None)
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source"] == "merged"


# ---------------------------------------------------------------------------
# effective authority: the observed value, not the label ceiling
# ---------------------------------------------------------------------------

class TestEffectiveAuthority:
    """A merge may never RAISE trust -- not even back up to what the labels would allow.

    Deriving authority from `source_confidence_for(label)` alone treats the tier table as proof of a
    node's confidence. It is only a ceiling: a writer may deliberately stamp lower (structure_queries
    writes an INFERRED import target as `source='project-scan'` at agent tier, not the structural
    tier the label permits). Recomputing from the label discards that downgrade and promotes an
    inference to scanned ground truth -- the same class of defect as the `+0.1` this replaced, just
    reached from the other side.
    """

    def test_an_explicit_downgrade_survives_a_merge(self):
        """THE regression. project-scan's ceiling is 0.9; both nodes are explicitly held at 0.5."""
        survivor = _node(source="project-scan", source_confidence=0.5)
        absorbed = _node(source="project-scan", source_confidence=0.5)
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source_confidence"] == 0.5
        assert result["source"] == "project-scan"
        assert source_confidence_for("project-scan") == 0.9, "precondition: the ceiling is higher"

    def test_repeated_merges_never_lift_a_downgraded_node(self):
        survivor = _node(source="project-scan", source_confidence=0.5)
        for _ in range(10):
            result = md.replay_survivor_merge(
                survivor, _node(source="project-scan", source_confidence=0.5)
            )
            survivor = {**survivor, **result}
        assert survivor["source_confidence"] == 0.5

    def test_the_weaker_of_the_two_observed_values_binds(self):
        survivor = _node(source="project-scan", source_confidence=0.9)
        absorbed = _node(source="project-scan", source_confidence=0.5)
        assert md.replay_survivor_merge(survivor, absorbed)["source_confidence"] == 0.5
        # ...and symmetrically, whichever side carries it.
        assert md.replay_survivor_merge(absorbed, survivor)["source_confidence"] == 0.5

    def test_the_label_ceiling_still_caps_inflated_legacy_confidence(self):
        """The pre-fix `+0.1` per absorption walked claude-code nodes (0.7) to 1.0 in production.
        Trusting the stored value alone would preserve that inflation forever."""
        survivor = _node(source="claude-code", source_confidence=1.0)
        absorbed = _node(source="claude-code", source_confidence=1.0)
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source_confidence"] == source_confidence_for("claude-code") == 0.7

    def test_authority_never_exceeds_any_contributing_label_ceiling(self):
        """user is the apex tier (1.0); merging it with a downweighted family cannot stay there."""
        survivor = _node(source="user", source_confidence=1.0)
        absorbed = _node(source="gemini-cli", source_confidence=0.3)
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["source_confidence"] == 0.3
        for label in result["sources"]:
            assert result["source_confidence"] <= source_confidence_for(label)

    def test_the_three_bounds_hold_for_every_combination(self):
        """merged <= survivor effective, merged <= absorbed effective, merged <= every ceiling."""
        labels = ["user", "project-scan", "claude-code", "codex", "gemini-cli", None]
        observed = [None, 0.0, 0.2, 0.5, 1.0, -0.25, 2.0, "not-a-number"]
        for s_label in labels:
            for a_label in labels:
                for s_conf in observed:
                    for a_conf in observed:
                        survivor = _node(source=s_label, source_confidence=s_conf)
                        absorbed = _node(source=a_label, source_confidence=a_conf)
                        got = md.replay_survivor_merge(survivor, absorbed)["source_confidence"]
                        assert got <= effective_authority(
                            md.contributors_of(survivor), survivor["source_confidence"])
                        assert got <= effective_authority(
                            md.contributors_of(absorbed), absorbed["source_confidence"])
                        for label in md.merged_contributors(survivor, absorbed):
                            assert got <= source_confidence_for(label)

    @pytest.mark.parametrize(
        "bad", [None, "0.5", float("nan"), float("inf"), float("-inf"), True, object()]
    )
    def test_a_malformed_confidence_falls_back_to_the_ceiling(self, bad):
        """Conservative, not permissive: an unusable stored value means 'unknown', and unknown is
        answered by the label ceiling (agent default when there are no labels at all)."""
        assert effective_authority(["claude-code"], bad) == 0.7
        assert effective_authority([], bad) == 0.5

    @pytest.mark.parametrize("value", [0.0, 0.1, 0.3, 0.5, 0.7, 1.0])
    def test_values_inside_the_ladders_domain_are_used(self, value):
        assert effective_authority(["claude-code"], value) == min(value, 0.7)

    def test_the_domain_boundaries_themselves_are_valid_observations(self):
        """0.0 and SOURCE_CONFIDENCE_USER are IN the domain -- an exclusive bound would silently
        promote a node explicitly stamped at zero all the way back to its ceiling."""
        assert effective_authority(["user"], 0.0) == 0.0
        assert effective_authority(["user"], 1.0) == 1.0
        assert effective_authority(["user"], -0.0) == 0.0

    @pytest.mark.parametrize("value", [-0.25, -1e-9, -1.0, -100.0, 1.0000001, 1.5, 2.0, 100.0])
    def test_a_finite_but_out_of_range_confidence_takes_the_malformed_path(self, value):
        """A number outside 0.0..1.0 is corruption, not a downgrade, and `min` would happily accept
        it: a stored -0.25 becomes the merged node's authority and lands BELOW every tier on the
        ladder, under any threshold defined against it. It carries no information, so the ceiling
        answers -- the same policy a non-numeric value gets."""
        assert effective_authority(["claude-code"], value) == 0.7
        assert effective_authority(["user"], value) == 1.0
        assert effective_authority([], value) == 0.5

    def test_a_corrupt_confidence_never_reaches_the_merged_node(self):
        below = md.replay_survivor_merge(
            _node(source="claude-code", source_confidence=-0.25),
            _node(source="claude-code", source_confidence=0.7),
        )
        assert below["source_confidence"] == 0.7

        above = md.replay_survivor_merge(
            _node(source="claude-code", source_confidence=5.0),
            _node(source="claude-code", source_confidence=0.7),
        )
        assert above["source_confidence"] == 0.7, "above the domain is capped, never trusted"


# ---------------------------------------------------------------------------
# contributor normalization: lossless, and never synthetic
# ---------------------------------------------------------------------------

class TestContributorNormalization:

    def test_a_prior_sources_list_on_the_ABSORBED_node_is_carried_forward(self):
        """An already-merged node absorbed by a THIRD node used to lose every contributor but one:
        the derivation read only its `source`, which by construction holds just the lowest-tier
        label. `project-scan` here would silently vanish from the graph's provenance."""
        survivor = _node(source="codex")
        absorbed = _node(
            source="claude-code", sources=["claude-code", "project-scan"], source_confidence=0.7
        )
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["sources"] == ["codex", "claude-code", "project-scan"]
        assert result["corroboration"] == 3

    def test_contributor_order_is_deterministic_and_duplicates_are_removed(self):
        survivor = _node(source="codex", sources=["codex", "claude-code"])
        absorbed = _node(source="claude-code", sources=["claude-code", "project-scan", "codex"])
        result = md.replay_survivor_merge(survivor, absorbed)
        assert result["sources"] == ["codex", "claude-code", "project-scan"]
        # Survivor positions are stable; only genuinely new labels append, in absorbed order.
        assert md.replay_survivor_merge(survivor, absorbed)["sources"] == result["sources"]

    def test_a_missing_sources_property_falls_back_to_the_legacy_source_string(self):
        """Nodes written before the list existed must merge correctly without being migrated."""
        assert md.contributors_of({"source": "codex,project-scan"}) == ["codex", "project-scan"]

    def test_an_explicitly_empty_sources_list_does_not_fall_back(self):
        """`sources: []` is a DERIVED fact the merge itself wrote -- this node has no contributors.
        Falling back to `source` there reads the `merged` placeholder back as provenance."""
        assert md.contributors_of({"sources": [], "source": "merged"}) == []
        assert md.contributors_of({"sources": [], "source": "codex"}) == []

    def test_the_synthetic_merged_label_is_never_a_contributor(self):
        assert md.contributors_of({"source": "merged"}) == []
        assert md.contributors_of({"sources": ["merged", "codex"]}) == ["codex"]

    def test_source_less_merge_chains_never_manufacture_provenance(self):
        """Chained regression. Merge 1 stamps source='merged'; without the exclusions above, merge 2
        reads that placeholder back as a real writer and the node claims a corroborating source that
        never existed."""
        survivor = _node(source=None)
        for _ in range(5):
            result = md.replay_survivor_merge(survivor, _node(source=None))
            assert result["sources"] == []
            assert result["source"] == "merged"
            assert result["corroboration"] == 0
            assert result["source_confidence"] == 0.5      # agent default, not raised
            survivor = {**survivor, **result}

    def test_same_family_labels_count_once_independent_families_count_separately(self):
        one_writer = md.replay_survivor_merge(
            _node(source="claude-code", sources=["claude-code", "claude-chat"]),
            _node(source="codex", sources=["codex", "codex-gpt-5"]),
        )
        assert one_writer["sources"] == ["claude-code", "claude-chat", "codex", "codex-gpt-5"]
        assert one_writer["corroboration"] == 2           # claude + codex, not four labels


# ---------------------------------------------------------------------------
# survivor_matches_merge_output
# ---------------------------------------------------------------------------

class TestSurvivorMatchesMergeOutput:
    """survivor_matches_merge_output(current, survivor_pre, absorbed_pre)"""

    def test_matches_when_current_equals_replayed_output(self):
        s = _node(summary="s", content="c", source="a", source_confidence=0.5)
        a = _node(summary="longer absorbed summary here", content="x", source="b", source_confidence=0.9)
        expected = md.replay_survivor_merge(s, a)
        matches, diffs = md.survivor_matches_merge_output(expected, s, a)
        assert matches is True
        assert diffs == {}

    def test_detects_a_changed_summary(self):
        s = _node(summary="s", content="c", source="a", source_confidence=0.5)
        a = _node(summary="some longer absorbed summary for this test", content="x", source="b", source_confidence=0.9)
        expected = md.replay_survivor_merge(s, a)
        current = dict(expected)
        current["summary"] = "someone edited this"
        matches, diffs = md.survivor_matches_merge_output(current, s, a)
        assert matches is False
        assert "summary" in diffs
        assert diffs["summary"]["expected"] == expected["summary"]
        assert diffs["summary"]["actual"] == "someone edited this"

    def test_float_confidence_compares_with_tolerance(self):
        s = _node(summary="s", source_confidence=0.5)
        a = _node(summary="a longer absorbed summary here", source_confidence=0.9)
        expected = md.replay_survivor_merge(s, a)
        current = dict(expected)
        current["source_confidence"] = expected["source_confidence"] + 1e-12
        matches, diffs = md.survivor_matches_merge_output(current, s, a)
        assert matches is True

    def test_detects_a_real_confidence_change(self):
        s = _node(summary="s", source_confidence=0.5)
        a = _node(summary="a longer absorbed summary here", source_confidence=0.9)
        expected = md.replay_survivor_merge(s, a)
        current = dict(expected)
        current["source_confidence"] = expected["source_confidence"] + 0.2
        matches, diffs = md.survivor_matches_merge_output(current, s, a)
        assert matches is False
        assert "source_confidence" in diffs


# ---------------------------------------------------------------------------
# restorable_survivor_properties
# ---------------------------------------------------------------------------

class TestRestorableSurvivorProperties:
    """restorable_survivor_properties(survivor_pre)"""

    def test_restorable_properties_returns_only_merge_owned_keys(self):
        survivor = _node(
            summary="s",
            content="c",
            source="a",
            source_confidence=0.5,
        )
        # Add lineage keys that should NOT appear in the result
        survivor["name"] = "some-name"
        survivor["merged_from"] = ["node-1"]

        result = md.restorable_survivor_properties(survivor)

        assert set(result.keys()) == set(md.MERGE_OWNED_SURVIVOR_PROPERTIES)
        assert "name" not in result
        assert "merged_from" not in result
