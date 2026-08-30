"""Tests for the Cypher query builder and shared field constants."""

import pytest

from menhir.infrastructure.cypher import (
    Cypher,
    ENTITY_METADATA_FIELDS,
    EPISODE_CLAIM_FIELDS,
    EPISODE_PROCESSING_FIELDS,
    EPISODE_RETRY_FIELDS,
    LLM_RESET_SET,
    MEMORY_RETURN_FIELDS,
    build_reset_or_fail_query,
)


# ---------------------------------------------------------------------------
# Builder — individual clause methods
# ---------------------------------------------------------------------------


class TestCypherMatch:
    def test_match_basic(self):
        q = Cypher().match("(n:Entity)").build()
        assert q == "MATCH (n:Entity)"

    def test_match_relationship(self):
        q = Cypher().match("(a)-[r:RELATES_TO]->(b)").build()
        assert q == "MATCH (a)-[r:RELATES_TO]->(b)"


class TestCypherOptionalMatch:
    def test_optional_match(self):
        q = Cypher().optional_match("(n)-[r]->(m)").build()
        assert q == "OPTIONAL MATCH (n)-[r]->(m)"


class TestCypherWhere:
    def test_single_condition(self):
        q = Cypher().where("n.uuid = $uuid").build()
        assert q == "WHERE n.uuid = $uuid"

    def test_multiple_conditions(self):
        q = Cypher().where("n:Entity OR n:Episodic", "n.scope = $scope").build()
        assert "WHERE n:Entity OR n:Episodic" in q
        assert "AND n.scope = $scope" in q

    def test_three_conditions(self):
        q = Cypher().where("a = 1", "b = 2", "c = 3").build()
        assert q.count("AND") == 2

    def test_where_no_args_is_noop(self):
        """where() with no conditions produces nothing."""
        q = Cypher().match("(n)").where().build()
        assert q == "MATCH (n)"


class TestCypherWhereIf:
    def test_where_if_true_appends_to_existing_where(self):
        q = (Cypher()
            .where("n.scope = 'SESSION'")
            .where_if(True, "n.session_id = $session_id")
            .build())
        assert "WHERE n.scope = 'SESSION'" in q
        assert "AND n.session_id = $session_id" in q
        assert q.count("WHERE") == 1

    def test_where_if_false_skips(self):
        q = (Cypher()
            .where("n.scope = 'SESSION'")
            .where_if(False, "n.session_id = $session_id")
            .build())
        assert "session_id" not in q

    def test_where_if_no_prior_where_starts_new(self):
        q = (Cypher()
            .match("(n)")
            .where_if(True, "n.x = 1")
            .build())
        assert "WHERE n.x = 1" in q

    def test_where_if_no_prior_where_false_does_nothing(self):
        q = Cypher().match("(n)").where_if(False, "n.x = 1").build()
        assert "WHERE" not in q

    def test_where_if_multiple_conditionals(self):
        session_id = "abc"
        max_age = 0
        q = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'")
            .where_if(session_id is not None, "n.session_id = $session_id")
            .where_if(max_age > 0, "n.created_at < $cutoff")
            .build())
        assert "n.session_id = $session_id" in q
        assert "$cutoff" not in q
        assert q.count("WHERE") == 1

    def test_where_if_appends_to_last_where_not_first(self):
        """After a WITH, where_if targets the second WHERE block."""
        q = (Cypher()
            .match("(n)")
            .where("n.x = 1")
            .with_clause("n")
            .match("(n)-[r]->(m)")
            .where("m.y = 2")
            .where_if(True, "m.z = 3")
            .build())
        # First WHERE untouched, second WHERE has the merged condition
        assert q.count("WHERE") == 2
        assert "WHERE n.x = 1" in q
        assert "m.y = 2" in q
        assert "AND m.z = 3" in q

    def test_where_if_with_none_value(self):
        val = None
        q = (Cypher()
            .match("(n)")
            .where("n.x = 1")
            .where_if(val is not None, "n.y = $y")
            .build())
        assert "n.y" not in q

    def test_where_if_fluent(self):
        c = Cypher()
        assert c.where_if(True, "x = 1") is c
        assert c.where_if(False, "y = 2") is c


class TestCypherWithClause:
    def test_with_clause(self):
        q = Cypher().with_clause("n, count(*) AS cnt").build()
        assert q == "WITH n, count(*) AS cnt"


class TestCypherCreate:
    def test_create(self):
        q = Cypher().create("(n:Entity {uuid: $uuid})").build()
        assert q == "CREATE (n:Entity {uuid: $uuid})"


