"""#409 review: source-search readiness matches what source recall can use, and the guidance
names the fix that actually changes it."""

from __future__ import annotations

import pytest

from menhir.core.backend_runtime_admin_ops import source_search_status
from menhir.mcp.formatters import _SOURCE_SEARCH_GUIDANCE

pytestmark = pytest.mark.unit


def _status(**kw):
    args = dict(eligible=True, size=768, stamped_model="nomic-embed-text",
                processing_state="READY", model="nomic-embed-text", expected_size=768)
    args.update(kw)
    return source_search_status(**args)


def test_a_compatible_vector_smaller_than_1024_is_ready() -> None:
    # F1: a 768-d embedder must not read PENDING forever.
    assert _status() == ("READY", None)


def test_unknown_embedder_size_is_unknown_not_pending() -> None:
    assert _status(expected_size=None) == ("UNKNOWN", None)


def test_ineligible_receipt_is_unavailable() -> None:
    assert _status(eligible=False, size=None) == ("UNAVAILABLE", None)


@pytest.mark.parametrize("state", ["READY", "FAILED"])
def test_missing_vector_on_a_settled_receipt_waits_for_repair(state) -> None:
    assert _status(size=None, processing_state=state) == ("PENDING", "missing")
    assert _status(size=0, processing_state=state) == ("PENDING", "missing")


@pytest.mark.parametrize("state", ["PENDING", "ENRICHING", None])
def test_missing_vector_in_flight_waits_for_ingest(state) -> None:
    assert _status(size=None, processing_state=state) == ("PENDING", "missing_in_flight")


def test_wrong_size_and_other_model_need_a_reembed() -> None:
    assert _status(size=1536) == ("PENDING", "size_mismatch")
    assert _status(stamped_model="text-embedding-3-small") == ("PENDING", "model_mismatch")
    assert _status(stamped_model=None) == ("PENDING", "model_mismatch")


def test_legacy_unknown_stamp_is_accepted_like_recall() -> None:
    assert _status(stamped_model="unknown")[0] == "READY"


def test_unresolved_current_model_skips_the_model_check() -> None:
    assert _status(model=None, stamped_model="anything") == ("READY", None)


def test_guidance_points_at_the_fix_for_each_reason() -> None:
    assert "repair job" in _SOURCE_SEARCH_GUIDANCE["missing"]
    assert "ingest step" in _SOURCE_SEARCH_GUIDANCE["missing_in_flight"]
    for reason in ("size_mismatch", "model_mismatch"):
        assert "backfill_episode_embeddings.py" in _SOURCE_SEARCH_GUIDANCE[reason]
        assert "repair job" not in _SOURCE_SEARCH_GUIDANCE[reason]
