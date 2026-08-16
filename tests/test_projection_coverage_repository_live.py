from __future__ import annotations

import json

import pytest

from menhir.domain.projection_coverage import (
    AuditFailureClassification,
    BindingStatus,
    ParityViolationKind,
    ProjectionStatus,
)
from menhir.infrastructure.projection_coverage_repository import ProjectionCoverageRepository
from menhir.services.projection_coverage_service import ProjectionCoverageService

pytestmark = pytest.mark.online

NS = "tenant-a"


def _seed_assertion(
    repo,
    assertion_id: str,
    *,
    subject_uuid: str,
    source_key: str | None = None,
    namespace: str = NS,
    attribute: str = "score",
    operation: str = "absolute",
    value=10,
    valid_at: str = "2026-01-01T00:00:00+00:00",
    superseded: bool = False,
    binding_pending: bool = False,
    projection_pending: bool = False,
    make_current: bool = True,
) -> None:
    source_key = source_key or f"source-{namespace}-{assertion_id}"
    repo.execute(
        """
        MERGE (h:TypedAssertionHead {source_key: $source_key})
        SET h.subject_uuid = $subject_uuid, h.namespace = $namespace
        CREATE (a:TypedAssertion {
            assertion_id: $assertion_id,
            assertion_key: $assertion_key,
            claim_key: $claim_key,
            source_key: $source_key,
            subject_uuid: $subject_uuid,
            subject_display: 'Entity',
            namespace: $namespace,
            attribute: $attribute,
            scope: '',
            value_kind: 'count',
            unit: '',
            operation: $operation,
            value: $value_surface,
            value_json: $value_json,
            stated_span: 'fixture',
            valid_at: datetime($valid_at),
            learned_at: datetime($valid_at),
            episode_uuid: $episode_uuid,
            evidence_tier: 'user',
            perceiver_version: 'v1',
            absolute_semantics: 'ordinary',
            superseded: $superseded,
            binding_pending: $binding_pending,
            projection_pending: $projection_pending
        })
        MERGE (h)-[:HAS_VERSION]->(a)
        FOREACH (_ IN CASE WHEN $make_current THEN [1] ELSE [] END |
            MERGE (h)-[:CURRENT]->(a))
        """,
        params={
            "source_key": source_key,
            "subject_uuid": subject_uuid,
            "namespace": namespace,
            "assertion_id": assertion_id,
            "assertion_key": f"ak-{namespace}-{assertion_id}",
            "claim_key": f"ck-{namespace}-{assertion_id}",
            "attribute": attribute,
            "operation": operation,
            "value_surface": str(value),
            "value_json": json.dumps(value),
            "valid_at": valid_at,
            "episode_uuid": f"ep-{namespace}-{assertion_id}",
            "superseded": superseded,
            "binding_pending": binding_pending,
            "projection_pending": projection_pending,
            "make_current": make_current,
        },
    )


def _seed_view(
    repo,
    *,
    subject_uuid: str,
    view_key: str,
    assertion_ids: list[str],
    namespace: str = NS,
    attribute: str = "score",
    value=10,
    valid_at: str = "2026-01-01T00:00:00+00:00",
    episodes: list[str] | None = None,
) -> None:
    repo.execute(
        """
        CREATE (v:Entity {
            uuid: $uuid,
            view_kind: 'scalar_state',
            view_key: $view_key,
            view_subject_uuid: $subject_uuid,
            group_id: $namespace,
            view_current: true,
            ss_attribute: $attribute,
            ss_scope: '',
            ss_kind: 'count',
            ss_unit: '',
            ss_value: $value,
            valid_at: datetime($valid_at),
            scalar_contributors: $contributors,
            scalar_effective_tier: 'user',
            episode_uuids: $episodes,
            audit_probe: 'keep'
        })
        """,
        params={
            "uuid": f"view-{namespace}-{view_key}",
            "view_key": view_key,
            "subject_uuid": subject_uuid,
            "namespace": namespace,
            "attribute": attribute,
            "value": value,
            "valid_at": valid_at,
            "contributors": assertion_ids,
            "episodes": episodes
            if episodes is not None
            else [f"ep-{namespace}-{assertion_id}" for assertion_id in assertion_ids],
        },
    )


def _service(test_neo4j_repo) -> ProjectionCoverageService:
    return ProjectionCoverageService(ProjectionCoverageRepository(test_neo4j_repo))


def test_projection_coverage_live_clean_pending_and_history(test_neo4j_repo) -> None:
    subject = "entity-live-a"
    _seed_assertion(test_neo4j_repo, "current", subject_uuid=subject)
    _seed_view(
        test_neo4j_repo,
        subject_uuid=subject,
        view_key="current",
        assertion_ids=["current"],
    )

    clean = _service(test_neo4j_repo).audit_entity(subject, namespace=NS)
    assert clean.clean is True
    assert clean.accounting[0].projection_status is ProjectionStatus.PROJECTED

    pending_subject = "entity-live-pending"
    _seed_assertion(
        test_neo4j_repo,
        "pending",
        subject_uuid=pending_subject,
        projection_pending=True,
    )
    pending = _service(test_neo4j_repo).audit_entity(pending_subject, namespace=NS)
    assert pending.accounting[0].projection_status is ProjectionStatus.PROJECTION_PENDING
    assert ParityViolationKind.MISSING_VIEW in {
        violation.kind for violation in pending.parity_violations
    }

    # History remains visible even though only the current version can feed the fold.
    history_subject = "entity-live-history"
    source_key = "source-history"
    _seed_assertion(
        test_neo4j_repo,
        "old",
        subject_uuid=history_subject,
        source_key=source_key,
        superseded=True,
        make_current=False,
        value=5,
    )
    _seed_assertion(
        test_neo4j_repo,
        "new",
        subject_uuid=history_subject,
        source_key=source_key,
        value=10,
    )
    _seed_view(
        test_neo4j_repo,
        subject_uuid=history_subject,
        view_key="history-current",
        assertion_ids=["new"],
    )
    history = _service(test_neo4j_repo).audit_entity(history_subject, namespace=NS)
    records = {record.assertion_id: record for record in history.accounting}
    assert records["old"].projection_status is ProjectionStatus.NOT_REQUIRED
    assert records["new"].projection_status is ProjectionStatus.PROJECTED