class TestCypherMerge:
    def test_merge(self):
        q = Cypher().merge("(a)-[r:RELATES_TO]->(b)").build()
        assert q == "MERGE (a)-[r:RELATES_TO]->(b)"

    def test_merge_with_on_create_set(self):
        q = (Cypher()
            .merge("(n:Entity {uuid: $uuid})")
            .on_create_set("n.created_at = datetime()")
            .on_match_set("n.last_accessed = datetime()")
            .build())
        assert "MERGE" in q
        assert "ON CREATE SET n.created_at = datetime()" in q
        assert "ON MATCH SET n.last_accessed = datetime()" in q


class TestCypherDelete:
    def test_delete(self):
        q = Cypher().delete("n").build()
        assert q == "DELETE n"

    def test_detach_delete(self):
        q = Cypher().detach_delete("n").build()
        assert q == "DETACH DELETE n"

    def test_detach_delete_in_query(self):
        q = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid = $uuid")
            .detach_delete("n")
            .build())
        assert "MATCH (n:Entity)" in q
        assert "DETACH DELETE n" in q


class TestCypherUnwind:
    def test_unwind(self):
        q = Cypher().unwind("$items AS item").build()
        assert q == "UNWIND $items AS item"

    def test_unwind_in_query(self):
        q = (Cypher()
            .unwind("$uuids AS uuid")
            .match("(n {uuid: uuid})")
            .return_raw("n")
            .build())
        assert "UNWIND $uuids AS uuid" in q
        assert "MATCH" in q


class TestCypherSkip:
    def test_skip_default(self):
        q = Cypher().skip().build()
        assert q == "SKIP $skip"

    def test_skip_custom(self):
        q = Cypher().skip("10").build()
        assert q == "SKIP 10"

    def test_skip_and_limit(self):
        q = Cypher().skip("$offset").limit("$page_size").build()
        lines = q.split("\n")
        assert lines[0] == "SKIP $offset"
        assert lines[1] == "LIMIT $page_size"


class TestCypherOnCreateOnMatch:
    def test_on_create_set(self):
        q = Cypher().on_create_set("n.x = 1").build()
        assert q == "ON CREATE SET n.x = 1"

    def test_on_match_set(self):
        q = Cypher().on_match_set("n.x = 1").build()
        assert q == "ON MATCH SET n.x = 1"

    def test_on_create_set_tuple(self):
        q = Cypher().on_create_set(("n.a = 1", "n.b = 2")).build()
        assert "ON CREATE SET n.a = 1" in q
        assert "n.b = 2" in q

    def test_on_match_set_list(self):
        q = Cypher().on_match_set(["n.a = 1", "n.b = 2"]).build()
        assert "ON MATCH SET n.a = 1" in q
        assert "n.b = 2" in q


class TestCypherSet:
    def test_set_string(self):
        q = Cypher().set("n.name = $name").build()
        assert q == "SET n.name = $name"

    def test_set_tuple(self):
        fields = ("n.a = 1", "n.b = 2", "n.c = 3")
        q = Cypher().set(fields).build()
        assert q.startswith("SET n.a = 1")
        assert "n.b = 2" in q
        assert "n.c = 3" in q

    def test_set_single_element_tuple(self):
        q = Cypher().set(("n.x = 42",)).build()
        assert q == "SET n.x = 42"

    def test_set_with_llm_reset_constant(self):
        q = Cypher().set(LLM_RESET_SET).build()
        assert "processing_llm_tasks_attempt = 0" in q
        assert "processing_llm_active_task = null" in q
        assert "processing_llm_active_kind = null" in q
        assert "processing_llm_active_model = null" in q
        assert "processing_llm_active_endpoint = null" in q

    def test_set_accepts_list(self):
        fields = ["n.a = 1", "n.b = 2"]
        q = Cypher().set(fields).build()
        assert "n.a = 1" in q
        assert "n.b = 2" in q

    def test_set_list_dynamically_built(self):
        """Simulate the conditional SET pattern from the adapter."""
        sets: list[str] = ["n.processing_state = 'PENDING'"]
        sets.append("n.processing_owner = null")
        if True:  # simulating a condition
            sets.append("n.processing_error = null")
        q = Cypher().set(sets).build()
        assert q.count("SET ") == 1
        assert "processing_state" in q
        assert "processing_owner" in q
        assert "processing_error" in q


