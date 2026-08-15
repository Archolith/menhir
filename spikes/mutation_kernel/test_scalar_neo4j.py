from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.view_repository import ViewRepository
from menhir.services import scalar_state_service, typed_scalar_persistence
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.typed_scalar_persistence import bind_and_persist_typed_scalars
from menhir.services.typed_scalar_rules import extract_typed_scalars_once, gate_typed_scalars

from spikes.mutation_kernel.kernel import View
from spikes.mutation_kernel.scalar_adapter import (
    SCALAR_VIEW,
    adapt_fold_result,
    scalar_dimensions,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "scalar_ingest_fixture.json"


@dataclass(frozen=True)
class Episode:
    uuid: str
    content: str
    reference_time: str


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _extract_samples(data: dict, episodes: list[Episode]) -> list[list]:
    samples = []
    for capture in data["success_samples"]:
        drops: list[str] = []
        proposals = extract_typed_scalars_once(
            episodes,
            lambda _system, _user, capture=capture: json.dumps(capture),
            on_drop=drops.append,
        )
        assert drops == []
        samples.append(proposals)
    return samples


def test_frozen_scalar_fixture_persists_and_projects_through_real_neo4j(monkeypatch) -> None:
    """Frozen model capture -> real deterministic ingest -> real Neo4j -> real projection -> kernel.

    This deliberately does not call a live LLM or Graphiti.  Everything after the frozen model
    response uses Menhir production code, including proposal parsing/gating, canonical-self binding,
    TypedAssertion persistence, ScalarStateService rebuild, View persistence, and contributor edges.
    The test skips unless an explicit throwaway test Neo4j is supplied.
    """
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

    data = _load_fixture()
    namespace = str(data["namespace"])
    subject_uuid = str(data["subject_uuid"])
    episodes = [Episode(**row) for row in data["episodes"]]
    reference_times = {episode.uuid: episode.reference_time for episode in episodes}

    assertions = TypedAssertionRepository(repo)
    views = ViewRepository(repo)
    service = ScalarStateService(assertions, views, scalar_history_enabled=False)

    # Keep this branch-only integration test out of the operator telemetry sink. The graph itself is
    # real and throwaway; telemetry is not part of the scalar persistence/projection contract tested.
    monkeypatch.setattr(typed_scalar_persistence._audit, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(scalar_state_service._audit, "audit", lambda *args, **kwargs: None)

    try:
        # The workflow gives this test its own service container. Start clean, then activate the real
        # scalar-state DDL so the same uniqueness constraints protecting production writes are present.
        repo.execute("MATCH (n) DETACH DELETE n")
        assertions.activate_scalar_state()

        repo.execute(
            "MERGE (u:Entity {uuid:$uuid}) "
            "SET u.name='user', u.group_id=$namespace",
            {"uuid": subject_uuid, "namespace": namespace},
        )
        repo.execute(
            "UNWIND $rows AS row "
            "MERGE (e:Episodic {uuid:row.uuid}) "
            "SET e.content=row.content, e.reference_time=datetime(row.reference_time), "
            "    e.group_id=$namespace",
            {
                "namespace": namespace,
                "rows": [
                    {
                        "uuid": episode.uuid,
                        "content": episode.content,
                        "reference_time": episode.reference_time,
                    }
                    for episode in episodes
                ],
            },
        )

        samples = _extract_samples(data, episodes)
        decisions = gate_typed_scalars(samples, threshold=1.0, canonical_self=True)
        assert len(decisions) == 2
        assert all(decision.committed for decision in decisions)

        def rebuild(subject: str) -> dict:
            return service.rebuild_scalar_state(subject, namespace=namespace)

        ingest = bind_and_persist_typed_scalars(
            decisions,
            linked_entities_for_episode=lambda _episode_uuid: [],
            record_assertion=assertions.record_assertion,
            rebuild_scalar_state=rebuild,
            episode_reference_time=reference_times.get,
            namespace=namespace,
            perceiver_version="v1",
            now=lambda: "2026-02-01T00:00:00+00:00",
            mark_projection_complete=assertions.mark_projection_complete,
            resolve_self_subject=lambda _namespace: (subject_uuid, "user"),
        )

        assert ingest["persisted"] == 2
        assert ingest["bound"] == 2
        assert ingest["advisory"] == 0
        assert ingest["mismatched"] == 0
        assert ingest["rebuilt"] == 2

        durable = assertions.materializable_assertions_for_entity(
            subject_uuid, namespace=namespace
        )
        assert len(durable) == 2
        assert {row["operation"] for row in durable} == {"absolute", "delta"}

        native = service.fold_entity(subject_uuid, namespace=namespace)
        assert len(native.states) == 1
        assert native.states[0].value == 13
        assert native.states[0].anchor_value == 10
        assert native.states[0].delta_total == 3
        assert native.states[0].effective_tier == "agent"

        persisted = views.fetch_current_scalar_view_for_slot(
            subject_uuid=subject_uuid,
            attribute="token_balance",
            scope="",
            value_kind="count",
            unit="",
            namespace=namespace,
        )
        assert persisted is not None
        assert str(persisted["value"]) == "13"

        current_views = views.list_scalar_state_views(
            subject_uuid=subject_uuid, namespace=namespace
        )
        assert len(current_views) == 1

        provenance = views.fetch_scalar_authority_contributors(
            view_uuid=str(persisted["uuid"]), namespace=namespace
        )
        assert provenance["total"] == 2
        assert [row["relation"] for row in provenance["contributors"]] == [
            "CURRENT_ANCHOR",
            "CONTRIBUTED_TO",
        ]

        adapted = adapt_fold_result(native)
        assert len(adapted) == 1
        generic = adapted[0]
        assert isinstance(generic, View)
        assert generic.view_type == SCALAR_VIEW
        assert generic.subject_id == subject_uuid
        assert generic.key == "token_balance"
        assert generic.scope == ""
        assert generic.value == 13
        assert generic.dimensions == scalar_dimensions("count", "")
        assert generic.contributor_ids == tuple(native.states[0].contributor_ids)
        assert generic.effective_authority == native.states[0].effective_tier

        # Replay the same already-grounded claims. Production assertion identity must dedupe and the
        # authoritative rebuild must converge on the same one current View rather than multiplying it.
        replay = bind_and_persist_typed_scalars(
            decisions,
            linked_entities_for_episode=lambda _episode_uuid: [],
            record_assertion=assertions.record_assertion,
            rebuild_scalar_state=rebuild,
            episode_reference_time=reference_times.get,
            namespace=namespace,
            perceiver_version="v1",
            now=lambda: "2026-02-01T00:00:00+00:00",
            mark_projection_complete=assertions.mark_projection_complete,
            resolve_self_subject=lambda _namespace: (subject_uuid, "user"),
        )
        assert replay["persisted"] == 2
        assert replay["rebuilt"] == 2

        assertion_count = repo.execute(
            "MATCH (a:TypedAssertion {namespace:$namespace}) RETURN count(a) AS n",
            {"namespace": namespace},
        )[0]["n"]
        current_view_count = repo.execute(
            "MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid:$uuid, group_id:$namespace}) "
            "WHERE coalesce(v.view_current, true) RETURN count(v) AS n",
            {"uuid": subject_uuid, "namespace": namespace},
        )[0]["n"]
        assert assertion_count == 2
        assert current_view_count == 1

        replayed = views.fetch_current_scalar_view_for_slot(
            subject_uuid=subject_uuid,
            attribute="token_balance",
            scope="",
            value_kind="count",
            unit="",
            namespace=namespace,
        )
        assert replayed is not None
        assert str(replayed["value"]) == "13"
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
