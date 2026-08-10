"""Tests for git diff attachment on episodes."""

from __future__ import annotations

import pytest

from menhir.services.enrichment_steps import compose_episode_body


# ---------------------------------------------------------------------------
# compose_episode_body unit tests
# ---------------------------------------------------------------------------

class TestComposeEpisodeBody:
    def test_no_diff_returns_content_only(self):
        claimed = {"content": "User prefers dark mode."}
        assert compose_episode_body(claimed) == "User prefers dark mode."

    def test_none_diff_returns_content_only(self):
        claimed = {"content": "User prefers dark mode.", "diff": None}
        assert compose_episode_body(claimed) == "User prefers dark mode."

    def test_empty_string_diff_returns_content_only(self):
        claimed = {"content": "User prefers dark mode.", "diff": ""}
        assert compose_episode_body(claimed) == "User prefers dark mode."

    def test_whitespace_only_diff_returns_content_only(self):
        claimed = {"content": "some content", "diff": "   \n  "}
        assert compose_episode_body(claimed) == "some content"

    def test_diff_appended_to_content(self):
        diff_text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new"
        claimed = {"content": "Refactored foo module.", "diff": diff_text}
        result = compose_episode_body(claimed)
        assert result.startswith("Refactored foo module.")
        assert "--- git diff ---" in result
        assert diff_text in result

    def test_diff_with_empty_content(self):
        claimed = {"content": "", "diff": "+added line"}
        result = compose_episode_body(claimed)
        assert "--- git diff ---" in result
        assert "+added line" in result

    def test_diff_with_missing_content_key(self):
        claimed = {"diff": "+added line"}
        result = compose_episode_body(claimed)
        assert "--- git diff ---" in result
        assert "+added line" in result

    def test_large_diff_truncated(self):
        from menhir.services.enrichment_steps import MAX_DIFF_CHARS

        big_diff = "x" * (MAX_DIFF_CHARS + 5000)
        claimed = {"content": "big change", "diff": big_diff}
        result = compose_episode_body(claimed)
        assert "... [diff truncated]" in result
        # The diff portion should be at most MAX_DIFF_CHARS + the truncation marker
        diff_section = result.split("--- git diff ---\n", 1)[1]
        assert len(diff_section) < MAX_DIFF_CHARS + 50

    def test_diff_at_exact_limit_not_truncated(self):
        from menhir.services.enrichment_steps import MAX_DIFF_CHARS

        exact_diff = "y" * MAX_DIFF_CHARS
        claimed = {"content": "exact", "diff": exact_diff}
        result = compose_episode_body(claimed)
        assert "... [diff truncated]" not in result
        assert exact_diff in result


# ---------------------------------------------------------------------------
# Stub adapter stores diff field
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_stub_adapter_stores_diff(stub_memory_graph_adapter):
    adapter = stub_memory_graph_adapter
    diff_text = "diff --git a/x.py b/x.py"
    adapter.create_pending_episode(
        episode_uuid="ep-diff-1",
        name="episode-test-ep-diff-1",
        content="test content",
        session_id="sess-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
        diff=diff_text,
    )
    row = adapter.pending_episode_rows["ep-diff-1"]
    assert row["diff"] == diff_text


@pytest.mark.unit
def test_stub_adapter_stores_none_diff_by_default(stub_memory_graph_adapter):
    adapter = stub_memory_graph_adapter
    adapter.create_pending_episode(
        episode_uuid="ep-diff-2",
        name="episode-test-ep-diff-2",
        content="test content",
        session_id="sess-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )
    row = adapter.pending_episode_rows["ep-diff-2"]
    assert row["diff"] is None


# ---------------------------------------------------------------------------
# IngestService threads diff to adapter
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_service_passes_diff_to_adapter(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
):
    from menhir.domain.session import new_session
    from menhir.services.ingest_service import IngestService

    session = new_session(user_id="test-user")
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graph_adapter=adapter,
        graphiti_client=stub_graphiti_client,
        llm=stub_llm_adapter,
    )

    async def _noop_enqueue(uuid: str) -> None:
        pass

    monkeypatch.setattr(service, "_ensure_enrichment_worker", lambda: _noop_enqueue(""))
    monkeypatch.setattr(service, "_enqueue_pending_episode", _noop_enqueue)

    diff_text = "diff --git a/bar.py b/bar.py\n+new code"
    result = await service.queue_episode_for_enrichment(
        "changed bar module", session, "unit-test", diff=diff_text,
    )
    assert result.status.value == "queued"
    row = adapter.pending_episode_rows[result.episode_id]
    assert row["diff"] == diff_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_service_omits_diff_when_not_provided(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch,
):
    from menhir.domain.session import new_session
    from menhir.services.ingest_service import IngestService

    session = new_session(user_id="test-user")
    adapter = stub_memory_graph_adapter
    service = IngestService(
        graph_adapter=adapter,
        graphiti_client=stub_graphiti_client,
        llm=stub_llm_adapter,
    )

    async def _noop_enqueue(uuid: str) -> None:
        pass

    monkeypatch.setattr(service, "_ensure_enrichment_worker", lambda: _noop_enqueue(""))
    monkeypatch.setattr(service, "_enqueue_pending_episode", _noop_enqueue)

    result = await service.queue_episode_for_enrichment(
        "plain memory", session, "unit-test",
    )
    row = adapter.pending_episode_rows[result.episode_id]
    assert row["diff"] is None