class TestCypherReturnFields:
    def test_return_from_tuple(self):
        fields = ("n.uuid AS uuid", "n.name AS name")
        q = Cypher().return_fields(fields).build()
        assert q.startswith("RETURN n.uuid AS uuid")
        assert "n.name AS name" in q

    def test_return_with_extra_fields(self):
        fields = ("n.uuid AS uuid",)
        q = Cypher().return_fields(fields, "count(*) AS total").build()
        assert "n.uuid AS uuid" in q
        assert "count(*) AS total" in q

    def test_return_with_memory_fields(self):
        q = Cypher().return_fields(MEMORY_RETURN_FIELDS).build()
        assert "labels(n) AS labels" in q
        assert "n.uuid AS uuid" in q
        assert "n.processing_error AS processing_error" in q

    def test_return_with_entity_metadata_fields(self):
        q = Cypher().return_fields(ENTITY_METADATA_FIELDS).build()
        assert "n.sharpness AS sharpness" in q
        assert "rehydration_count" in q

    def test_return_with_episode_claim_fields(self):
        q = Cypher().return_fields(EPISODE_CLAIM_FIELDS).build()
        assert "n.content AS content" in q
        assert "n.processing_owner AS processing_owner" in q

    def test_return_with_episode_processing_fields(self):
        q = Cypher().return_fields(EPISODE_PROCESSING_FIELDS).build()
        assert "n.processing_substage AS processing_substage" in q
        assert "n.processing_llm_active_model AS processing_llm_active_model" in q

    def test_return_with_episode_retry_fields(self):
        q = Cypher().return_fields(EPISODE_RETRY_FIELDS).build()
        assert "n.processing_error AS processing_error" in q
        assert "processing_completed_at" in q


class TestCypherReturnRaw:
    def test_return_raw(self):
        q = Cypher().return_raw("count(n) AS total").build()
        assert q == "RETURN count(n) AS total"


class TestCypherOrderBy:
    def test_order_by(self):
        q = Cypher().order_by("n.created_at DESC").build()
        assert q == "ORDER BY n.created_at DESC"


class TestCypherLimit:
    def test_limit_default(self):
        q = Cypher().limit().build()
        assert q == "LIMIT $limit"

    def test_limit_custom(self):
        q = Cypher().limit("10").build()
        assert q == "LIMIT 10"

    def test_limit_named_param(self):
        q = Cypher().limit("$page_size").build()
        assert q == "LIMIT $page_size"


class TestCypherRaw:
    def test_raw_appends_verbatim(self):
        block = "CALL { WITH n DETACH DELETE n RETURN 1 AS x }"
        q = Cypher().raw(block).build()
        assert q == block

    def test_raw_unwind(self):
        q = Cypher().raw("UNWIND $items AS item").build()
        assert q == "UNWIND $items AS item"


# ---------------------------------------------------------------------------
# Builder — chaining and composition
# ---------------------------------------------------------------------------


