from __future__ import annotations

from dataclasses import dataclass
import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.freshness import TrustResolverProjectionFreshness
from spikes.mutation_kernel.kernel import View
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.registry import ProjectionTarget
from spikes.mutation_kernel.trust import TrustResolutionProposal
from spikes.mutation_kernel.trust_deployment import (
    PurposeTrustResolverDefinition,
    TrustResolverDeploymentCoordinator,
)


NAMESPACE = "mutation-freshness"
PURPOSES = ("test_purpose",)


@dataclass(frozen=True)
class ResolverV1:
    resolver_id: str = "freshness.test_resolver"
    version: int = 1

    def resolve(self, profile, purpose: str) -> TrustResolutionProposal:
        return TrustResolutionProposal(
            authority="agent",
            reason=f"resolver_v{self.version}",
        )


@dataclass(frozen=True)
class ResolverV2(ResolverV1):
    version: int = 2


@dataclass(frozen=True)
class ResolverV3(ResolverV1):
    version: int = 3


def _definition(resolver) -> PurposeTrustResolverDefinition:
    return PurposeTrustResolverDefinition.from_resolver(
        resolver,
        purposes=PURPOSES,
    )


def _target() -> ProjectionTarget:
    return ProjectionTarget(
        view_type="freshness.test_view",
        subject_id="freshness-subject",
        scope="",
        key="answer",
        dimensions=(("purpose", "test_purpose"),),
    )


def _view(value: str, marker: str) -> View:
    return View(
        view_type="freshness.test_view",
        subject_id="freshness-subject",
        scope="",
        key="answer",
        value=value,
        confidence=1.0,
        contributor_ids=(f"assertion-{marker}",),
        effective_authority="agent",
        rationale=(f"derived-by={marker}",),
        dimensions=(("purpose", "test_purpose"),),
    )


