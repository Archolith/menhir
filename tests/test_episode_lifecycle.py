"""Tests for EpisodeLifecycleRepository — context retry logic fix."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository


def _make_repo(execute_return=None):
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = execute_return or []
    return repo


class TestClaimContextRetryDefault:
    """Verify context_retry_attempts=0 disables extra context retries."""

    def test_context_retry_none_defaults_to_max_attempts(self):
        """When context_retry_attempts is None, context_cap = max_attempts."""
        repo = _make_repo()
        repo.claim_pending_episode(
            "uuid-1",
            max_attempts=3,
            context_retry_attempts=None,
            worker_id="w1",
            lease_seconds=60,
        )
        # The claim query should be called with context_retry_attempts = max(3, 3) = 3
        call_args = repo.neo4j.execute.call_args_list
        # Second call is the claim query
        assert len(call_args) == 2
        claim_params = call_args[1].kwargs.get("params") or call_args[1][1].get("params", {})
        assert claim_params["context_retry_attempts"] == 3

    def test_context_retry_zero_means_no_extra_retries(self):
        """When context_retry_attempts=0 explicitly, context_cap = max(max_attempts, 0) = max_attempts."""
        repo = _make_repo()
        repo.claim_pending_episode(
            "uuid-2",
            max_attempts=3,
            context_retry_attempts=0,
            worker_id="w1",
            lease_seconds=60,
        )
        call_args = repo.neo4j.execute.call_args_list
        assert len(call_args) == 2
        claim_params = call_args[1].kwargs.get("params") or call_args[1][1].get("params", {})
        # max(3, 0) = 3 — no extra context retries
        assert claim_params["context_retry_attempts"] == 3

    def test_context_retry_higher_than_max(self):
        """When context_retry_attempts > max_attempts, context_cap uses the higher value."""
        repo = _make_repo()
        repo.claim_pending_episode(
            "uuid-3",
            max_attempts=3,
            context_retry_attempts=5,
            worker_id="w1",
            lease_seconds=60,
        )
        call_args = repo.neo4j.execute.call_args_list
        assert len(call_args) == 2
        claim_params = call_args[1].kwargs.get("params") or call_args[1][1].get("params", {})
        assert claim_params["context_retry_attempts"] == 5


class TestClaimProjectionCarriesWorldTime:
    """The claim query must PROJECT every field enrichment consumes, not merely store it.

    `occurred_at` is parsed into `reference_time` and written onto the pending :Episodic node
    correctly -- but the claim projection omitted it, so `ctx.claimed.get("reference_time")` was
    always None and the enrichment handoff fell through to `ctx.claimed.get("queued_at")`. Every
    backdated episode was therefore stamped in graphiti with its INGESTION time, and supersession
    ordered history by import order instead of event order. 1707 of 2862 episodes (62%) of the
    lme-scalar-ku corpus landed that way.

    Note the sibling comment in the same projection about `namespace`: this is the second time a
    field was stored correctly and projected nowhere. Hence a test on the projection itself.
    """

    #: Fields the enrichment worker reads off `ctx.claimed`. Each must survive the claim.
    REQUIRED = (
        "uuid",
        "name",
        "content",
        "source",
        "session_id",
        "user_id",
        "namespace",
        "reference_time",
        "queued_at",
        "turn_evidence_uuid",
    )

    def _claim_query(self):
        repo = _make_repo()
        repo.claim_pending_episode(
            "uuid-1", max_attempts=3, context_retry_attempts=None,
            worker_id="w1", lease_seconds=60,
        )
        # call 0 is the precheck; call 1 is the claim itself.
        call = repo.neo4j.execute.call_args_list[1]
        return call.args[0] if call.args else call.kwargs["query"]

    @pytest.mark.parametrize("field", REQUIRED)
    def test_claim_projects_field(self, field):
        assert f"AS {field}" in self._claim_query(), (
            f"claim_pending_episode does not project {field!r}; enrichment will silently read None")

    def test_reference_time_is_projected_not_just_stored(self):
        """The specific regression: without this the world time is replaced by the queue time."""
        query = self._claim_query()
        assert "n.reference_time AS reference_time" in query
        assert "n.queued_at AS queued_at" in query, (
            "queued_at is the intended fallback for live turns and must stay")
    def test_detects_context_window_markers(self):
        from menhir.infrastructure.episode_lifecycle import is_context_window_error_text

        assert is_context_window_error_text("cannot truncate prompt to fit")
        assert is_context_window_error_text("exceeds the available context window")
        assert is_context_window_error_text("n_ctx exceeded")
        assert not is_context_window_error_text("timeout error")
        assert not is_context_window_error_text(None)
        assert not is_context_window_error_text("")

    def test_detects_hosted_provider_context_limit(self):
        """The OpenAI phrasing was missing, so a 400 fell through to the manual_review default."""
        from menhir.infrastructure.episode_lifecycle import is_context_window_error_text

        openai_400 = (
            "Error code: 400 - {'error': {'message': \"This model's maximum context length is "
            "128000 tokens.\", 'code': 'context_length_exceeded'}}"
        )
        assert is_context_window_error_text(openai_400)

    def test_only_operator_clearable_errors_earn_extended_retries(self):
        """The retry budget must NOT widen for a provider's fixed limit.

        A local `n_ctx` error clears when the operator reloads the model with a bigger
        window -- the same payload then succeeds, so extra attempts are worth spending. A
        hosted provider's ceiling is not ours to raise and the payload is unchanged, so
        retrying can only burn attempts. Both are context-window errors for classification;
        only the first one is retryable.
        """
        from menhir.infrastructure.episode_lifecycle import (
            is_context_window_error_text,
            is_recoverable_context_window_error,
        )

        local = "cannot truncate prompt to fit n_ctx"
        hosted = "This model's maximum context length is 128000 tokens"

        assert is_recoverable_context_window_error(local)
        assert is_context_window_error_text(local)

        assert is_context_window_error_text(hosted)
        assert not is_recoverable_context_window_error(hosted)

    def test_hosted_provider_limit_classifies_terminal(self):
        from menhir.services.enrichment_failures import classify_enrichment_failure

        assert classify_enrichment_failure(
            "This model's maximum context length is 128000 tokens"
        ) == "terminal"


class TestFetchEpisodeProcessingNoDuplicateColumns:
    """Regression guard: fetch_episode_processing's RETURN clause must not repeat a
    column alias. MEMORY_RETURN_FIELDS already carries processing_substage/
    processing_substage_started_at and the active LLM task/kind/model/endpoint fields
    (SSOT-11); this call site used to re-add them explicitly, producing a RETURN
    clause Neo4j rejects with "Multiple result columns with the same name are not
    supported" — silently tolerated until caught live during the 2026-07-12
    graphiti-core 0.29.2 upgrade canary (this path only runs during background
    enrichment finalize, never exercised by the mocked offline suite)."""

    def test_no_duplicate_return_aliases(self):
        repo = _make_repo()
        repo.fetch_episode_processing("ep-uuid-1")
        call_args = repo.neo4j.execute.call_args_list
        assert len(call_args) == 1
        query = call_args[0].args[0] if call_args[0].args else call_args[0].kwargs["query"]
        # Every "AS <alias>" in the RETURN clause must be unique.
        aliases = [line.strip().rsplit(" AS ", 1)[-1].rstrip(",") for line in query.splitlines() if " AS " in line]
        assert len(aliases) == len(set(aliases)), f"duplicate RETURN aliases: {aliases}"


class TestEnsureSelfEntityAbsorbsForks:
    """One self identity per namespace, even though two independent creators mint it.

    `ensure_self_entity` writes the canonical uuid5 node; combined-extraction now names the self
    endpoint so a first-person edge survives, which mints a second `user` :Entity. Both fire in any
    namespace where scalar consolidation runs, so the fork is deterministic, not a dedup race.
    """

    @staticmethod
    def _statements(repo):
        return [c.args[0] for c in repo.neo4j.execute.call_args_list if c.args]

    def test_no_fork_means_no_rewiring(self):
        """The common case (extraction never named a self endpoint) must stay a plain MERGE."""
        repo = _make_repo([])
        repo.ensure_self_entity("ns-1")
        assert not any("DETACH DELETE" in s for s in self._statements(repo))

    def test_fork_is_rewired_then_deleted(self):
        repo = _make_repo([{"uuid": "fork-1"}])
        repo.ensure_self_entity("ns-1")
        joined = "\n".join(self._statements(repo))
        assert "MERGE (c)-[r2:RELATES_TO {uuid: r.uuid}]->(m)" in joined
        assert "MERGE (m)-[r2:RELATES_TO {uuid: r.uuid}]->(c)" in joined
        assert "MERGE (e)-[:MENTIONS]->(c)" in joined
        assert "DETACH DELETE f" in joined

    def test_entity_edge_payload_is_carried_across(self):
        """`SET r2 = properties(r)` — a rewire that drops the payload keeps the graph's shape and
        loses the fact the edge carried (fact, fact_embedding, episodes, valid_at)."""
        repo = _make_repo([{"uuid": "fork-1"}])
        repo.ensure_self_entity("ns-1")
        rewires = [s for s in self._statements(repo) if "RELATES_TO" in s]
        assert rewires
        assert all("SET r2 = properties(r)" in s for s in rewires)

    def test_embedding_is_carried_before_the_fork_dies(self):
        """The canonical node is raw-Cypher written and has no name_embedding, so it is invisible to
        semantic search. Folding an embedded node into it without the vector would move every stated
        fact onto a node recall cannot find — strictly worse than the fork."""
        repo = _make_repo([{"uuid": "fork-1"}])
        repo.ensure_self_entity("ns-1")
        statements = self._statements(repo)
        carry = [i for i, s in enumerate(statements) if "c.name_embedding = coalesce(" in s]
        delete = [i for i, s in enumerate(statements) if "DETACH DELETE" in s]
        assert carry and delete and carry[0] < delete[0]

    def test_views_are_never_absorbed(self):
        """A View is a projection of the subject; absorbing one would fold a derived answer into the
        thing it describes."""
        repo = _make_repo([])
        repo.ensure_self_entity("ns-1")
        find = self._statements(repo)[1]
        assert "NOT coalesce(f.is_view, false)" in find
        assert "NOT coalesce(f.is_quantstate, false)" in find
        assert "f.view_kind IS NULL" in find

    def test_absorbed_summary_survives_a_later_ensure(self):
        """The canned self summary is ON CREATE only. Setting it unconditionally would overwrite the
        richer absorbed summary on the very next consolidation pass."""
        repo = _make_repo([])
        repo.ensure_self_entity("ns-1")
        merge = self._statements(repo)[0]
        assert "ON CREATE SET n.created_at = datetime(), n.summary = $summary" in merge
        assert "SET n.name = $name" in merge
        assert "n.summary = $summary," not in merge

    def test_self_loops_are_not_rewired(self):
        """An edge from the twin back to the canonical node is an artifact of the split, not a fact."""
        repo = _make_repo([{"uuid": "fork-1"}])
        repo.ensure_self_entity("ns-1")
        rewires = [s for s in self._statements(repo) if "RELATES_TO" in s]
        assert all("m.uuid <> $self_uuid" in s for s in rewires)

    def test_returns_the_deterministic_uuid_unchanged(self):
        """Recall computes uuid5("menhir-self:<ns>") with no DB read, so absorbing must not move it."""
        import uuid as _uuid
        repo = _make_repo([{"uuid": "fork-1"}])
        got = repo.ensure_self_entity("ns-1")
        assert got == str(_uuid.uuid5(_uuid.NAMESPACE_URL, "menhir-self:ns-1"))


class TestLinkedEntitiesAcceptsTurnEvidenceId:
    """G14 regression: the scalar batch keys on `turn_id` when :TurnEvidence exists, but the binder
    looked that id up as an :Episodic uuid, so the candidate list was empty on EVERY call and every
    non-self subject abstained. The failure was invisible because `_resolve_subject` tries the self
    seam first and that path never consults this lookup."""

    def _query(self, repo):
        call = repo.neo4j.execute.call_args
        return call.args[0] if call.args else call.kwargs["query"]

    def test_matches_episode_uuid_directly(self):
        """The original contract still holds: a real Episodic uuid binds without any traversal."""
        repo = _make_repo([{"uuid": "e-1", "name": "shifts"}])
        got = repo.fetch_linked_entities_for_episode("episode-uuid-1")
        assert got == [{"uuid": "e-1", "name": "shifts"}]
        assert "a.uuid = $episode_uuid" in self._query(repo)

    def test_follows_the_resolved_twin_where_entities_actually_attach(self):
        """Entities attach to GRAPHITI's episode node, not menhir's pending one. They are
        content-identical twins written by different producers, joined by `resolved_episode_uuid`.
        Anchoring only on the node the id names returns NOTHING on a live ingest: measured on a
        fresh isolated build, every assertion's ADMITTED_ON hop landed on the pending node with
        entities=0 while its twin held 5. The earlier verification missed this because the backfill
        script joined on `group_id`, which ONLY graphiti's nodes carry -- so a backfilled graph
        pointed at the right node and a real ingest did not."""
        repo = _make_repo([])
        repo.fetch_linked_entities_for_episode("turn-id-1")
        query = self._query(repo)
        assert "resolved_episode_uuid" in query
        assert "collect(DISTINCT a) + collect(DISTINCT g)" in query

    def test_resolves_a_turn_id_through_admitted_on(self):
        """A turn_id must reach the episode's entities via the ADMITTED_ON admission join."""
        repo = _make_repo([{"uuid": "e-1", "name": "agents"}])
        repo.fetch_linked_entities_for_episode("turn-id-1")
        query = self._query(repo)
        assert "ADMITTED_ON" in query
        assert "TurnEvidence" in query
        assert "turn_id: $episode_uuid" in query

    def test_both_id_kinds_are_one_disjunction_not_two_round_trips(self):
        """One query, not a probe-then-fallback: a second round trip would double the per-assertion
        cost of the hot binding path and could straddle a concurrent write."""
        repo = _make_repo([])
        repo.fetch_linked_entities_for_episode("some-id")
        assert repo.neo4j.execute.call_count == 1
        assert " OR EXISTS" in self._query(repo)

    def test_still_excludes_derived_view_nodes(self):
        """Views are :Entity too and are MENTIONS-linked to their source episode. Binding a scalar
        assertion to a projection would make a View its own subject."""
        repo = _make_repo([
            {"uuid": "v-1", "name": "user's shifts", "is_view": True},
            {"uuid": "q-1", "name": "counter", "is_quantstate": True},
            {"uuid": "s-1", "name": "scalar", "view_kind": "scalar_state"},
            {"uuid": "e-1", "name": "shifts"},
        ])
        got = repo.fetch_linked_entities_for_episode("turn-id-1")
        assert got == [{"uuid": "e-1", "name": "shifts"}]

    def test_view_filter_applies_to_the_turn_id_path_too(self):
        """The Cypher-level View exclusion must not be scoped to only one branch of the disjunction."""
        repo = _make_repo([])
        repo.fetch_linked_entities_for_episode("turn-id-1")
        query = self._query(repo)
        assert "n.view_kind IS NULL" in query
        assert "is_view" in query and "is_quantstate" in query

    def test_blank_uuid_rows_are_dropped(self):
        repo = _make_repo([{"uuid": "", "name": "ghost"}, {"uuid": "e-1", "name": "shifts"}])
        assert repo.fetch_linked_entities_for_episode("turn-id-1") == [{"uuid": "e-1", "name": "shifts"}]