class TestCypherChaining:
    def test_fluent_returns_same_instance(self):
        c = Cypher()
        assert c.match("(n)") is c
        assert c.optional_match("(m)") is c
        assert c.where("n.x = 1") is c
        assert c.where_if(True, "n.y = 2") is c
        assert c.with_clause("n") is c
        assert c.create("(m:Entity)") is c
        assert c.merge("(p:Entity)") is c
        assert c.delete("m") is c
        assert c.detach_delete("p") is c
        assert c.set("n.a = 1") is c
        assert c.on_create_set("n.b = 2") is c
        assert c.on_match_set("n.c = 3") is c
        assert c.unwind("$items AS item") is c
        assert c.return_fields(("n.uuid AS uuid",)) is c
        assert c.return_raw("count(*)") is c
        assert c.order_by("n.name") is c
        assert c.skip() is c
        assert c.limit() is c
        assert c.raw("// comment") is c

    def test_full_read_query(self):
        q = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids")
            .return_fields(ENTITY_METADATA_FIELDS)
            .order_by("n.name")
            .limit()
            .build())

        assert "MATCH (n:Entity)" in q
        assert "WHERE n.uuid IN $uuids" in q
        assert "RETURN" in q
        assert "n.sharpness AS sharpness" in q
        assert "ORDER BY n.name" in q
        assert "LIMIT $limit" in q

    def test_full_write_query(self):
        q = (Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $uuid", "n.processing_owner = $worker_id")
            .set(LLM_RESET_SET)
            .return_raw("count(n) AS updated")
            .build())

        assert "MATCH (n:Episodic)" in q
        assert "WHERE n.uuid = $uuid" in q
        assert "AND n.processing_owner = $worker_id" in q
        assert "SET" in q
        assert "processing_llm_tasks_attempt = 0" in q
        assert "RETURN count(n) AS updated" in q

    def test_optional_match_with_clause(self):
        q = (Cypher()
            .match("(n:Entity)")
            .optional_match("(n)-[r:RELATES_TO]->(m)")
            .with_clause("n, count(r) AS edge_count")
            .return_raw("n.uuid AS uuid, edge_count")
            .build())

        assert "MATCH (n:Entity)" in q
        assert "OPTIONAL MATCH (n)-[r:RELATES_TO]->(m)" in q
        assert "WITH n, count(r) AS edge_count" in q
        assert "RETURN n.uuid AS uuid, edge_count" in q

    def test_consecutive_sets_merge(self):
        """Two .set() calls merge into a single SET block."""
        q = (Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $uuid")
            .set(("n.processing_state = 'READY'", "n.processing_stage = 'ready'"))
            .set(LLM_RESET_SET)
            .return_raw("n.uuid AS uuid")
            .build())

        assert q.count("SET ") == 1
        assert "processing_state = 'READY'" in q
        assert "processing_llm_active_task = null" in q

    def test_sets_separated_by_raw_stay_separate(self):
        """A raw() between two .set() calls prevents merging."""
        q = (Cypher()
            .set("n.a = 1")
            .raw("// separator")
            .set("n.b = 2")
            .build())

        assert q.count("SET ") == 2

    def test_return_fields_with_extras(self):
        q = (Cypher()
            .match("(n:Entity)")
            .return_fields(ENTITY_METADATA_FIELDS, "n.extra AS extra")
            .build())

        assert "n.uuid AS uuid" in q
        assert "n.extra AS extra" in q

    def test_return_fields_and_return_raw_merge(self):
        """return_fields + return_raw merge into one RETURN."""
        q = (Cypher()
            .return_fields(("n.uuid AS uuid",))
            .return_raw("count(*) AS total")
            .build())

        assert q.count("RETURN") == 1
        assert "n.uuid AS uuid" in q
        assert "count(*) AS total" in q

    def test_realistic_conditional_query(self):
        """Mirrors the fetch_session_entities pattern from memory_graph_adapter."""
        session_id = "abc-123"
        max_age_hours = 0  # disabled

        q = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'")
            .where_if(session_id is not None, "n.session_id = $session_id")
            .where_if(max_age_hours > 0, "n.created_at < datetime() - duration({hours: $max_age_hours})")
            .return_fields(ENTITY_METADATA_FIELDS)
            .order_by("n.created_at DESC")
            .limit()
            .build())

        assert "MATCH (n:Entity)" in q
        assert "n.scope = 'SESSION'" in q
        assert "n.session_id = $session_id" in q
        assert "max_age_hours" not in q
        # RETURN projections now include provenance subqueries with their own WHERE clauses.
        # Count only the builder's top-level session predicate.
        assert q.splitlines().count("WHERE n.scope = 'SESSION'") == 1
        assert "RETURN" in q
        assert "ORDER BY" in q
        assert "LIMIT" in q

    def test_realistic_merge_with_sets(self):
        """Mirrors the consolidation edge merge pattern."""
        q = (Cypher()
            .unwind("$neighbors AS a")
            .unwind("$neighbors AS b")
            .match("(a_node) WHERE a_node.uuid = a")
            .match("(b_node) WHERE b_node.uuid = b")
            .merge("(a_node)-[r:RELATES_TO]->(b_node)")
            .on_create_set("r.weight = 1.0, r.created_at = datetime()")
            .on_match_set("r.weight = r.weight + 0.1")
            .return_raw("count(r) AS edges_created")
            .build())

        assert "UNWIND $neighbors AS a" in q
        assert "MERGE" in q
        assert "ON CREATE SET" in q
        assert "ON MATCH SET" in q

    def test_realistic_reset_episode(self):
        """Mirrors the episode reset pattern with LLM_RESET_SET."""
        q = (Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $uuid",
                   "n.processing_state = 'FAILED'")
            .set(LLM_RESET_SET + (
                "n.processing_state = 'PENDING'",
                "n.processing_stage = 'queued'",
                "n.processing_owner = null",
                "n.processing_lease_expires_at = null",
                "n.processing_error = null",
            ))
            .return_raw("count(n) AS reset")
            .build())

        assert "MATCH (n:Episodic)" in q
        assert q.count("SET ") == 1
        assert "processing_llm_tasks_attempt = 0" in q
        assert "processing_state = 'PENDING'" in q
        assert "processing_error = null" in q

    def test_realistic_delete_with_detach(self):
        """Mirrors the orphan cleanup pattern."""
        q = (Cypher()
            .match("(n:Entity)")
            .where("NOT n.uuid IN $exclude_uuids",
                   "NOT EXISTS { MATCH (n)-[]-() }")
            .detach_delete("n")
            .return_raw("count(n) AS deleted")
            .build())

        assert "MATCH (n:Entity)" in q
        assert "NOT n.uuid IN $exclude_uuids" in q
        assert "DETACH DELETE n" in q


class TestCypherBuild:
    def test_empty_build(self):
        assert Cypher().build() == ""

    def test_parts_joined_with_newlines(self):
        q = Cypher().match("(n)").where("n.x = 1").build()
        lines = q.split("\n")
        assert len(lines) == 2
        assert lines[0] == "MATCH (n)"
        assert lines[1] == "WHERE n.x = 1"

    def test_independent_instances(self):
        """Two builder instances don't share state."""
        a = Cypher().match("(a)")
        b = Cypher().match("(b)")
        assert "a" in a.build()
        assert "b" in b.build()
        assert "a" not in b.build()


