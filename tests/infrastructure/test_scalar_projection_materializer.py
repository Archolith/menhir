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


def _row(
    assertion_id: str,
    *,
    attribute: str = "owned",
    value: int = 10,
    valid_at: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, object]:
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
        "valid_at": valid_at,
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

    def _resolve_evidence_anchors(self, episode_uuids, *, namespace):
        assert namespace == "default"
        return list(episode_uuids)

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
@pytest.mark.parametrize("installed_alias", ["turn-1", "episode-target"])
def test_materializer_hashes_resolved_view_provenance(monkeypatch: pytest.MonkeyPatch, installed_alias: str):
    row = _row("target")
    state = fold_assertions([row]).states[0]
    digest = scalar_projection_present_hash(
        SCALAR_STATE_PROJECTION,
        scalar_projection_target(row),
        value=state.value,
        valid_at=state.valid_at,
        contributor_ids=state.contributor_ids,
        effective_tier=state.effective_tier,
        episode_uuids=[installed_alias],
    )
    tx = _Tx(current=False, hashes=["before", digest])
    _install(monkeypatch, [row])

    def resolve(views, episodes, *, namespace):
        assert episodes == ["episode-target"]
        assert namespace == "default"
        return ["turn-1"]

    monkeypatch.setattr(_Views, "_resolve_evidence_anchors", resolve)
    if installed_alias == "turn-1":
        assert ScalarStateProjectionMaterializer()(tx, _token(row)) == digest
    else:
        with pytest.raises(ProjectionLifecycleCorruptionError, match="does not match"):
            ScalarStateProjectionMaterializer()(tx, _token(row))
    assert tx.writes[0]["episode_uuids"] == ["turn-1"]


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


@pytest.mark.unit
def test_default_materializer_clock_does_not_activate_future_assertions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row("future", valid_at="2099-01-01T00:00:00+00:00")
    token = _token(row)
    expected = scalar_projection_absent_hash(SCALAR_STATE_PROJECTION, token.target)
    tx = _Tx(current=False, hashes=["before", expected])
    _install(monkeypatch, [row])

    result = ScalarStateProjectionMaterializer()(tx, token)

    assert result == expected
    assert tx.current is False
    assert tx.writes == []


