"""Offline unit tests for the derived re-derive job (fake adapter + fake LLM)."""

from __future__ import annotations

import asyncio

from menhir.services.derived_rederive import MAX_ATTEMPTS, run_derived_rederive


class FakeAdapter:
    def __init__(self, items, repairs, cas_ok=True):
        self.items = items  # (kind, uuid) -> item dict
        self.repairs = {r["repair_key"]: dict(r, status="pending", attempts=0) for r in repairs}
        self.cas_ok = cas_ok
        self.applied = []

    def list_pending_derived_rederive(self, limit):
        return [r for r in self.repairs.values() if r["status"] == "pending"][:limit]

    def load_derived_rederive_item(self, kind, uuid):
        return self.items.get((kind, uuid))

    def apply_derived_rederive(self, **kw):
        self.applied.append(kw)
        if not self.cas_ok:
            return False
        self.repairs[kw["repair_key"]]["status"] = "deleted" if kw.get("delete") else "done"
        return True

    def mark_derived_rederive_status(self, repair_key, status):
        self.repairs[repair_key]["status"] = status
        return True

    def record_derived_rederive_failure(self, repair_key, error_class, max_attempts):
        r = self.repairs[repair_key]
        r["attempts"] += 1
        r["last_error"] = error_class
        if r["attempts"] >= max_attempts:
            r["status"] = "failed"
        return r["status"]


class FakeLLM:
    def __init__(self, verdict=None, exc=None):
        self.verdict = verdict
        self.exc = exc
        self.calls = 0

    async def rederive_from_sources(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.verdict


def _item(sources=("remaining source",)):
    return {
        "pending": True, "flagged_at": "2026-11-09T00:00:00Z", "episodes": ["e1"],
        "text": "old fact", "name": "Thing", "sources": list(sources),
    }


def _repair(kind="fact"):
    return {"repair_key": "k1", "item_kind": kind, "item_uuid": "u1", "namespace": "default"}


def _run(adapter, llm):
    return asyncio.run(run_derived_rederive(adapter, llm))


def test_supported_fact_rewritten_with_cas_token():
    adapter = FakeAdapter({("fact", "u1"): _item()}, [_repair()])
    result = _run(adapter, FakeLLM(("supported", "new fact")))
    assert result["rewritten"] == 1
    call = adapter.applied[0]
    assert call["new_text"] == "new fact" and not call.get("delete")
    assert call["flagged_at"] == "2026-11-09T00:00:00Z" and call["episodes"] == ["e1"]
    assert adapter.repairs["k1"]["status"] == "done"


def test_unsupported_fact_deleted():
    adapter = FakeAdapter({("fact", "u1"): _item()}, [_repair()])
    result = _run(adapter, FakeLLM(("unsupported", None)))
    assert result["deleted"] == 1
    assert adapter.applied[0]["delete"] is True
    assert adapter.repairs["k1"]["status"] == "deleted"


def test_fact_without_sources_deleted_without_llm_call():
    adapter = FakeAdapter({("fact", "u1"): _item(sources=())}, [_repair()])
    llm = FakeLLM(("supported", "x"))
    result = _run(adapter, llm)
    assert result["deleted"] == 1 and llm.calls == 0


def test_entity_without_sources_gets_empty_summary_not_deleted():
    adapter = FakeAdapter({("entity", "u1"): _item(sources=())}, [_repair("entity")])
    result = _run(adapter, FakeLLM())
    assert result["rewritten"] == 1
    call = adapter.applied[0]
    assert call["new_text"] == "" and not call.get("delete")


def test_cas_failure_leaves_repair_pending():
    adapter = FakeAdapter({("fact", "u1"): _item()}, [_repair()], cas_ok=False)
    result = _run(adapter, FakeLLM(("supported", "new fact")))
    assert result["cas_failed"] == 1 and result["rewritten"] == 0
    assert adapter.repairs["k1"]["status"] == "pending"


def test_missing_item_marked_obsolete():
    adapter = FakeAdapter({}, [_repair()])
    result = _run(adapter, FakeLLM())
    assert result["obsolete"] == 1
    assert adapter.repairs["k1"]["status"] == "obsolete"


def test_no_llm_skips_and_leaves_pending():
    adapter = FakeAdapter({("fact", "u1"): _item()}, [_repair()])
    assert _run(adapter, None) == {"skipped": "no_llm"}
    assert adapter.repairs["k1"]["status"] == "pending" and not adapter.applied


def test_five_failures_mark_failed_without_content_in_error():
    adapter = FakeAdapter({("fact", "u1"): _item()}, [_repair()])
    llm = FakeLLM(exc=RuntimeError("echoed secret erased text"))
    for _ in range(MAX_ATTEMPTS - 1):
        _run(adapter, llm)
        assert adapter.repairs["k1"]["status"] == "pending"
    result = _run(adapter, llm)
    assert result["failed"] == 1
    assert adapter.repairs["k1"]["status"] == "failed"
    assert adapter.repairs["k1"]["attempts"] == MAX_ATTEMPTS
    assert adapter.repairs["k1"]["last_error"] == "RuntimeError"
