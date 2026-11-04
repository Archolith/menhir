import pytest

from menhir.mcp.tools.ops.get_memory_stats import (
    DEFAULT_P95_TARGET_S,
    P95_TARGET_ENV,
    enrichment_p95_target_seconds,
    format_p95,
)

pytestmark = pytest.mark.unit


def test_target_absent_uses_default(monkeypatch):
    monkeypatch.delenv(P95_TARGET_ENV, raising=False)
    assert enrichment_p95_target_seconds() == DEFAULT_P95_TARGET_S == 120.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 120.0),
        ("   ", 120.0),
        ("90", 90.0),
        ("90.5", 90.5),
        ("0", 0.0),
        ("abc", 120.0),
        ("-5", 120.0),
        ("nan", 120.0),
        ("inf", 120.0),
    ],
)
def test_target_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(P95_TARGET_ENV, raw)
    assert enrichment_p95_target_seconds() == expected


def test_format_under_target():
    assert format_p95(60_000, 120.0) == "60.0s  (target 120s: ok)"


def test_format_at_target_is_ok():
    assert format_p95(120_000, 120.0) == "120.0s  (target 120s: ok)"


def test_format_over_target():
    assert format_p95(130_000, 120.0) == "130.0s  (target 120s: MISS)"


def test_format_fractional_target_label():
    assert format_p95(100_000, 90.5) == "100.0s  (target 90.5s: MISS)"


def test_format_target_zero_has_no_grade():
    assert format_p95(500_000, 0.0) == "500.0s"


def test_format_none_is_na():
    assert format_p95(None, 120.0) == "n/a"
    assert format_p95(None, 0.0) == "n/a"