def _repo() -> Neo4jRepository:
    uri = os.getenv("MENHIR_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("requires explicit MENHIR_TEST_NEO4J_URI scratch instance")
    repo = Neo4jRepository(
        uri=uri,
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
    )
    if not repo.ping():
        repo.close()
        pytest.fail(f"configured test Neo4j is unreachable: {uri}")
    return repo


def test_freshness_lifecycle_marks_dirty_projection_and_requires_exact_new_certificate() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        freshness = TrustResolverProjectionFreshness(
            repo,
            namespace=NAMESPACE,
            envelope_store=store,
        )
        store.activate()
        deployment.activate()

        target = _target()
        resolver_v1 = ResolverV1()
        definition_v1 = _definition(resolver_v1)
        deployment.publish_definition(definition_v1)
        deployment.register_dependency(definition_v1, target)
        snapshot_v1 = deployment.capture_snapshot(definition_v1)
        view_v1 = _view("old-v1-answer", "v1")

        def commit_and_certify_v1():
            store.record_outcome(view_v1)
            freshness.certify(
                definition_v1,
                target=target,
                outcome=view_v1,
                derivation_id="derivation-v1",
                derivation_resolver_version=1,
                generation=0,
            )

        deployment.run_fenced(
            definition_v1,
            resolver_v1,
            snapshot_v1,
            commit_and_certify_v1,
        )

        first = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        assert first.state == "fresh"
        assert first.outcome == view_v1
        assert first.reason == "certified_current"
        assert first.work_generation == first.completed_generation == 0
        assert first.certified_resolver_version == 1
        assert first.projection_hash == first.certified_projection_hash
        assert first.derivation_id == "derivation-v1"

        resolver_v2 = ResolverV2()
        definition_v2 = _definition(resolver_v2)
        upgraded = deployment.publish_definition(definition_v2)
        assert upgraded["invalidated_target_count"] == 1
        work_v2 = deployment.pending_work(resolver_v1.resolver_id)[0]
        assert work_v2.generation == 1

        # The raw generic store still has the old row, but freshness-aware reads cannot call it fresh.
        assert store.load_views(
            subject_id=target.subject_id,
            view_type=target.view_type,
            scope=target.scope,
            key=target.key,
            dimensions=target.dimensions,
        ) == [view_v1]

        strict_dirty = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        assert strict_dirty.state == "unavailable"
        assert strict_dirty.outcome is None
        assert "pending_generation" in strict_dirty.reason
        assert "resolver_version_not_certified" in strict_dirty.reason

        marked_dirty = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="allow_stale_with_marker",
        )
        assert marked_dirty.state == "stale"
        assert marked_dirty.outcome == view_v1
        assert marked_dirty.current_resolver_version == 2
        assert marked_dirty.work_generation == 1
        assert marked_dirty.completed_generation == 0
        assert marked_dirty.certified_resolver_version == 1

        # A new projection write is still not fresh until its exact hash/generation is certified.
        snapshot_v2 = deployment.capture_snapshot(definition_v2)
        view_v2 = _view("new-v2-answer", "v2")
        deployment.run_fenced(
            definition_v2,
            resolver_v2,
            snapshot_v2,
            lambda: store.record_outcome(view_v2),
        )
        after_write_before_certificate = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="allow_stale_with_marker",
        )
        assert after_write_before_certificate.state == "stale"
        assert after_write_before_certificate.outcome == view_v2
        assert "pending_generation" in after_write_before_certificate.reason
        assert "projection_hash_not_certified" in after_write_before_certificate.reason

        deployment.run_fenced(
            definition_v2,
            resolver_v2,
            snapshot_v2,
            lambda: freshness.certify(
                definition_v2,
                target=target,
                outcome=view_v2,
                derivation_id="derivation-v2",
                derivation_resolver_version=2,
                generation=work_v2.generation,
            ),
        )
        assert deployment.pending_work(resolver_v1.resolver_id) == ()

        fresh_v2 = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        assert fresh_v2.state == "fresh"
        assert fresh_v2.outcome == view_v2
        assert fresh_v2.certified_resolver_version == 2
        assert fresh_v2.derivation_id == "derivation-v2"
        assert fresh_v2.work_generation == fresh_v2.completed_generation == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_generation_ack_without_new_projection_certificate_cannot_make_old_view_fresh() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        freshness = TrustResolverProjectionFreshness(
            repo,
            namespace=NAMESPACE,
            envelope_store=store,
        )
        store.activate()
        deployment.activate()

        target = _target()
        resolver_v1 = ResolverV1()
        definition_v1 = _definition(resolver_v1)
        deployment.publish_definition(definition_v1)
        deployment.register_dependency(definition_v1, target)
        snapshot_v1 = deployment.capture_snapshot(definition_v1)
        view_v1 = _view("v1", "v1-ack")

        deployment.run_fenced(
            definition_v1,
            resolver_v1,
            snapshot_v1,
            lambda: (
                store.record_outcome(view_v1),
                freshness.certify(
                    definition_v1,
                    target=target,
                    outcome=view_v1,
                    derivation_id="ack-v1",
                    derivation_resolver_version=1,
                    generation=0,
                ),
            ),
        )

        resolver_v2 = ResolverV2()
        definition_v2 = _definition(resolver_v2)
        deployment.publish_definition(definition_v2)
        work_v2 = deployment.pending_work(resolver_v1.resolver_id)[0]
        view_v2 = _view("v2", "v2-ack")
        snapshot_v2 = deployment.capture_snapshot(definition_v2)
        deployment.run_fenced(
            definition_v2,
            resolver_v2,
            snapshot_v2,
            lambda: (
                store.record_outcome(view_v2),
                freshness.certify(
                    definition_v2,
                    target=target,
                    outcome=view_v2,
                    derivation_id="ack-v2",
                    derivation_resolver_version=2,
                    generation=work_v2.generation,
                ),
            ),
        )
        assert freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
        ).fresh

        # V3 publication invalidates the target. Deliberately misuse the weaker Experiment-16
        # generation acknowledgement without writing/certifying any v3 projection.
        resolver_v3 = ResolverV3()
        definition_v3 = _definition(resolver_v3)
        deployment.publish_definition(definition_v3)
        work_v3 = deployment.pending_work(resolver_v1.resolver_id)[0]
        deployment.complete_work(work_v3, definition=definition_v3)

        # Scheduler work now looks complete, but the projection certificate is still v2.
        assert deployment.pending_work(resolver_v1.resolver_id) == ()
        strict = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        assert strict.state == "unavailable"
        assert strict.outcome is None
        assert "resolver_version_not_certified" in strict.reason

        marked = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="allow_stale_with_marker",
        )
        assert marked.state == "stale"
        assert marked.outcome == view_v2
        assert marked.current_resolver_version == 3
        assert marked.work_generation == marked.completed_generation == 2
        assert marked.certified_resolver_version == 2
        assert marked.derivation_id == "ack-v2"
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_certificate_rejects_expected_outcome_that_was_not_persisted() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        freshness = TrustResolverProjectionFreshness(
            repo,
            namespace=NAMESPACE,
            envelope_store=store,
        )
        store.activate()
        deployment.activate()

        target = _target()
        resolver_v1 = ResolverV1()
        definition_v1 = _definition(resolver_v1)
        deployment.publish_definition(definition_v1)
        deployment.register_dependency(definition_v1, target)
        persisted = _view("persisted", "persisted")
        store.record_outcome(persisted)

        different_expected = _view("not-persisted", "not-persisted")
        with pytest.raises(ValueError, match="does not match derivation"):
            freshness.certify(
                definition_v1,
                target=target,
                outcome=different_expected,
                derivation_id="wrong-hash",
                derivation_resolver_version=1,
                generation=0,
            )
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_unregistered_projection_is_never_treated_as_fresh_by_semantic_reader() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        freshness = TrustResolverProjectionFreshness(
            repo,
            namespace=NAMESPACE,
            envelope_store=store,
        )
        store.activate()
        deployment.activate()

        resolver_v1 = ResolverV1()
        definition_v1 = _definition(resolver_v1)
        deployment.publish_definition(definition_v1)
        target = _target()
        raw = _view("raw-unregistered", "raw")
        store.record_outcome(raw)
        assert store.load_views(
            subject_id=target.subject_id,
            view_type=target.view_type,
            scope=target.scope,
            key=target.key,
            dimensions=target.dimensions,
        ) == [raw]

        result = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="allow_stale_with_marker",
        )
        assert result.state == "unavailable"
        assert result.reason == "unregistered_dependency"
        assert result.outcome is None
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