@pytest.mark.online
def test_real_view_episode_bridge_certification_history_and_rollback(test_neo4j_repo) -> None:
    from uuid import uuid4

    from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S
    from menhir.infrastructure.projection_lifecycle_repository import (
        ProjectionLifecycleRepository,
    )
    from menhir.infrastructure.projection_lifecycle_repository_encoding import (
        _receipt_key,
    )
    from menhir.infrastructure.realization_coverage_repository import (
        ScalarStateProjectionHashSource,
    )

    repo = test_neo4j_repo
    suffix = uuid4().hex
    # Exercise the same uniqueness contract whether this test runs alone or after activation tests.
    repo.execute(
        "CREATE CONSTRAINT typed_assertion_id_unique IF NOT EXISTS "
        "FOR (a:TypedAssertion) REQUIRE a.assertion_id IS UNIQUE"
    )
    subject, namespace = "subject-" + suffix, "materializer-" + suffix
    assertion, turn, episode = "assertion-" + suffix, "turn-" + suffix, "episode-" + suffix
    target = scalar_projection_target(
        {
            "subject_uuid": subject,
            "namespace": namespace,
            "attribute": "owned",
            "scope": "",
            "value_kind": "count",
            "unit": "count",
        }
    )
    lifecycle = ProjectionLifecycleRepository(repo)
    token = None
    try:
        repo.execute(
            """
            CREATE (:TypedAssertion {assertion_id:$assertion,subject_uuid:$subject,subject_display:'Alice',
              namespace:$namespace,attribute:'owned',scope:'',value_kind:'count',unit:'count',
              operation:'absolute',value:10,value_json:'10',evidence_tier:'user',episode_uuid:$turn,
              valid_at:datetime('2026-01-01T00:00:00Z'),learned_at:datetime('2026-01-01T00:00:00Z')})
            CREATE (t:TurnEvidence {turn_id:$turn,namespace:$namespace,evidence_finalized:true,evidence_generation:0})
            CREATE (:Episodic {uuid:$episode,group_id:$namespace})-[:ADMITTED_ON]->(t)
            CREATE (:EvidenceNamespaceFence {namespace_key:$namespace,generation:0})
            """,
            {"assertion": assertion, "subject": subject, "namespace": namespace, "turn": turn, "episode": episode},
        )
        lifecycle.publish_definition(SCALAR_STATE_PROJECTION, semantic_hash=suffix)

        def dirty():
            return lifecycle.dirty_targets(SCALAR_STATE_PROJECTION, [target], reason="online bridge test")[0]

        def commit(work):
            return lifecycle.commit(
                work,
                derivation_id=uuid4().hex,
                materialize=ScalarStateProjectionMaterializer(),
                timeout_s=SAGA_MUTATION_TIMEOUT_S,
            )

        def views():
            return repo.execute(
                """
                MATCH (n:Entity {view_subject_uuid:$subject,view_kind:'scalar_state'})
                RETURN n.uuid AS uuid,n.view_current AS current,n.ss_value AS value,
                       n.episode_uuids AS episodes,n.scalar_effective_tier AS tier ORDER BY n.uuid
                """,
                {"subject": subject},
            )

        token = dirty()
        first = commit(token)
        assert views()[0]["episodes"] == [turn]
        assert first.projection_hash == ScalarStateProjectionHashSource(repo).current_projection_hash(
            definition=SCALAR_STATE_PROJECTION, target=target, target_present=True
        )
        original_views = views()
        repo.execute(
            """
            MATCH (a:TypedAssertion {assertion_id:$id})
            CREATE (b:TypedAssertion {assertion_id:$next,subject_uuid:a.subject_uuid,
              subject_display:a.subject_display,namespace:a.namespace,attribute:a.attribute,
              scope:a.scope,value_kind:a.value_kind,unit:a.unit,operation:a.operation,
              evidence_tier:a.evidence_tier,episode_uuid:a.episode_uuid,learned_at:a.learned_at,
              value:11,value_json:'11',valid_at:datetime('2026-01-02T00:00:00Z')})
            """,
            {"id": assertion, "next": assertion + "-next"},
        )
        token = dirty()
        receipt = _receipt_key(token.work_key, token.generation)
        repo.execute(
            "CREATE (:ProjectionDerivationReceipt {receipt_key:$key,derivation_id:'conflicting'})", {"key": receipt}
        )
        with pytest.raises(ProjectionLifecycleCorruptionError):
            commit(token)
        assert views() == original_views, "failed certification left a new version or changed the current View"
        repo.execute("MATCH (r:ProjectionDerivationReceipt {receipt_key:$key}) DELETE r", {"key": receipt})
        second = commit(token)
        current = next(row for row in views() if row["current"])
        assert len(views()) == 2 and current["value"] == "11"
        assert second.projection_hash != first.projection_hash
        assert repo.execute(
            "MATCH (n:Entity {uuid:$id})-[:SUPERSEDES]->() RETURN count(*) AS count", {"id": current["uuid"]}
        ) == [{"count": 1}]
        repo.execute(
            "MATCH (a:TypedAssertion {subject_uuid:$subject}) SET a.evidence_tier='agent'", {"subject": subject}
        )
        token = dirty()
        third = commit(token)
        refreshed = next(row for row in views() if row["current"])
        assert refreshed["uuid"] == current["uuid"] and refreshed["tier"] == "agent"
        assert len(views()) == 2 and third.projection_hash != second.projection_hash
        token = lifecycle.retire_targets(SCALAR_STATE_PROJECTION, [target], reason="online retirement")[0]
        retired = commit(token)
        assert retired.projection_hash == scalar_projection_absent_hash(SCALAR_STATE_PROJECTION, target)
        assert len(views()) == 2 and not any(row["current"] for row in views())
    finally:
        repo.execute("MATCH (n:Entity {view_subject_uuid:$subject}) DETACH DELETE n", {"subject": subject})
        repo.execute("MATCH (a:TypedAssertion {subject_uuid:$subject}) DETACH DELETE a", {"subject": subject})
        repo.execute("MATCH (t:TurnEvidence {turn_id:$turn}) DETACH DELETE t", {"turn": turn})
        repo.execute("MATCH (e:Episodic {uuid:$episode}) DETACH DELETE e", {"episode": episode})
        repo.execute("MATCH (f:EvidenceNamespaceFence {namespace_key:$ns}) DELETE f", {"ns": namespace})
        if token is not None:
            repo.execute("MATCH (w:ProjectionWorkState {work_key:$key}) DELETE w", {"key": token.work_key})
            repo.execute("MATCH (r:ProjectionDerivationReceipt {work_key:$key}) DELETE r", {"key": token.work_key})
        repo.execute(
            "MATCH (d:ProjectionDefinitionState {definition_id:$id,semantic_hash:$hash}) DELETE d",
            {"id": SCALAR_STATE_PROJECTION.definition_id, "hash": suffix},
        )