# ---------------------------------------------------------------------------
# Merging behavior — the core of the declarative builder
# ---------------------------------------------------------------------------


class TestCypherMerging:
    """Consecutive mergeable ops (WHERE, SET, RETURN) combine into one clause."""

    def test_consecutive_wheres_merge(self):
        q = Cypher().where("a = 1").where("b = 2").build()
        assert q.count("WHERE") == 1
        assert "AND b = 2" in q

    def test_consecutive_sets_merge(self):
        q = Cypher().set("n.a = 1").set("n.b = 2").build()
        assert q.count("SET ") == 1
        assert "n.a = 1" in q
        assert "n.b = 2" in q

    def test_consecutive_returns_merge(self):
        q = Cypher().return_raw("a").return_raw("b").build()
        assert q.count("RETURN") == 1
        assert "a" in q
        assert "b" in q

    def test_consecutive_on_create_sets_merge(self):
        q = (Cypher()
            .on_create_set("n.a = 1")
            .on_create_set("n.b = 2")
            .build())
        assert q.count("ON CREATE SET") == 1
        assert "n.a = 1" in q
        assert "n.b = 2" in q

    def test_consecutive_on_match_sets_merge(self):
        q = (Cypher()
            .on_match_set("n.a = 1")
            .on_match_set("n.b = 2")
            .build())
        assert q.count("ON MATCH SET") == 1

    def test_with_breaks_where_merge(self):
        """WITH between two WHEREs keeps them separate (multi-stage query)."""
        q = (Cypher()
            .match("(n)")
            .where("n.x = 1")
            .with_clause("n")
            .match("(m)")
            .where("m.y = 2")
            .build())
        assert q.count("WHERE") == 2

    def test_raw_breaks_set_merge(self):
        """RAW between two SETs keeps them separate."""
        q = (Cypher()
            .set("n.a = 1")
            .raw("// break")
            .set("n.b = 2")
            .build())
        assert q.count("SET ") == 2

    def test_match_breaks_where_merge(self):
        """MATCH between two WHEREs keeps them separate."""
        q = (Cypher()
            .where("a = 1")
            .match("(n)")
            .where("b = 2")
            .build())
        assert q.count("WHERE") == 2

    def test_where_if_merges_with_preceding_where(self):
        """where_if merges with a directly preceding where()."""
        q = (Cypher()
            .where("n.x = 1")
            .where_if(True, "n.y = 2")
            .where_if(True, "n.z = 3")
            .build())
        assert q.count("WHERE") == 1
        assert q.count("AND") == 2

    def test_set_tuple_and_set_string_merge(self):
        """set(tuple) followed by set(string) merge into one block."""
        q = (Cypher()
            .set(("n.a = 1", "n.b = 2"))
            .set("n.c = 3")
            .build())
        assert q.count("SET ") == 1
        assert "n.a = 1" in q
        assert "n.c = 3" in q

    def test_set_with_llm_reset_plus_extras_merge(self):
        """LLM_RESET_SET + extra fields via separate .set() calls merge."""
        q = (Cypher()
            .set(LLM_RESET_SET)
            .set("n.processing_state = 'PENDING'")
            .set("n.processing_owner = null")
            .build())
        assert q.count("SET ") == 1
        assert "processing_llm_tasks_attempt = 0" in q
        assert "processing_state = 'PENDING'" in q
        assert "processing_owner = null" in q


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestCypherEdgeCases:
    """Edge cases for production resilience."""

    # -- Empty inputs --

    def test_set_empty_tuple_is_noop(self):
        q = Cypher().match("(n)").set(()).build()
        assert q == "MATCH (n)"

    def test_set_empty_list_is_noop(self):
        q = Cypher().match("(n)").set([]).build()
        assert q == "MATCH (n)"

    def test_return_fields_empty_tuple_is_noop(self):
        q = Cypher().match("(n)").return_fields(()).build()
        assert q == "MATCH (n)"

    def test_return_fields_empty_tuple_with_extras(self):
        """Empty base tuple but extras provided — only extras appear."""
        q = Cypher().return_fields((), "count(*) AS total").build()
        assert q == "RETURN count(*) AS total"

    # -- Duplicate clause calls (non-mergeable) --

    def test_double_match(self):
        """Two MATCH calls produce two MATCH lines (valid Cypher)."""
        q = Cypher().match("(a:Entity)").match("(b:Episodic)").build()
        lines = q.split("\n")
        assert lines[0] == "MATCH (a:Entity)"
        assert lines[1] == "MATCH (b:Episodic)"

    def test_double_limit(self):
        """LIMIT is not mergeable — two calls produce two lines."""
        q = Cypher().limit().limit("10").build()
        assert q.count("LIMIT") == 2

    # -- Build behavior --

    def test_build_is_idempotent(self):
        """Calling build() twice returns the same result."""
        c = Cypher().match("(n)").where("n.x = 1")
        assert c.build() == c.build()

    def test_mutation_after_build(self):
        """Adding clauses after build() extends the query."""
        c = Cypher().match("(n)")
        first = c.build()
        c.where("n.x = 1")
        second = c.build()
        assert first != second
        assert "WHERE" not in first
        assert "WHERE" in second

    # -- Tuple concatenation for SET --

    def test_set_concatenated_tuples(self):
        """Combining LLM_RESET_SET with extra fields via tuple concatenation."""
        combined = LLM_RESET_SET + (
            "n.processing_state = 'PENDING'",
            "n.processing_owner = null",
        )
        q = Cypher().set(combined).build()
        assert "processing_llm_tasks_attempt = 0" in q
        assert "processing_llm_active_endpoint = null" in q
        assert "processing_state = 'PENDING'" in q
        assert "processing_owner = null" in q
        assert q.count("SET ") == 1

    # -- Order preservation --

    def test_clause_order_preserved(self):
        """Output lines match the call order exactly."""
        q = (Cypher()
            .match("(n)")
            .where("n.x = 1")
            .with_clause("n")
            .match("(n)-[r]->(m)")
            .return_raw("n, m")
            .order_by("n.name")
            .limit()
            .build())
        lines = q.split("\n")
        assert lines[0] == "MATCH (n)"
        assert lines[1] == "WHERE n.x = 1"
        assert lines[2] == "WITH n"
        assert lines[3] == "MATCH (n)-[r]->(m)"
        assert lines[4] == "RETURN n, m"
        assert lines[5] == "ORDER BY n.name"
        assert lines[6] == "LIMIT $limit"

    # -- Multiline raw --

    def test_raw_multiline_block(self):
        block = "CALL {\n  WITH n\n  DETACH DELETE n\n  RETURN 1 AS x\n}"
        q = Cypher().match("(n)").raw(block).build()
        assert q.startswith("MATCH (n)\n")
        assert "CALL {" in q
        assert "DETACH DELETE n" in q

    def test_raw_preserves_indentation(self):
        block = "    SET n.x = 1,\n        n.y = 2"
        q = Cypher().raw(block).build()
        assert q == block

    # -- Special characters --

    def test_backtick_labels(self):
        q = Cypher().match("(n:`My Label`)").build()
        assert q == "MATCH (n:`My Label`)"

    def test_where_with_string_literal(self):
        q = Cypher().where("n.name = 'O\\'Brien'").build()
        assert "O\\'Brien" in q


