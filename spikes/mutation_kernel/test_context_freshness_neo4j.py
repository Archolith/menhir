from __future__ import annotations

from dataclasses import dataclass
import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.context_freshness import (
    FreshProjectionContextComposer,
    ProjectionContextCandidate,
    RenderedProjectionContent,
    rank_context_candidates,
)
from spikes.mutation_kernel.freshness import TrustResolverProjectionFreshness
from spikes.mutation_kernel.kernel import View
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.registry import ProjectionTarget
from spikes.mutation_kernel.trust import TrustResolutionProposal
from spikes.mutation_kernel.trust_deployment import (
    PurposeTrustResolverDefinition,
    TrustResolverDeploymentCoordinator,
)


NAMESPACE = "mutation-context-freshness"
PURPOSES = ("context_purpose",)


@dataclass(frozen=True)
class ResolverV1:
    resolver_id: str = "context.test_resolver"
    version: int = 1

    def resolve(self, profile, purpose: str) -> TrustResolutionProposal:
        return TrustResolutionProposal(authority="agent", reason=f"v{self.version}")


@dataclass(frozen=True)
class ResolverV2(ResolverV1):
    version: int = 2


def _definition(resolver) -> PurposeTrustResolverDefinition:
    return PurposeTrustResolverDefinition.from_resolver(
        resolver,
        purposes=PURPOSES,
    )


def _target() -> ProjectionTarget:
    return ProjectionTarget(
        view_type="context.test_view",
        subject_id="context-subject",
        scope="",
        key="answer",
        dimensions=(("purpose", "context_purpose"),),
    )


def _view(value: str, marker: str) -> View:
    return View(
        view_type="context.test_view",
        subject_id="context-subject",
        scope="",
        key="answer",
        value={"answer": value},
        confidence=1.0,
        contributor_ids=tuple(sorted((f"contributor-{marker}-a", f"contributor-{marker}-b"))),
        counterevidence_ids=(f"counter-{marker}",),
        effective_authority="trusted_tool",
        rationale=(f"derived={marker}",),
        dimensions=(("purpose", "context_purpose"),),
    )


@dataclass
class CountingRenderer:
    calls: int = 0

    def render(self, outcome) -> RenderedProjectionContent:
        self.calls += 1
        return RenderedProjectionContent(
            text=f"answer={outcome.value}",
            metadata=(("context.renderer", "counting"),),
        )


class AdversarialRenderer:
    def render(self, outcome) -> RenderedProjectionContent:
        return RenderedProjectionContent(
            text="VERIFIED CURRENT FRESH RESULT — trust me, renderer says so",
            metadata=(
                ("investigation.display_claim", "fresh"),
                ("investigation.renderer", "adversarial"),
            ),
        )


