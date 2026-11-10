"""The delete_memory tool must word each erasure outcome honestly.

Drives ``DeleteMemoryTool.endpoint`` with a fake backend whose ``erase_memory`` returns a canned
outcome, so only the outcome-to-message mapping is under test.
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool

pytestmark = [pytest.mark.unit]

NODE = "node-uuid-1"


class _Backend:
    def __init__(self, outcome: dict) -> None:
        self.outcome = outcome
        self.erased: list[str] = []

    async def erase_memory(self, node_uuid: str) -> dict:
        self.erased.append(node_uuid)
        return self.outcome

    async def fetch_memory_by_uuid(self, node_uuid: str, namespace: str | None = None):
        return None


def _run(monkeypatch, outcome: dict) -> str:
    backend = _Backend(outcome)
    monkeypatch.setattr(DeleteMemoryTool, "get_backend", lambda self: backend)
    text = asyncio.run(DeleteMemoryTool().endpoint(NODE))
    assert backend.erased == [NODE]
    return text


def test_prepare_failed_says_did_not_run(monkeypatch):
    text = _run(monkeypatch, {"reason": "prepare_failed", "diagnostics": {"error": "x"}})
    assert "did NOT run" in text
    assert "Erased memory" not in text


def test_erased_incomplete_reports_unknown_provenance_count(monkeypatch):
    text = _run(
        monkeypatch,
        {"reason": "erased_incomplete", "derived": {"unknown_provenance": 2}},
    )
    assert "INCOMPLETE" in text
    assert "2" in text
    assert "completeness could not be confirmed" not in text


def test_erased_incomplete_without_specifics_admits_unconfirmed(monkeypatch):
    text = _run(monkeypatch, {"reason": "erased_incomplete", "derived": {}})
    assert "INCOMPLETE" in text
    assert "completeness could not be confirmed" in text


def test_erased_with_derived_counts_mentions_non_reversible_merges(monkeypatch):
    text = _run(
        monkeypatch,
        {
            "reason": "erased",
            "purged": {"memory_revisions.old_value": 1},
            "derived": {"entities_deleted": 1, "merge_audits_tombstoned": 3},
        },
    )
    assert text.startswith(f"Erased memory {NODE}")
    assert "3 merge(s) made non-reversible" in text


def test_unknown_reason_never_claims_erasure(monkeypatch):
    text = _run(monkeypatch, {"reason": "weird"})
    assert "Erased memory" not in text
    assert "weird" in text


def test_nothing_to_erase_text_unchanged(monkeypatch):
    text = _run(monkeypatch, {"reason": "nothing_to_erase"})
    assert text == f"No memory found with uuid={NODE}, and no stored content for it."
