"""CF-165/CF-168 regression for the coordinator's direct namespace form."""

from __future__ import annotations

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ERASED, ErasureCoordinator


def test_direct_namespace_form_purges_same_named_turn_evidence(tmp_path) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str | None]] = []
            self.turn_evidence: list[str] = []

        def capture_namespace_uuids(self, group_id: str, *, namespace: str | None = None):
            return []

        def delete_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
            self.deleted.append((group_id, namespace))
            return 1

        def purge_turn_evidence(self, namespace: str) -> int:
            self.turn_evidence.append(namespace)
            return 1

    db = tmp_path / "telemetry.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    adapter = Adapter()
    coord = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )

    out = coord.erase_namespace("tenant-a")

    assert out["reason"] == ERASED
    assert adapter.deleted == [("tenant-a", None)]
    assert adapter.turn_evidence == ["tenant-a"]