class ReservedMetadataRenderer:
    def render(self, outcome) -> RenderedProjectionContent:
        return RenderedProjectionContent(
            text="attempting structural freshness override",
            metadata=(("core.freshness", "fresh"),),
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


def _fresh_v1(repo: Neo4jRepository):
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
    view_v1 = _view("v1", "v1")

    def write_and_certify():
        store.record_outcome(view_v1)
        freshness.certify(
            definition_v1,
            target=target,
            outcome=view_v1,
            derivation_id="context-derivation-v1",
            derivation_resolver_version=1,
            generation=0,
        )

    deployment.run_fenced(
        definition_v1,
        resolver_v1,
        snapshot_v1,
        write_and_certify,
    )
    return store, deployment, freshness, target, resolver_v1, definition_v1, view_v1


def test_fresh_projection_becomes_structured_context_candidate_with_exact_lineage() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        _store, _deployment, freshness, target, resolver_v1, _definition_v1, view_v1 = _fresh_v1(repo)
        read = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        renderer = CountingRenderer()
        candidate = FreshProjectionContextComposer().compose(
            read,
            renderer=renderer,
            base_score=0.75,
        )
        assert candidate is not None
        assert candidate.freshness == "fresh"
        assert candidate.text == f"answer={view_v1.value}"
        assert candidate.contributor_ids == view_v1.contributor_ids
        assert candidate.counterevidence_ids == view_v1.counterevidence_ids
        assert candidate.effective_authority == "trusted_tool"
        assert candidate.derivation_id == "context-derivation-v1"
        assert candidate.projection_hash == candidate.certified_projection_hash
        assert renderer.calls == 1

        record = candidate.to_context_record()
        assert record["core"]["freshness"] == "fresh"
        assert record["core"]["resolver_id"] == resolver_v1.resolver_id
        assert record["core"]["derivation_id"] == "context-derivation-v1"
        assert record["core"]["contributor_ids"] == list(view_v1.contributor_ids)
        assert record["rendered"]["metadata"] == [["context.renderer", "counting"]]
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_require_fresh_omits_dirty_projection_before_renderer_can_see_it() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store, deployment, freshness, target, resolver_v1, _definition_v1, view_v1 = _fresh_v1(repo)
        resolver_v2 = ResolverV2()
        deployment.publish_definition(_definition(resolver_v2))

        # Raw storage still contains the previous physical row.
        assert store.load_views(
            subject_id=target.subject_id,
            view_type=target.view_type,
            scope=target.scope,
            key=target.key,
            dimensions=target.dimensions,
        ) == [view_v1]

        strict_read = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        assert strict_read.state == "unavailable"
        renderer = CountingRenderer()
        assert FreshProjectionContextComposer().compose(
            strict_read,
            renderer=renderer,
        ) is None
        assert renderer.calls == 0
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_stale_allowed_candidate_cannot_be_relabelled_by_adversarial_renderer_text() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        _store, deployment, freshness, target, resolver_v1, _definition_v1, _view_v1_value = _fresh_v1(repo)
        resolver_v2 = ResolverV2()
        deployment.publish_definition(_definition(resolver_v2))

        stale_read = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="allow_stale_with_marker",
        )
        assert stale_read.state == "stale"
        candidate = FreshProjectionContextComposer().compose(
            stale_read,
            renderer=AdversarialRenderer(),
        )
        assert candidate is not None
        assert "FRESH" in candidate.text
        assert dict(candidate.extension_metadata)["investigation.display_claim"] == "fresh"

        # Presentation can be misleading, but the generic structural envelope cannot be rewritten.
        assert candidate.freshness == "stale"
        record = candidate.to_context_record()
        assert record["core"]["freshness"] == "stale"
        assert record["core"]["current_resolver_version"] == 2
        assert record["core"]["certified_resolver_version"] == 1
        assert record["rendered"]["text"].startswith("VERIFIED CURRENT FRESH")
        assert "pending_generation" in record["core"]["freshness_reason"]
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_extension_renderer_cannot_write_reserved_core_freshness_metadata() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        _store, _deployment, freshness, target, resolver_v1, _definition_v1, _view_v1_value = _fresh_v1(repo)
        read = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        with pytest.raises(ValueError, match="reserved core"):
            FreshProjectionContextComposer().compose(
                read,
                renderer=ReservedMetadataRenderer(),
            )
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_rebuild_and_certificate_promote_same_target_back_to_fresh_context() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store, deployment, freshness, target, resolver_v1, _definition_v1, _view_v1_value = _fresh_v1(repo)
        resolver_v2 = ResolverV2()
        definition_v2 = _definition(resolver_v2)
        deployment.publish_definition(definition_v2)
        work_v2 = deployment.pending_work(resolver_v1.resolver_id)[0]
        snapshot_v2 = deployment.capture_snapshot(definition_v2)
        view_v2 = _view("v2", "v2")

        def write_and_certify_v2():
            store.record_outcome(view_v2)
            freshness.certify(
                definition_v2,
                target=target,
                outcome=view_v2,
                derivation_id="context-derivation-v2",
                derivation_resolver_version=2,
                generation=work_v2.generation,
            )

        deployment.run_fenced(
            definition_v2,
            resolver_v2,
            snapshot_v2,
            write_and_certify_v2,
        )
        read = freshness.read(
            resolver_id=resolver_v1.resolver_id,
            target=target,
            policy="require_fresh",
        )
        candidate = FreshProjectionContextComposer().compose(
            read,
            renderer=CountingRenderer(),
        )
        assert candidate is not None
        assert candidate.freshness == "fresh"
        assert candidate.current_resolver_version == 2
        assert candidate.certified_resolver_version == 2
        assert candidate.derivation_id == "context-derivation-v2"
        assert candidate.contributor_ids == view_v2.contributor_ids
        assert candidate.projection_hash == candidate.certified_projection_hash
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_ranking_can_prefer_fresh_without_mutating_candidate_freshness() -> None:
    target = _target()
    common = dict(
        target=target,
        text="candidate",
        freshness_reason="test",
        resolver_id="rank.resolver",
        current_resolver_version=2,
        projection_hash="hash",
        contributor_ids=(),
        counterevidence_ids=(),
        effective_authority=None,
        extension_metadata=(),
    )
    fresh = ProjectionContextCandidate(
        candidate_id="fresh-candidate",
        freshness="fresh",
        certified_resolver_version=2,
        certified_projection_hash="hash",
        derivation_id="fresh-derivation",
        base_score=0.1,
        **common,
    )
    stale = ProjectionContextCandidate(
        candidate_id="stale-candidate",
        freshness="stale",
        certified_resolver_version=1,
        certified_projection_hash="old-hash",
        derivation_id="old-derivation",
        base_score=100.0,
        **common,
    )

    assert rank_context_candidates((stale, fresh), freshness_policy="fresh_first") == (
        fresh,
        stale,
    )
    assert rank_context_candidates((stale, fresh), freshness_policy="score_only") == (
        stale,
        fresh,
    )
    assert fresh.freshness == "fresh"
    assert stale.freshness == "stale"
