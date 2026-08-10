import asyncio

import pytest

from menhir.services import scheduler_tasks


class _FakeStore:
    """Cutoff-bound folds for the two telemetry sources."""

    def fold_failures_bounded(self, *, cutoff_id=None, after_id=0, min_count=1):
        return {"cutoff_id": 100, "groups": [
            {"operation": "enrich", "error_type": "timeout", "count": 3, "row_ids": [1, 2, 3]},
        ]}

    def fold_revisions_bounded(self, *, cutoff_id=None, after_id=0, min_count=2, exclude_fields=()):
        return {"cutoff_id": 50, "groups": [
            {"node_uuid": "n1", "field": "status", "count": 2, "row_ids": [1, 2], "latest_value": "x"},
        ]}


class _FakeMetricGraph:
    """Mimics the :Metric surface the coordinator drives (record + fingerprint reads)."""

    def __init__(self):
        self.metrics = {}

    @staticmethod
    def _key(namespace, subject, counter):
        return f"{(namespace or '').strip()}::{subject.strip().lower()}::{counter.strip().lower()}"

    def record_metric(self, *, subject, counter, value, receipt_op_id, namespace=None,
                      node_uuid=None, **_):
        key = self._key(namespace, subject, counter)
        existing = self.metrics.get(key)
        uuid = existing["uuid"] if (existing and float(existing["value"]) == float(value)) \
            else (node_uuid or f"m{len(self.metrics)}")
        self.metrics[key] = {"uuid": uuid, "value": float(value), "receipt_op_id": receipt_op_id}
        return {"uuid": uuid, "view_key": key, "created": existing is None}

    def fetch_metric(self, *, subject, counter, namespace=None):
        got = self.metrics.get(self._key(namespace, subject, counter))
        return dict(got) if got else None

    def fetch_metric_state(self, *, view_key):
        got = self.metrics.get(view_key)
        if not got:
            return None
        return {"uuid": got["uuid"], "value": float(got["value"]), "type": "METRIC",
                "labels": ["Metric"], "view_current": True, "receipt_op_id": got.get("receipt_op_id")}


@pytest.mark.unit
def test_sync_experience_counters_writes_both_folds_as_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setattr("menhir.infrastructure.telemetry.telemetry_store", _FakeStore())
    graph = _FakeMetricGraph()

    result = asyncio.run(scheduler_tasks.sync_experience_counters(graph, namespace="ns"))

    assert result == {"failure_counters": 1, "instability_counters": 1}
    # Both landed as :Metric, keyed by the two bridges' subjects.
    # failure counter = '<operation>_<error_type>_failed'; subject = the bridge default 'enrichment'.
    assert graph.metrics["ns::enrichment::enrich_timeout_failed"]["value"] == 3.0
    assert graph.metrics["ns::belief:n1::status_revised"]["value"] == 2.0