class TestLookupEntitiesByNormalizedNames:
    """The optional same-namespace repository fallback for typed-scalar binding (C.4.3) is exact and
    fail-closed: nonblank namespace, group_id equality, View exclusion, blank-uuid exclusion."""

    def _query_and_params(self, repo):
        call = repo.neo4j.execute.call_args
        query = call.args[0] if call.args else call.kwargs["query"]
        params = (call.kwargs.get("params") if call.args else call.kwargs.get("params")) or {}
        return query, params

    def test_blank_namespace_returns_empty_without_querying(self):
        repo = _make_repo([{"uuid": "e-1", "name": "black shoes"}])
        got = repo.lookup_entities_by_normalized_names("   ", ["black shoes"])
        assert got == []
        repo.neo4j.execute.assert_not_called()

    def test_empty_spellings_returns_empty_without_querying(self):
        repo = _make_repo([{"uuid": "e-1", "name": "black shoes"}])
        assert repo.lookup_entities_by_normalized_names("ns", []) == []
        repo.neo4j.execute.assert_not_called()

    def test_query_uses_group_id_equality_for_same_namespace(self):
        repo = _make_repo([])
        repo.lookup_entities_by_normalized_names("tenant-a", ["black shoes"])
        query, params = self._query_and_params(repo)
        assert "n.group_id = $namespace" in query                 # same-namespace, not cross-tenant
        assert "toLower(trim(n.name)) IN $spellings" in query     # exact normalized-name equality
        assert params["namespace"] == "tenant-a"
        assert params["spellings"] == ["black shoes"]

    def test_query_excludes_derived_views_and_blank_uuids(self):
        repo = _make_repo([])
        repo.lookup_entities_by_normalized_names("ns", ["black shoes"])
        query, _params = self._query_and_params(repo)
        assert "NOT coalesce(n.is_view, false)" in query
        assert "NOT coalesce(n.is_quantstate, false)" in query
        assert "n.view_kind IS NULL" in query
        assert "trim(toString(n.uuid)) <> ''" in query

    def test_returns_only_non_blank_uuid_rows(self):
        repo = _make_repo([
            {"uuid": "", "name": "black shoes"},
            {"uuid": "e-1", "name": "black shoes"},
        ])
        got = repo.lookup_entities_by_normalized_names("ns", ["black shoes"])
        assert got == [{"uuid": "e-1", "name": "black shoes"}]

class TestAdapterDelegatesNormalizedNameLookup:
    """The MemoryGraphAdapter façade must forward the same-namespace lookup to the episode repo, so the
    service's `getattr(adapter, 'lookup_entities_by_normalized_names', None)` seam actually reaches the
    repository."""

    def test_adapter_forwards_to_episode_repository(self):
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        inner = MagicMock()
        inner.execute.return_value = [
            {"uuid": "e-1", "name": "black shoes", "is_view": False,
             "is_quantstate": False, "view_kind": None},
            {"uuid": "e-2", "name": "black shoes", "is_view": True,
             "is_quantstate": False, "view_kind": "scalar_state"},
        ]
        adapter = MemoryGraphAdapter(neo4j=inner)
        got = adapter.lookup_entities_by_normalized_names("tenant-a", ["black shoes"])
        # the façade returns only the non-View {uuid, name} rows for the binder.
        assert got == [{"uuid": "e-1", "name": "black shoes"}]

    def test_adapter_requires_nonblank_namespace(self):
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        inner = MagicMock()
        adapter = MemoryGraphAdapter(neo4j=inner)
        assert adapter.lookup_entities_by_normalized_names("", ["black shoes"]) == []
        inner.execute.assert_not_called()
