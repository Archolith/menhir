"""U14 delta-audit fixes: CF-293, CF-295, CF-296, CF-298."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain.artifact_reconciliation_metadata import (
    parse_frontmatter,
    read_document_metadata,
)
from menhir.infrastructure import schema
from menhir.services.context_builder import ContextBuilderService
from tests.test_context_builder import _mem, _recall_result

_UUID = "123e4567-e89b-42d3-a456-426614174000"
_UUID2 = "223e4567-e89b-42d3-a456-426614174000"


@pytest.mark.asyncio
async def test_cf293_todo_search_failure_is_logged(caplog):
    adapter = MagicMock()
    adapter.list_todos_matching_query.side_effect = RuntimeError("db down")
    recall = AsyncMock()
    recall.recall = AsyncMock(return_value=_recall_result([_mem("a", "Fact", "content", 0.9)]))
    svc = ContextBuilderService(recall_service=recall, graph_adapter=adapter)

    with caplog.at_level(logging.WARNING, logger="menhir.services.context_builder"):
        result = await svc.build_context("query", max_tokens=500)

    assert "Fact" in result.context
    records = [r for r in caplog.records if "TODO search failed" in r.getMessage()]
    assert records and records[0].exc_info is not None


def test_cf295_refused_duplicate_stays_refused_with_list_items():
    text = f"---\nartifact_uuid: {_UUID}\nartifact_uuid: {_UUID2}\n- {_UUID}\n---\n# T\n"
    mapping, _, errors = parse_frontmatter(text)
    assert "artifact_uuid" not in mapping
    assert "duplicate_frontmatter_key:artifact_uuid" in errors
    assert read_document_metadata(text).artifact_uuid is None


def test_cf295_third_occurrence_does_not_rebind():
    text = f"---\nartifact_uuid: {_UUID}\nartifact_uuid: {_UUID2}\nartifact_uuid: {_UUID}\n---\n"
    mapping, _, _ = parse_frontmatter(text)
    assert "artifact_uuid" not in mapping


def test_cf295_list_artifact_uuid_refused():
    text = f"---\nartifact_uuid: [{_UUID}]\n---\n# T\n"
    meta = read_document_metadata(text)
    assert meta.artifact_uuid is None
    assert "invalid_artifact_uuid" in meta.errors
    block = f"---\nartifact_uuid:\n- {_UUID}\n---\n# T\n"
    meta = read_document_metadata(block)
    assert meta.artifact_uuid is None
    assert "invalid_artifact_uuid" in meta.errors


def test_cf295_scalar_artifact_uuid_still_accepted():
    meta = read_document_metadata(f"---\nartifact_uuid: {_UUID}\n---\n# T\n")
    assert meta.artifact_uuid == _UUID


def test_cf296_correlation_service_reexports_explicit():
    from menhir.services import correlation_service as cs

    for name in (
        "CORRELATION_CONFLICT_THRESHOLD",
        "CORRELATION_MERGE_THRESHOLD",
        "CORRELATION_RELATED_THRESHOLD",
        "CorrelationBatchResult",
        "CorrelationResult",
    ):
        assert name in cs.__all__
        assert hasattr(cs, name)


def _view_text_migration() -> str:
    matches = [
        q
        for q in schema.get_scalar_state_activation_queries()
        if "n.view_summary" in q and "SET n.view_name" in q
    ]
    assert len(matches) == 1
    return matches[0]


def test_cf298_migration_truncates_to_writer_limits():
    q = _view_text_migration()
    assert f"substring(n.name, 0, {schema.VIEW_NAME_MAX_CHARS})" in q
    assert f"substring(n.summary, 0, {schema.VIEW_SUMMARY_MAX_CHARS})" in q
    assert (schema.VIEW_NAME_MAX_CHARS, schema.VIEW_SUMMARY_MAX_CHARS) == (300, 1000)


def test_cf298_migration_stays_idempotent_by_construction():
    q = _view_text_migration()
    # Selects only rows still holding indexed text, and blanks it afterwards.
    assert "coalesce(n.name, '') <> '' OR coalesce(n.summary, '') <> ''" in q
    assert "n.name = '', n.summary = ''" in q
