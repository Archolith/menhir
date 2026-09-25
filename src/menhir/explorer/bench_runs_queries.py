"""Live Cypher query constants for the bench-run task reader.

Ported from ``archolith_bench/scalar_viewer.py``; extracted from
``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

_LIVE_EVIDENCE_QUERY = """
MATCH (t:TurnEvidence {namespace: $namespace})
OPTIONAL MATCH (t)-[:FOUNDS]->(a:TypedAssertion {namespace: $namespace})
RETURN t.turn_id AS id,
       coalesce(t.role, t.declarant, 'unknown') AS role,
       t.text AS text,
       t.session_id AS session_id,
       toString(t.occurred_at) AS occurred_at,
       toString(t.recorded_at) AS recorded_at,
       collect(DISTINCT a.assertion_id) AS founds
ORDER BY recorded_at, id
"""

_LIVE_ASSERTIONS_QUERY = """
MATCH (a:TypedAssertion {namespace: $namespace})
RETURN a.assertion_id AS id,
       a.source_key AS source_key,
       a.episode_uuid AS evidence_id,
       a.subject_uuid AS subject_uuid,
       a.subject_display AS subject,
       a.attribute AS attribute,
       a.scope AS scope,
       a.value_kind AS value_kind,
       a.unit AS unit,
       a.operation AS operation,
       a.value AS value,
       a.stated_span AS stated_span,
       toString(a.valid_at) AS valid_at,
       toString(a.learned_at) AS learned_at,
       a.time_basis AS time_basis,
       a.evidence_tier AS evidence_tier,
       a.perceiver_version AS perceiver_version,
       coalesce(a.binding_pending, false) AS binding_pending,
       coalesce(a.superseded, false) AS superseded
ORDER BY valid_at, learned_at, id
"""

_LIVE_STATE_VIEWS_QUERY = """
MATCH (v:Entity {group_id: $namespace, view_kind: 'scalar_state'})
RETURN v.uuid AS id,
       v.view_key AS view_key,
       v.view_subject_uuid AS subject_uuid,
       v.view_subject AS subject,
       v.ss_attribute AS attribute,
       v.ss_scope AS scope,
       v.ss_kind AS value_kind,
       v.ss_unit AS unit,
       v.ss_value AS value,
       v.ss_display AS display,
       v.summary AS summary,
       toString(v.valid_at) AS valid_at,
       toString(v.created_at) AS created_at,
       coalesce(v.view_current, true) AS current,
       v.scalar_effective_tier AS effective_tier,
       coalesce(v.scalar_contributors, []) AS contributor_ids,
       v.supersedes AS supersedes,
       v.superseded_by AS superseded_by
ORDER BY valid_at, created_at, id
"""

_LIVE_HISTORY_VIEWS_QUERY = """
MATCH (v:Entity {group_id: $namespace, view_kind: 'scalar_history'})
OPTIONAL MATCH (v)-[:HISTORY_ENTRY]->(history_assertion:TypedAssertion)
WITH v, collect(DISTINCT history_assertion.assertion_id) AS contributor_ids
RETURN v.uuid AS id,
       v.view_key AS view_key,
       v.view_subject_uuid AS subject_uuid,
       v.view_subject AS subject,
       v.ss_attribute AS attribute,
       v.ss_scope AS scope,
       v.ss_kind AS value_kind,
       v.ss_unit AS unit,
       v.sh_entry_count AS entry_count,
       v.sh_signature AS signature,
       v.sh_op_counts AS op_counts,
       v.sh_first_valid_at AS first_valid_at,
       v.sh_last_valid_at AS last_valid_at,
       v.view_payload AS payload,
       contributor_ids,
       toString(v.valid_at) AS valid_at,
       toString(v.created_at) AS created_at,
       coalesce(v.view_current, true) AS current
ORDER BY valid_at, created_at, id
"""

_LIVE_EVENT_ASSERTIONS_QUERY = """
MATCH (a:TypedEventAssertion {namespace: $namespace})
RETURN a.assertion_id AS id,
       a.assertion_key AS assertion_key,
       a.source_key AS source_key,
       a.episode_uuid AS evidence_id,
       a.turn_evidence_uuid AS turn_evidence_id,
       a.subject_uuid AS subject_uuid,
       a.subject_display AS subject,
       a.predicate AS predicate,
       a.domain AS domain,
       a.object_key AS object_key,
       a.object_display AS object_display,
       a.stated_span AS stated_span,
       toString(a.valid_at) AS valid_at,
       toString(a.learned_at) AS learned_at,
       a.time_basis AS time_basis,
       a.evidence_tier AS evidence_tier,
       coalesce(a.binding_pending, false) AS binding_pending,
       coalesce(a.superseded, false) AS superseded
ORDER BY valid_at, learned_at, assertion_key
"""

_LIVE_EVENT_VIEWS_QUERY = """
MATCH (v:Entity {group_id: $namespace, view_kind: 'timeline'})
WHERE v.view_predicate IS NOT NULL AND trim(coalesce(v.view_predicate, '')) <> ''
OPTIONAL MATCH (v)-[:EVENT_HISTORY_ENTRY]->(event_assertion:TypedEventAssertion)
WITH v, collect(DISTINCT event_assertion.assertion_key) AS contributor_ids
RETURN v.uuid AS id,
       v.view_key AS view_key,
       v.view_subject_uuid AS subject_uuid,
       v.view_subject AS subject,
       v.view_predicate AS predicate,
       v.view_domain AS domain,
       v.view_value AS entry_count,
       v.view_payload AS payload,
       contributor_ids,
       toString(v.valid_at) AS valid_at,
       toString(v.created_at) AS created_at,
       coalesce(v.view_current, true) AS current
ORDER BY predicate, domain, valid_at, created_at, id
"""

_LIVE_FACTS_QUERY = """
MATCH (s:Entity {group_id: $namespace})-[r]->(o:Entity {group_id: $namespace})
WHERE r.fact IS NOT NULL
RETURN s.name AS subject,
       type(r) AS relation,
       o.name AS object,
       r.fact AS fact,
       coalesce(r.episodes, []) AS episode_ids,
       toString(r.valid_at) AS valid_at,
       toString(r.created_at) AS learned_at
ORDER BY coalesce(toString(r.valid_at), toString(r.created_at)), fact
LIMIT 40
"""