# ---------------------------------------------------------------------------
# Field constant integrity
# ---------------------------------------------------------------------------


class TestFieldConstants:
    """Verify field constants are tuples with expected structure."""

    @pytest.mark.parametrize("fields,min_count", [
        (MEMORY_RETURN_FIELDS, 28),
        (ENTITY_METADATA_FIELDS, 12),
        (EPISODE_CLAIM_FIELDS, 10),
        (EPISODE_PROCESSING_FIELDS, 22),
        (EPISODE_RETRY_FIELDS, 8),
    ])
    def test_return_field_tuple_size(self, fields, min_count):
        assert isinstance(fields, tuple)
        assert len(fields) >= min_count

    @pytest.mark.parametrize("fields", [
        MEMORY_RETURN_FIELDS,
        ENTITY_METADATA_FIELDS,
        EPISODE_CLAIM_FIELDS,
        EPISODE_PROCESSING_FIELDS,
        EPISODE_RETRY_FIELDS,
    ])
    def test_return_fields_contain_as_alias(self, fields):
        """Every RETURN field should have an AS alias."""
        for field in fields:
            assert " AS " in field, f"Missing AS alias: {field}"

    def test_llm_reset_set_is_tuple(self):
        assert isinstance(LLM_RESET_SET, tuple)
        assert len(LLM_RESET_SET) == 5

    def test_llm_reset_set_are_assignments(self):
        """Every SET field should be an assignment (contains '=')."""
        for field in LLM_RESET_SET:
            assert "=" in field, f"Not an assignment: {field}"

    def test_memory_return_fields_starts_with_labels(self):
        assert MEMORY_RETURN_FIELDS[0] == "labels(n) AS labels"

    def test_memory_return_fields_has_uuid(self):
        assert "n.uuid AS uuid" in MEMORY_RETURN_FIELDS

    def test_entity_metadata_has_scoring_fields(self):
        names = {f.split(" AS ")[-1] for f in ENTITY_METADATA_FIELDS}
        assert "sharpness" in names
        assert "edge_count" in names
        assert "freshness" in names
        assert "rehydration_count" in names

    def test_episode_claim_has_owner_and_lease(self):
        names = {f.split(" AS ")[-1] for f in EPISODE_CLAIM_FIELDS}
        assert "processing_owner" in names
        assert "processing_lease_expires_at" in names

    def test_episode_processing_has_substage_fields(self):
        names = {f.split(" AS ")[-1] for f in EPISODE_PROCESSING_FIELDS}
        assert "processing_substage" in names
        assert "processing_substage_started_at" in names

    def test_episode_processing_has_llm_active_fields(self):
        names = {f.split(" AS ")[-1] for f in EPISODE_PROCESSING_FIELDS}
        assert "processing_llm_active_task" in names
        assert "processing_llm_active_kind" in names
        assert "processing_llm_active_model" in names
        assert "processing_llm_active_endpoint" in names

    def test_episode_retry_has_error_and_completed(self):
        names = {f.split(" AS ")[-1] for f in EPISODE_RETRY_FIELDS}
        assert "processing_error" in names
        assert "processing_completed_at" in names

    def test_memory_return_fields_is_processing_field_superset(self):
        """Regression (SSOT-11): MEMORY_RETURN_FIELDS silently omitted
        processing_substage/processing_substage_started_at and the active LLM
        task/kind/model/endpoint fields that EPISODE_PROCESSING_FIELDS already
        carried -- the two "full projection" views drifted. Both now compose
        the processing-detail block from a shared tuple in cypher.py, so every
        processing_* field EPISODE_PROCESSING_FIELDS reports must also appear
        in MEMORY_RETURN_FIELDS. `namespace` is exempt: it's not a processing
        field, and MEMORY_RETURN_FIELDS deliberately doesn't project it (that's
        a separate, unrelated design choice, not this finding's drift)."""
        memory_names = {f.split(" AS ")[-1].strip() for f in MEMORY_RETURN_FIELDS}
        episode_names = {f.split(" AS ")[-1].strip() for f in EPISODE_PROCESSING_FIELDS}
        processing_only = {n for n in episode_names if n.startswith("processing_")}
        missing = processing_only - memory_names
        assert not missing, f"MEMORY_RETURN_FIELDS is missing processing fields: {missing}"

    def test_no_duplicate_aliases_in_return_fields(self):
        """Each RETURN field set should have unique aliases."""
        for label, fields in [
            ("MEMORY_RETURN_FIELDS", MEMORY_RETURN_FIELDS),
            ("ENTITY_METADATA_FIELDS", ENTITY_METADATA_FIELDS),
            ("EPISODE_CLAIM_FIELDS", EPISODE_CLAIM_FIELDS),
            ("EPISODE_PROCESSING_FIELDS", EPISODE_PROCESSING_FIELDS),
            ("EPISODE_RETRY_FIELDS", EPISODE_RETRY_FIELDS),
        ]:
            aliases = [f.split(" AS ")[-1].strip() for f in fields]
            assert len(aliases) == len(set(aliases)), f"Duplicate alias in {label}: {aliases}"


