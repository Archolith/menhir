"""The B/D paired-trio contract, verified offline.

B and D are the SAME three LLM calls scored two ways: B groups proposals by exact span offsets, D
groups them by span overlap. Running the trio once and scoring it twice is both cheaper (12 -> 9
calls per namespace-trial) and better science -- the B-vs-D contrast becomes paired, so it is not
contaminated by two independent draws at temp=0.7, whose measured swing (16 -> 12 CURRENT on
identical input) is the same magnitude as the effect being tested.

That sharing introduces a specific failure mode this file exists to prevent: the probe now runs the
trio under the internal label "BD", and every downstream consumer keys on the arm name. Anything
still writing `stats["BD"]` or `arm: "BD"` silently vanishes from both the report and the gate
scorer -- the same class of capture gap that already cost three live panels today.

No network, no database, no API spend.
"""

from __future__ import annotations

import os
import re


def _probe_src() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                        "probe_scalar_proposer_reviewer.py")
    return open(os.path.normpath(path), encoding="utf-8").read()


def test_bd_trio_is_planned_once_not_twice():
    """The whole point of the pairing: one proposer+reviewer trio serves both arms."""
    src = _probe_src()
    assert 'arm_plan' in src, "no arm plan -- B and D would each spend their own trio"
    assert '"BD"' in src, "shared trio label absent"


def test_no_stat_or_capture_row_escapes_under_the_bd_label():
    """`BD` is an internal scheduling label. If it reaches stats or the capture, the arm's numbers
    disappear from the report and from every gate -- silently, and only discoverable after the
    calls are spent."""
    src = _probe_src()

    # every arm_commit emit must stamp a real arm
    assert 'row["arm"] = real_arm' in src, (
        "arm_commit rows must be re-stamped with the real arm; a row labelled BD is invisible "
        "to the gate scorer")
    # decision rows must be re-stamped too
    assert '{**ctx, "arm": ra}' in src, (
        "decision rows must be re-stamped per real arm, or decoy stats vanish for B and D")
    # stats writes inside the shared branch must fan out to real arms
    assert "real_arms = [" in src, "shared-trio stats must fan out to the real arms"


def test_drops_fan_out_to_both_shared_arms():
    """Parse drops are attributed per arm; under a shared trio they belong to both."""
    src = _probe_src()
    drop_fn = src[src.index("def _pdrop("):src.index("cards = propose_claim_cards")]
    assert '["B", "D"]' in drop_fn, "drop counter still keys on the BD label"


def test_scored_populations_are_emitted_for_every_arm():
    """Each arm's SCORED population must reach the capture -- A from the baseline path, B and D
    from the shared trio, C from its card decisions. An arm whose commits are counted but not
    captured leaves its gates uncomputable after the run."""
    src = _probe_src()
    assert src.count('"type": "arm_commit"') >= 3, (
        "expected arm_commit emits on the baseline path, the shared B/D path, and the C path; "
        "a missing one is exactly the gap that made gates 2 and 3 uncomputable")


def test_both_arms_scored_from_the_shared_trio():
    """B and D must BOTH be scored off the shared per_sample, and each honour --arms."""
    src = _probe_src()
    shared = src[src.index('if arm == "BD":'):src.index("            print(f\"  done")]
    assert 'if "B" in args.arms:' in shared, "B not scored from the shared trio"
    assert 'if "D" in args.arms:' in shared, "D not scored from the shared trio"
    assert "gate_typed_scalars(per_sample" in shared, "B must use the exact-span gate"
    assert "align(per_sample)" in shared, "D must use deterministic alignment"


def test_align_groups_only_across_samples_and_requires_common_intersection():
    """The grouping rule itself, exercised directly rather than asserted about.

    Two guards matter and both are easy to lose in a refactor: never group two readings from the
    SAME sample (that is the conflicted signal, not agreement), and require a COMMON intersection
    rather than chained pairwise overlap (chaining drags non-overlapping A and C together via B).
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
    from probe_scalar_proposer_reviewer import align

    class P:
        def __init__(self, s, e, ep="e1"):
            self.span_start, self.span_end, self.episode_uuid = s, e, ep

    # chain: A[0,10) B[8,20) C[18,30) -- A and C do NOT overlap, so this must NOT become one head
    heads = align([[P(0, 10)], [P(8, 20)], [P(18, 30)]])
    assert all(len(h) < 3 for h in heads), "chained overlap collapsed non-overlapping spans"

    # genuine common intersection across three samples -> one head
    heads = align([[P(0, 20)], [P(5, 20)], [P(10, 20)]])
    assert any(len(h) == 3 for h in heads), "common intersection failed to group"

    # two readings in ONE sample must never group together
    heads = align([[P(0, 20), P(5, 20)], []])
    assert all(len(h) == 1 for h in heads), "grouped two readings from the same sample"

    # different episodes never group
    heads = align([[P(0, 20, "e1")], [P(0, 20, "e2")]])
    assert all(len(h) == 1 for h in heads), "grouped across episodes"
