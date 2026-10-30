"""U6 / CF-270: the coverage audit folds the rows the REAL repository returns (count_* fields and the
admitted episode, as the writer's query returns them), so a correct match-mode View of decided,
turn-grounded occurrences audits clean. Online: needs the test Neo4j instance."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import normalize_scalar
from menhir.infrastructure.projection_coverage_repository import ProjectionCoverageRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.services.projection_coverage_service import ProjectionCoverageService

pytestmark = pytest.mark.online

NS = "tenant-counted"
SUBJECT = "entity-counted"


def _seed_occurrence(repo, aid: str, *, status: str, turn_id: str, episode_uuid: str) -> None:
    repo.execute(
        """
        MERGE (te:TurnEvidence {turn_id: $turn_id})
        MERGE (ep:Episodic {uuid: $episode_uuid})
        MERGE (ep)-[:ADMITTED_ON]->(te)
        MERGE (h:TypedAssertionHead {source_key: $sk})
        SET h.subject_uuid = $subject, h.namespace = $ns
        CREATE (a:TypedAssertion {
            assertion_id: $aid, assertion_key: $ak, claim_key: $ck, source_key: $sk,
            subject_uuid: $subject, subject_display: 'Me', namespace: $ns,
            attribute: 'model_kit_count', scope: '', value_kind: 'count', unit: '',
            operation: 'occurrence', value: '1', value_json: '1', stated_span: 'fixture',
            valid_at: datetime('2026-01-01T00:00:00Z'), learned_at: datetime('2026-01-01T00:00:00Z'),
            episode_uuid: $turn_id, evidence_tier: 'user', perceiver_version: 'v1',
            absolute_semantics: 'ordinary', importance: 'notable', superseded: false,
            binding_pending: false, projection_pending: false,
            count_object: 'model kit', count_item: '', count_action: '', count_family: '',
            count_status: $status, count_target_id: ''
        })
        MERGE (h)-[:HAS_VERSION]->(a)
        MERGE (h)-[:CURRENT]->(a)
        """,
        params={"aid": aid, "ak": f"ak-{aid}", "ck": f"ck-{aid}", "sk": f"sk-{aid}",
                "subject": SUBJECT, "ns": NS, "status": status, "turn_id": turn_id,
                "episode_uuid": episode_uuid},
    )


def _seed_view(repo, state) -> None:
    repo.execute(
        """
        CREATE (v:Entity {
            uuid: 'view-counted', view_kind: 'scalar_state', view_key: 'vk-counted',
            view_subject_uuid: $subject, group_id: $ns, view_current: true,
            ss_attribute: $attribute, ss_scope: $scope, ss_kind: $kind, ss_unit: $unit,
            ss_value: $value, valid_at: datetime($valid_at),
            scalar_contributors: $contributors, scalar_effective_tier: $tier,
            episode_uuids: $episodes
        })
        """,
        params={"subject": SUBJECT, "ns": NS, "attribute": state.attribute, "scope": state.scope,
                "kind": state.value_kind, "unit": state.unit,
                "value": normalize_scalar(state.value), "valid_at": state.valid_at,
                "contributors": list(state.contributor_ids), "tier": state.effective_tier,
                "episodes": list(state.episode_uuids)},
    )


def test_correct_match_mode_view_of_decided_turn_grounded_occurrences_audits_clean(
    test_neo4j_repo,
) -> None:
    _seed_occurrence(test_neo4j_repo, "o1", status="counted", turn_id="turn-1", episode_uuid="ep-1")
    _seed_occurrence(test_neo4j_repo, "o2", status="uncertain", turn_id="turn-2", episode_uuid="ep-2")

    # What the writer folds: its own reader's rows, match mode, evaluated at now.
    writer_rows = TypedAssertionRepository(test_neo4j_repo).assertions_for_entity(
        SUBJECT, materializable_only=True, namespace=NS)
    fold = fold_assertions(writer_rows, as_of=datetime.now(timezone.utc), count_match=True)
    state, = fold.states
    assert state.value == 1 and state.count_uncertain == 1
    assert set(state.episode_uuids) == {"ep-1", "ep-2"}  # the admitted episodes, not the turn ids
    _seed_view(test_neo4j_repo, state)

    service = ProjectionCoverageService(
        ProjectionCoverageRepository(test_neo4j_repo), counting_enabled=True, count_match=True)
    report = service.audit_entity(SUBJECT, namespace=NS)
    assert report.clean, report