# ---------------------------------------------------------------------------
# Reset-or-fail query template
# ---------------------------------------------------------------------------


class TestBuildResetOrFailQuery:
    """Tests for the build_reset_or_fail_query template function."""

    def test_basic_output_structure(self):
        """Template produces MATCH, WHERE, WITH, SET, RETURN blocks."""
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        lines = query.split("\n")
        assert lines[0] == "MATCH (n:Episodic)"
        assert any("WHERE" in l for l in lines)
        assert any("WITH n" in l for l in lines)
        assert any("SET" in l for l in lines)
        assert lines[-1] == "RETURN count(n) AS reset"

    def test_custom_match_pattern(self):
        query = build_reset_or_fail_query(
            match="(n:Episodic {uuid: $uuid})",
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert query.startswith("MATCH (n:Episodic {uuid: $uuid})")

    def test_custom_return_alias(self):
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
            return_alias="released",
        )
        assert "RETURN count(n) AS released" in query

    def test_multiple_where_conditions(self):
        query = build_reset_or_fail_query(
            where=[
                "n.processing_state = 'ENRICHING'",
                "n.processing_owner = $worker_id",
            ],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "n.processing_state = 'ENRICHING'" in query
        assert "n.processing_owner = $worker_id" in query

    def test_exhausted_flag_computed_via_with(self):
        """The exhausted flag is pre-computed in a WITH clause."""
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert ("WITH n, coalesce(toInteger(n.processing_attempts), 0)"
                " >= $max_attempts AS exhausted") in query

    def test_state_stage_bifurcation(self):
        """CASE WHEN exhausted branches for state and stage."""
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'sub_exhausted'",
            reset_substage="'sub_reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "CASE WHEN exhausted THEN 'FAILED' ELSE 'PENDING' END" in query
        assert "CASE WHEN exhausted THEN 'failed' ELSE 'queued' END" in query

    def test_substage_bifurcation(self):
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'sub_exhausted'",
            reset_substage="'sub_reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "CASE WHEN exhausted THEN 'sub_exhausted' ELSE 'sub_reset' END" in query

    def test_error_bifurcation(self):
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'sub_exhausted'",
            reset_substage="'sub_reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "CASE WHEN exhausted THEN 'err_exhausted' ELSE 'err_reset' END" in query

    def test_llm_reset_fields_included(self):
        """All LLM_RESET_SET fields appear in the output."""
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        for field in LLM_RESET_SET:
            assert field in query, f"Missing LLM reset field: {field}"

    def test_common_set_fields_included(self):
        """Common processing fields (owner, lease, heartbeat, etc.) are present."""
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "n.processing_owner = null" in query
        assert "n.processing_lease_expires_at = null" in query
        assert "n.processing_heartbeat_at = datetime()" in query
        assert "n.processing_started_at = null" in query
        assert "n.processing_substage_started_at = datetime()" in query

    def test_progress_and_completed_at_bifurcation(self):
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert ("CASE WHEN exhausted"
                " THEN coalesce(n.processing_progress, 100.0) ELSE 0.0 END") in query
        assert "CASE WHEN exhausted THEN datetime() ELSE null END" in query

    def test_steps_completed_bifurcation(self):
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'exhausted'",
            reset_substage="'reset'",
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "coalesce(toInteger(n.processing_steps_completed)" in query
        assert "coalesce(toInteger(n.processing_steps_total), 5)" in query

    def test_nested_case_expressions(self):
        """Substage/error params can contain nested CASE expressions."""
        query = build_reset_or_fail_query(
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage=(
                "CASE WHEN n.processing_lease_expires_at IS NULL"
                " THEN 'lease_missing_exhausted'"
                " ELSE 'lease_expired_exhausted' END"
            ),
            reset_substage=(
                "CASE WHEN n.processing_lease_expires_at IS NULL"
                " THEN 'lease_missing_reset'"
                " ELSE 'lease_expired_reset' END"
            ),
            exhausted_error="'err_exhausted'",
            reset_error="'err_reset'",
        )
        assert "'lease_missing_exhausted'" in query
        assert "'lease_expired_exhausted'" in query
        assert "'lease_missing_reset'" in query
        assert "'lease_expired_reset'" in query

    def test_mirrors_orphaned_reset_pattern(self):
        """Produces correct output for the orphaned reset use case."""
        query = build_reset_or_fail_query(
            where=[
                "n.processing_state = 'ENRICHING'",
                "(n.processing_owner IS NULL OR trim(toString(n.processing_owner)) = '')",
            ],
            exhausted_substage="'orphaned_reset_attempts_exhausted'",
            reset_substage="'orphaned_reset'",
            exhausted_error="'orphaned_enriching_reset_attempts_exhausted'",
            reset_error="'orphaned_enriching_reset'",
        )
        assert "MATCH (n:Episodic)" in query
        assert "'orphaned_reset_attempts_exhausted'" in query
        assert "'orphaned_reset'" in query
        assert "'orphaned_enriching_reset_attempts_exhausted'" in query
        assert "'orphaned_enriching_reset'" in query

    def test_mirrors_force_release_pattern(self):
        """Produces correct output for the force-release use case."""
        query = build_reset_or_fail_query(
            match="(n:Episodic {uuid: $uuid})",
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'lease_released_attempts_exhausted'",
            reset_substage="'lease_released'",
            exhausted_error="'manual_lease_release_attempts_exhausted'",
            reset_error="'manual_lease_release'",
            return_alias="released",
        )
        assert query.startswith("MATCH (n:Episodic {uuid: $uuid})")
        assert "'lease_released_attempts_exhausted'" in query
        assert "'manual_lease_release'" in query
        assert query.endswith("RETURN count(n) AS released")


# ---------------------------------------------------------------------------
# __all__ export list
# ---------------------------------------------------------------------------


class TestExports:
    def test_all_exports(self):
        from menhir.infrastructure import cypher
        expected = {
            "Cypher",
            "MEMORY_RETURN_FIELDS",
            "ENTITY_METADATA_FIELDS",
            "EPISODE_CLAIM_FIELDS",
            "EPISODE_PROCESSING_FIELDS",
            "EPISODE_RETRY_FIELDS",
            "FACT_TEMPORAL_FIELDS",
            "SHADOW_CANDIDATE_FACT_EDGE_FIELDS",
            "LLM_RESET_SET",
            "build_reset_or_fail_query",
            # CF-200: the shared View-exclusion predicate. This test exists to force a deliberate
            # decision whenever the module's public surface changes, which is exactly what adding
            # a shared Cypher fragment helper is.
            "non_derived_view_cypher",
        }
        assert set(cypher.__all__) == expected
