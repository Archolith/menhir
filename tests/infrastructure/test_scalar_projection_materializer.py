from __future__ import annotations

import pytest

import menhir.infrastructure.scalar_projection_materializer as materializer_module
from menhir.domain.projection_lifecycle import (
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
)
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.infrastructure.scalar_projection_materializer import (
    ScalarStateProjectionMaterializer,
)
from menhir.services.scalar_projection_definition import (
    SCALAR_STATE_PROJECTION,
    scalar_projection_target,
)
from menhir.services.scalar_projection_hash import (
    scalar_projection_absent_hash,
    scalar_projection_present_hash,
)


def _row(assertion_id: str, *, attribute: str = "owned", value: int = 10) -> dict[str, object]:
    return {
        "assertion_id": assertion_id,
        "subject_uuid": "entity-1",
        "subject_display": "Alice",
        "attribute": attribute,
        "scope": "",
        "value_kind": "number",
        "unit": "count",
        "operation": "absolute",
        "value": value,
        "valid_at": "2026-01-01T00:00:00+00:00",
        "learned_at": "2026-01-01T00:00:00+00:00",
        "evidence_tier": "user",
        "episode_uuid": f"episode-{assertion_id}",
        "absolute_semantics": "ordinary",
        "namespace": "default",
    }


def _token(row: dict[str, object], *, target_present: bool = True) -> ProjectionWorkToken:
    return ProjectionWorkToken(
        work_key="work-key",
        definition_id=SCALAR_STATE_PROJECTION.definition_id,
        definition_version=SCALAR_STATE_PROJECTION.version,
        target=scalar_projection_target(row),
        generation=1,
        target_present=target_present,
        reason="test",
    )


class _Tx:
    def __init__(self, *, current: bool, hashes: list[str]) -> None:
        self.current = current
        self.hashes = list(hashes)
        self.writes: list[dict[str, object]] = []
        self.reads: list[tuple[str, dict[str, object]]] = []

    def execute(self, query: str, params=None):
        if "RETURN n.view_key AS view_key" in query:
            self.reads.append((query, dict(params or {})))
            return [{"view_key": "view-key"}] if self.current else []
        raise AssertionError(f"unexpected direct transaction query: {query}")


class _Views:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def record_scalar_state(self, **kwargs):
        self.tx.writes.append(dict(kwargs))
        self.tx.current = True
        return {"uuid": "view-1", "view_key": "view-key"}

    def retire_scalar_state(self, *, view_key: str) -> bool:
        assert view_key == "view-key"
        self.tx.current = False
        return True

    def draw_scalar_state_provenance_edges(self, **kwargs):
        return {"current_anchor": 1, "contributed_to": 0, "superseded_anchor": 0}


class _Hashes:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def current_projection_hash(self, **kwargs):
        return self.tx.hashes.pop(0)


def _install(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]) -> None:
    class _Assertions:
        def __init__(self, tx: _Tx) -> None:
            self.tx = tx

        def materializable_assertions_for_entity(self, subject_uuid: str, *, namespace=None):
            assert subject_uuid == "entity-1"
            assert namespace == "default"
            return list(rows)

    monkeypatch.setattr(materializer_module, "ViewRepository", _Views)
    monkeypatch.setattr(materializer_module, "TypedAssertionRepository", _Assertions)
    monkeypatch.setattr(materializer_module, "ScalarStateProjectionHashSource", _Hashes)


@pytest.mark.unit
def test_materializer_mutates_only_token_slot(monkeypatch: pytest.MonkeyPatch):
    target_row = _row("target", attribute="owned", value=10)
    other_row = _row("other", attribute="height", value=72)
    state = fold_assertions([target_row]).states[0]
    expected = scalar_projection_present_hash(
        SCALAR_STATE_PROJECTION,
        scalar_projection_target(target_row),
        value=state.value,
        valid_at=state.valid_at,
        contributor_ids=state.contributor_ids,
        effective_tier=state.effective_tier,
        episode_uuids=state.episode_uuids,
    )
    tx = _Tx(current=False, hashes=["before", expected])
    _install(monkeypatch, [target_row, other_row])

    result = ScalarStateProjectionMaterializer()(tx, _token(target_row))

    assert result == expected
    assert len(tx.writes) == 1
    assert tx.writes[0]["attribute"] == "owned"
    assert tx.writes[0]["value"] == 10
    assert tx.reads[-1][1]["tenant_namespaces"] == ["default", ""]
    assert "coalesce(n.namespace, n.group_id, '') IN $tenant_namespaces" in tx.reads[-1][0]


@pytest.mark.unit
def test_materializer_refuses_to_certify_wrong_installed_state(monkeypatch: pytest.MonkeyPatch):
    row = _row("target")
    tx = _Tx(current=False, hashes=["before", "wrong-installed-hash"])
    _install(monkeypatch, [row])

    with pytest.raises(ProjectionLifecycleCorruptionError, match="does not match"):
        ScalarStateProjectionMaterializer()(tx, _token(row))


@pytest.mark.unit
def test_retirement_certifies_only_physical_absence(monkeypatch: pytest.MonkeyPatch):
    row = _row("target")
    token = _token(row, target_present=False)
    expected = scalar_projection_absent_hash(SCALAR_STATE_PROJECTION, token.target)
    tx = _Tx(current=True, hashes=["before", expected])
    _install(monkeypatch, [row])

    result = ScalarStateProjectionMaterializer()(tx, token)

    assert result == expected
    assert tx.current is False
    assert tx.writes == []
