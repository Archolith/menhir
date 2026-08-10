"""Unit tests for domain utilities."""

from __future__ import annotations

import pytest
import json
from datetime import datetime, timezone, timedelta
from menhir.domain.utils import (
    source_confidence_for,
    days_ago,
    excerpt,
    decode_json_value,
    symbol_structure_path,
)

@pytest.mark.unit
class TestSourceConfidenceFor:
    """Tests for source_confidence_for utility."""

    def test_manual_source(self):
        assert source_confidence_for("manual") == 1.0
        assert source_confidence_for(" MANUAL ") == 1.0

    def test_user_source(self):
        # SSOT-09: "user" is the apex trust tier (SOURCE_CONFIDENCE_USER = 1.0,
        # matching domain.truth.kinds and the Trusted Memory Admission design
        # doc). The prior 0.9 here was drift, not the canonical value --
        # deliberately corrected 2026-07-11, not a mechanical refactor.
        assert source_confidence_for("user") == 1.0
        assert source_confidence_for("USER") == 1.0

    def test_claude_code_source(self):
        assert source_confidence_for("claude-code") == 0.7

    def test_gemini_source(self):
        assert source_confidence_for("google-gemini") == 0.3
        assert source_confidence_for("GEMINI-PRO") == 0.3

    def test_gpt_source(self):
        assert source_confidence_for("gpt-4o") == 0.5
        assert source_confidence_for("CHATGPT") == 0.5

    def test_default_source(self):
        assert source_confidence_for("unknown") == 0.5
        assert source_confidence_for("") == 0.5

@pytest.mark.unit
class TestDaysAgo:
    """Tests for days_ago utility."""

    def test_none_input(self):
        assert days_ago(None) == 30.0
        assert days_ago(None, default=10.0) == 10.0

    def test_naive_datetime(self):
        # Naive datetime is treated as UTC, so build the reference in UTC (not local)
        # to keep this deterministic regardless of the host timezone.
        dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
        # We can't check exact value due to 'now' changing, but it should be around 5
        res = days_ago(dt)
        assert 4.9 <= res <= 5.1

    def test_aware_datetime(self):
        dt = datetime.now(timezone.utc) - timedelta(days=10)
        res = days_ago(dt)
        assert 9.9 <= res <= 10.1

    def test_neo4j_to_native(self):
        class MockNeo4jDateTime:
            def to_native(self):
                return datetime.now(timezone.utc) - timedelta(days=2)

        assert 1.9 <= days_ago(MockNeo4jDateTime()) <= 2.1

    def test_future_date(self):
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        assert days_ago(dt) == 0.0

    def test_invalid_type(self):
        assert days_ago("not a date") == 30.0

@pytest.mark.unit
class TestExcerpt:
    """Tests for excerpt utility."""

    def test_none_input(self):
        assert excerpt(None) is None

    def test_empty_input(self):
        assert excerpt("") is None
        assert excerpt("   ") is None

    def test_whitespace_collapsing(self):
        assert excerpt("  hello   world  ") == "hello world"

    def test_under_limit(self):
        text = "Hello World"
        assert excerpt(text, limit=20) == text

    def test_over_limit(self):
        text = "This is a very long string that should be truncated"
        # "This is..." is 10 chars
        assert excerpt(text, limit=10) == "This is..."

    def test_non_string_input(self):
        assert excerpt(123) == "123"

@pytest.mark.unit
class TestDecodeJsonValue:
    """Tests for decode_json_value utility."""

    def test_none_input(self):
        assert decode_json_value(None) is None
        assert decode_json_value("") is None

    def test_valid_json(self):
        data = {"key": "value", "list": [1, 2, 3]}
        json_str = json.dumps(data)
        assert decode_json_value(json_str) == data

    def test_invalid_json(self):
        invalid_json = "{not a json}"
        assert decode_json_value(invalid_json) == invalid_json

    def test_non_string_input(self):
        assert decode_json_value(123) == 123
        assert decode_json_value({"already": "parsed"}) == {"already": "parsed"}


@pytest.mark.unit
class TestSymbolStructurePath:
    """Tests for symbol_structure_path (SSOT-12: the single source of truth for
    fully-qualified symbol paths, replacing the two independent copies that used
    to live in infrastructure/project_scanner.py::_sym_path and
    infrastructure/structure_queries.py::_symbol_path)."""

    def test_method_includes_parent_class(self):
        assert symbol_structure_path("pkg/mod.py", "run", "Worker") == "pkg/mod.py::Worker.run"

    def test_top_level_function_has_no_parent(self):
        assert symbol_structure_path("pkg/mod.py", "run", "") == "pkg/mod.py::run"

    def test_scanner_and_query_writer_agree_on_the_same_corpus(self):
        """Regression (SSOT-12): project_scanner._sym_path and
        structure_queries._symbol_path both delegate to this function now, so
        they can never independently drift again. Exercise both call sites
        directly against the same SymbolEntry corpus to lock that in."""
        from menhir.infrastructure.project_scanner import SymbolEntry, _sym_path
        from menhir.infrastructure.structure_queries import _symbol_path

        corpus = [
            SymbolEntry(file_path="a/b.py", name="Foo", kind="class", line_no=1, signature="class Foo:", docstring="", parent=""),
            SymbolEntry(file_path="a/b.py", name="bar", kind="method", line_no=2, signature="def bar(self)", docstring="", parent="Foo"),
            SymbolEntry(file_path="a/b.py", name="baz", kind="function", line_no=10, signature="def baz()", docstring="", parent=""),
        ]
        for sym in corpus:
            assert _sym_path(sym) == _symbol_path(sym) == symbol_structure_path(sym.file_path, sym.name, sym.parent)