def test_projection_coverage_live_binding_pending_and_parity_drift(test_neo4j_repo) -> None:
    sentinel = "unbound:source-live"
    _seed_assertion(
        test_neo4j_repo,
        "advisory",
        subject_uuid=sentinel,
        binding_pending=True,
    )
    advisory = _service(test_neo4j_repo).audit_entity(sentinel, namespace=NS)
    assert advisory.accounting[0].binding_status is BindingStatus.BINDING_PENDING
    assert advisory.accounting[0].projection_status is ProjectionStatus.NOT_REQUIRED

    subject = "entity-live-drift"
    _seed_assertion(test_neo4j_repo, "anchor", subject_uuid=subject)
    _seed_view(
        test_neo4j_repo,
        subject_uuid=subject,
        view_key="stale-receipt",
        assertion_ids=["wrong-contributor"],
        value=10,
        episodes=["ep-tenant-a-anchor"],
    )
    drift = _service(test_neo4j_repo).audit_entity(subject, namespace=NS)
    assert ParityViolationKind.CONTRIBUTOR_SET_MISMATCH in {
        violation.kind for violation in drift.parity_violations
    }
    assert ParityViolationKind.VALUE_MISMATCH not in {
        violation.kind for violation in drift.parity_violations
    }


def test_projection_coverage_live_orphaned_abstention_and_namespace_isolation(
    test_neo4j_repo,
) -> None:
    abstained = "entity-live-abstain"
    _seed_assertion(
        test_neo4j_repo,
        "delta",
        subject_uuid=abstained,
        operation="delta",
        value=1,
    )
    _seed_view(
        test_neo4j_repo,
        subject_uuid=abstained,
        view_key="orphan",
        assertion_ids=["delta"],
        value=1,
    )
    orphan = _service(test_neo4j_repo).audit_entity(abstained, namespace=NS)
    assert AuditFailureClassification.INVALID_COMBINATION in {
        violation.classification for violation in orphan.coverage_violations
    }
    assert ParityViolationKind.ORPHANED_VIEW in {
        violation.kind for violation in orphan.parity_violations
    }

    shared = "entity-live-shared"
    _seed_assertion(
        test_neo4j_repo,
        "a",
        subject_uuid=shared,
        namespace="tenant-a",
        value=10,
    )
    _seed_assertion(
        test_neo4j_repo,
        "b",
        subject_uuid=shared,
        namespace="tenant-b",
        value=20,
    )
    _seed_view(
        test_neo4j_repo,
        subject_uuid=shared,
        namespace="tenant-a",
        view_key="a",
        assertion_ids=["a"],
        value=10,
    )
    _seed_view(
        test_neo4j_repo,
        subject_uuid=shared,
        namespace="tenant-b",
        view_key="b",
        assertion_ids=["b"],
        value=20,
    )

    service = _service(test_neo4j_repo)
    report_a = service.audit_entity(shared, namespace="tenant-a")
    report_b = service.audit_entity(shared, namespace="tenant-b")
    assert report_a.clean is True
    assert report_b.clean is True
    assert {record.assertion_id for record in report_a.accounting} == {"a"}
    assert {record.assertion_id for record in report_b.accounting} == {"b"}


def test_projection_coverage_live_audit_performs_no_writes(test_neo4j_repo) -> None:
    subject = "entity-live-readonly"
    _seed_assertion(
        test_neo4j_repo,
        "current",
        subject_uuid=subject,
        projection_pending=True,
    )
    _seed_view(
        test_neo4j_repo,
        subject_uuid=subject,
        view_key="readonly",
        assertion_ids=["current"],
    )

    def snapshot() -> tuple[int, int, bool, str, bool]:
        nodes = test_neo4j_repo.execute("MATCH (n) RETURN count(n) AS count")[0]["count"]
        relationships = test_neo4j_repo.execute(
            "MATCH ()-[r]->() RETURN count(r) AS count"
        )[0]["count"]
        assertion = test_neo4j_repo.execute(
            """
            MATCH (a:TypedAssertion {assertion_id: 'current'})
            RETURN a.projection_pending AS pending
            """
        )[0]
        view = test_neo4j_repo.execute(
            """
            MATCH (v:Entity {view_key: 'readonly'})
            RETURN v.audit_probe AS probe, v.view_current AS current
            """
        )[0]
        return (
            int(nodes),
            int(relationships),
            bool(assertion["pending"]),
            str(view["probe"]),
            bool(view["current"]),
        )

    before = snapshot()

    report = _service(test_neo4j_repo).audit_entity(subject, namespace=NS)
    assert report.accounting[0].projection_status is ProjectionStatus.PROJECTION_PENDING

    assert snapshot() == before
