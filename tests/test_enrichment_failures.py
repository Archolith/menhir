"""Unit tests for enrichment failure classification."""

from __future__ import annotations

import json
import pytest

from menhir.services.enrichment_failures import is_graphiti_output_parse_error


@pytest.mark.unit
@pytest.mark.parametrize(
    "error, error_type, expected",
    [
        # Positive cases via error type
        (json.JSONDecodeError("msg", "doc", 0), None, True),
        (ValueError("some error"), "ValidationError", True),
        (RuntimeError("msg"), "jsondecodeerror", True),

        # Positive cases via error markers in text
        ("expecting value", None, True),
        ("Malformed JSON response from server", None, True),
        (RuntimeError("empty json response"), None, True),
        ("Invalid \\escape sequence", None, True),
        ("Validation Error: field required", None, True),
        ("input should be a string", None, True),

        # Case insensitivity
        ("EXPECTING VALUE", None, True),
        (None, "JSONDECODEERROR", True),

        # Negative cases
        (None, None, False),
        (RuntimeError("something else"), None, False),
        ("timeout", None, False),
        (ValueError("connection refused"), None, False),
        ("not a parse error", "UnknownError", False),
    ],
)
def test_is_graphiti_output_parse_error(error: object | None, error_type: object | None, expected: bool) -> None:
    assert is_graphiti_output_parse_error(error, error_type=error_type) is expected


@pytest.mark.unit
def test_is_graphiti_output_parse_error_handles_custom_object() -> None:
    class MyError:
        def __str__(self):
            return "malformed json response"

    assert is_graphiti_output_parse_error(MyError()) is True
