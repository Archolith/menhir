"""CF-14: STRUCTURAL_FIELDS is enforced, not decorative.

A field in STRUCTURAL_FIELDS must never be redacted, even when it also appears in
the caller's explicit ``fields`` deny-list. The two sets are disjoint today, so
this is a no-op on current constants; these tests guard against a future
collision (e.g. the ``label`` / ``labels`` pluralization) and pin today's
behavior as a regression net.
"""

from menhir.privacy import (
    MASK,
    REDACTED_FIELDS,
    STRUCTURAL_FIELDS,
    redact_mapping,
    redact_rows,
)


def test_collision_structural_wins_through_redact_mapping():
    row = {"content": "free text", "uuid": "deadbeef"}
    out = redact_mapping(row, fields=frozenset({"content", "uuid"}))
    assert out["uuid"] == "deadbeef"
    assert out["content"] == MASK


def test_collision_structural_wins_through_redact_rows():
    rows = [{"content": "free text", "uuid": "deadbeef"}]
    out = redact_rows(rows, fields=frozenset({"content", "uuid"}))
    assert out[0]["uuid"] == "deadbeef"
    assert out[0]["content"] == MASK


def test_default_args_mask_all_redacted_and_pass_all_structural():
    row = {f: "value" for f in REDACTED_FIELDS}
    row.update({f: "value" for f in STRUCTURAL_FIELDS})
    out = redact_mapping(row)
    for f in REDACTED_FIELDS:
        assert out[f] == MASK, f
    for f in STRUCTURAL_FIELDS:
        assert out[f] == "value", f


def test_reveal_is_full_passthrough_for_both_sets():
    row = {f: "value" for f in REDACTED_FIELDS}
    row.update({f: "value" for f in STRUCTURAL_FIELDS})
    assert redact_mapping(row, reveal=True) == row
    assert redact_rows([row], reveal=True) == [row]


def test_pluralization_tripwire_label_vs_labels():
    assert REDACTED_FIELDS & STRUCTURAL_FIELDS == frozenset()
    row = {"label": "free text", "labels": ["a", "b"]}
    out = redact_mapping(row, fields=frozenset({"label", "labels"}))
    assert out["labels"] == ["a", "b"]
    assert out["label"] == MASK


def test_positive_control_redaction_really_masks():
    row = {"content": "free text"}
    out = redact_mapping(row)
    assert out["content"] == MASK
