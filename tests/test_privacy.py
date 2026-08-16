"""Tests for central memory-content redaction (privacy mode)."""

import pytest

from menhir.privacy import (
    MASK,
    redact_log_line,
    redact_mapping,
    redact_rows,
    redact_text,
)

pytestmark = pytest.mark.unit


def test_redact_text_masks_nonempty_when_not_revealing():
    assert redact_text("secret memory", reveal=False) == MASK


def test_redact_text_passthrough_when_revealing():
    assert redact_text("secret memory", reveal=True) == "secret memory"


def test_redact_text_leaves_empty_and_nonstring():
    assert redact_text("", reveal=False) == ""
    assert redact_text("   ", reveal=False) == "   "
    assert redact_text(None, reveal=False) is None
    assert redact_text(42, reveal=False) == 42


def test_redact_mapping_masks_content_fields_keeps_structure():
    row = {
        "uuid": "abc",
        "name": "Charles email note",
        "content": "lives at 123 Main St",
        "summary": "a summary",
        "preview": "preview text",
        "scope": "PERSISTENT",
        "session_id": "s1",
        "created_at": "2026-07-12",
        "labels": ["Entity"],
    }
    out = redact_mapping(row, reveal=False)
    # content fields masked
    assert out["name"] == MASK
    assert out["content"] == MASK
    assert out["summary"] == MASK
    assert out["preview"] == MASK
    # structural fields preserved
    assert out["uuid"] == "abc"
    assert out["scope"] == "PERSISTENT"
    assert out["session_id"] == "s1"
    assert out["created_at"] == "2026-07-12"
    assert out["labels"] == ["Entity"]
    # original not mutated
    assert row["content"] == "lives at 123 Main St"


def test_redact_mapping_reveal_returns_same_object():
    row = {"content": "x"}
    assert redact_mapping(row, reveal=True) is row


def test_redact_rows_applies_to_each():
    rows = [{"content": "a", "uuid": "1"}, {"content": "b", "uuid": "2"}]
    out = redact_rows(rows, reveal=False)
    assert [r["content"] for r in out] == [MASK, MASK]
    assert [r["uuid"] for r in out] == ["1", "2"]


def test_redact_log_line_keeps_prefix_masks_quoted_content():
    line = (
        "2026-07-12 14:36:27,333 - menhir.services.correlation_service - INFO - "
        "Merge proposal VETOED name='Jane Doe secret detail'"
    )
    out = redact_log_line(line, reveal=False)
    # structural prefix kept
    assert "2026-07-12 14:36:27,333" in out
    assert "menhir.services.correlation_service" in out
    assert "INFO" in out
    # quoted memory content masked
    assert "Jane Doe secret detail" not in out
    assert MASK in out


def test_redact_log_line_reveal_passthrough():
    line = "2026-07-12 00:00:00,000 - x - INFO - name='keep me'"
    assert redact_log_line(line, reveal=True) == line


def test_redact_log_line_handles_non_prefixed_line():
    line = 'raw traceback line with "quoted secret"'
    out = redact_log_line(line, reveal=False)
    assert "quoted secret" not in out
    assert MASK in out


def test_redact_log_line_empty():
    assert redact_log_line("", reveal=False) == ""


def test_redact_log_line_preserves_structural_noise():
    """Enum tokens, status codes, dict keys and uuids are NOT memory content.

    Masking them made the redacted log tail unreadable without hiding anything
    sensitive, so only free text (long, spaced, non-identifier) is masked.
    """
    line = (
        "2026-07-12 15:22:07,469 - neo4j.notifications - WARNING - "
        "<GqlStatusObject gql_status='01N51', raw_severity='WARNING', "
        "diagnostic_record={'_classification': 'UNRECOGNIZED'}>"
    )
    out = redact_log_line(line, reveal=False)
    assert out == line  # nothing masked -- no free text present
    assert MASK not in out


def test_redact_log_line_masks_memory_text_but_keeps_uuid():
    line = (
        "2026-07-12 15:22:03,078 - menhir.services.ingest - INFO - Ingested entity "
        "name='Charles lives at 123 Main Street' summary='he prefers morning meetings' "
        "uuid='ab87a3cd-64c2-4ce2-96e4-7f0c790f73db'"
    )
    out = redact_log_line(line, reveal=False)
    # memory free text masked
    assert "Charles lives at 123 Main Street" not in out
    assert "he prefers morning meetings" not in out
    assert out.count(MASK) == 2
    # uuid preserved so the line stays diagnosable
    assert "ab87a3cd-64c2-4ce2-96e4-7f0c790f73db" in out


def test_possessive_apostrophe_does_not_shift_quote_pairing():
    line = "user's note: password='hunter2' token='abc123'"
    out = redact_log_line(line, reveal=False)
    # the mid-word apostrophe must NOT open a span, so no 'user'[hidden]'' artifact
    assert "user'[hidden]" not in out
    # the two =-preceded quoted values survive as their own quoted spans
    assert "'hunter2'" in out
    assert "'abc123'" in out
    assert "'hunter2' token='abc123'" in out


def test_long_quoted_phrase_after_space_is_still_masked():
    line = "He said 'this is a long secret sentence' loudly"
    out = redact_log_line(line, reveal=False)
    assert "this is a long secret sentence" not in out
    assert MASK in out


def test_list_under_redacted_key_is_masked_elementwise():
    row = {"notes": ["secret one", "secret two"]}
    out = redact_mapping(row, reveal=False)
    assert out["notes"] == [MASK, MASK]
    assert isinstance(out["notes"], list)
    assert len(out["notes"]) == 2


def test_dict_under_redacted_key_is_masked_by_value():
    row = {"content": {"inner": "hidden"}}
    out = redact_mapping(row, reveal=False)
    assert out["content"] == {"inner": MASK}


def test_non_redacted_key_with_nested_value_is_untouched():
    row = {"labels": ["Entity", "Agent"], "extra": {"tags": ["x", "y"]}}
    out = redact_mapping(row, reveal=False)
    assert out["labels"] == ["Entity", "Agent"]
    assert out["extra"] == {"tags": ["x", "y"]}
