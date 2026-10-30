"""Allowlist redaction for Recall Lab output (CF-269)."""

from __future__ import annotations

import pytest

from menhir.explorer.recall_lab import _redact_allowlist

MASK = "[hidden]"


def _verdict() -> dict:
    return {
        "kind": "current",
        "status": "leads",
        "subject_uuid": "subj-1",
        "attribute": "bike_spend",
        "scope": "PERSISTENT",
        "value": 185,
        "valid_at": "2026-03-15",
        "has_foundation": True,
        "contributors_total": 1,
        "brand_new_text_field": "leaky new content",
        "contributors": (
            {
                "assertion_id": "a-1",
                "relation": "bike_spend",
                "operation": "add",
                "value": 40,
                "stated_span": "I spent another $40 on bike lights",
                "source_sentence": "Yesterday I spent another $40 on bike lights.",
                "valid_at": "2026-03-15",
                "evidence_tier": "agent",
                "episode_uuid": "ep-1",
                "nested": {"deep": ["secret text", {"more": "secret"}]},
            },
        ),
    }


@pytest.mark.unit
def test_contributor_value_and_source_sentence_masked() -> None:
    out = _redact_allowlist([_verdict()], reveal=False)[0]
    c = out["contributors"][0]
    assert c["value"] == MASK
    assert c["source_sentence"] == MASK
    assert c["stated_span"] == MASK
    (nested_key, nested_val), = c["nested"].items()
    assert nested_key != "deep" and nested_key.startswith("h~")  # data keys are not passed through
    assert nested_val == [MASK, {next(iter(nested_val[1])): MASK}]


@pytest.mark.unit
def test_unknown_new_string_key_masked_by_default() -> None:
    out = _redact_allowlist([_verdict()], reveal=False)[0]
    assert out["brand_new_text_field"] == MASK
    assert out["attribute"] == MASK
    assert out["value"] == MASK


@pytest.mark.unit
def test_allowlisted_ids_scores_unmasked() -> None:
    out = _redact_allowlist([_verdict()], reveal=False)[0]
    assert out["kind"] == "current"
    assert out["subject_uuid"] == "subj-1"
    assert out["valid_at"] == "2026-03-15"
    assert out["has_foundation"] is True
    assert out["contributors_total"] == 1
    c = out["contributors"][0]
    assert c["assertion_id"] == "a-1"
    assert c["episode_uuid"] == "ep-1"
    assert c["evidence_tier"] == "agent"
    row = _redact_allowlist(
        {"uuid": "u", "final_score": 0.9, "name": "n", "score_parts": {"alpha": 0.2, "preset": "knowledge"}},
        reveal=False,
    )
    assert row["uuid"] == "u"
    assert row["final_score"] == 0.9
    assert row["name"] == MASK
    assert row["score_parts"] == {"alpha": 0.2, "preset": "knowledge"}


@pytest.mark.unit
def test_redaction_off_output_identical() -> None:
    verdicts = [_verdict()]
    assert _redact_allowlist(verdicts, reveal=True) is verdicts
    assert verdicts[0]["contributors"][0]["source_sentence"].startswith("Yesterday")
