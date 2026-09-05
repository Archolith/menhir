"""Typed-assertion writes, contributor queries, and suppression provenance."""

from __future__ import annotations

import json
from typing import Any, Callable

from menhir.domain.typed_assertion import IDENTITY_VERSION, TypedAssertion, normalize_scalar
from menhir.domain.self_authority import (
    UnconfirmedSelfAssertionError,
    is_canonical_self_subject,
)
from menhir.infrastructure.schema import get_scalar_state_activation_queries

from menhir.infrastructure.typed_assertion_models import (
    ScalarStateActivationError,
    _RECORD_CYPHER,
)
from menhir.infrastructure.self_binding import SelfBindMode, resolve_bind_mode

class TypedAssertionWriteMixin:
    """Write and query durable `:TypedAssertion` nodes. Direct Neo4j CRUD, no View writes."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j
        self._canonical_self_binding_mode = SelfBindMode.OFF

    def configure_canonical_self_binding_mode(self, value: Any) -> None:
        self._canonical_self_binding_mode = resolve_bind_mode(value)

    # ---- write -------------------------------------------------------------------------------

    def record_assertion(self, assertion: TypedAssertion) -> dict[str, Any]:
        """Persist one assertion in ONE atomic statement (head lock + supersede + provenance).
        Returns {assertion_id, created, superseded_prior, binding_pending}.

        The head is the ATOMIC lock, MERGEd on the BINDING-STABLE ``source_key`` (episode + span +
        ordinal), which has a DB uniqueness constraint. So two concurrent first writes for the same
        source claim — even bound through different subject_uuids after a merge — converge on ONE
        head and ONE current assertion; there is no read-before-write preflight to race. Both
        ``source_key`` (head identity) and ``assertion_key`` (exact interpreted identity) omit
        subject_uuid, so re-perception after a rebind is idempotent and lands on the same head."""
        protects_self = self._canonical_self_binding_mode is SelfBindMode.ENFORCE
        if protects_self and is_canonical_self_subject(
            assertion.subject_uuid, assertion.namespace
        ):
            # This low point has no pinned-key verifier and metadata is caller-controlled. Reject
            # even an "owner_confirmed" flag: scalar model output remains a durable advisory until
            # an exact confirmation object is verified by a future trusted promotion seam.
            raise UnconfirmedSelfAssertionError(
                "typed scalar assertion cannot attach to canonical self without exact "
                "owner confirmation"
            )
        params = self._to_params(assertion)
        params["allow_canonical_self"] = not protects_self
        rows = self._neo4j.execute(_RECORD_CYPHER, params=params)
        row = rows[0] if rows else {}
        return {
            "assertion_id": str(row.get("assertion_id") or ""),
            "created": bool(row.get("created")),
            "superseded_prior": bool(row.get("superseded_prior")),
            "binding_pending": bool(row.get("binding_pending")),
            # C.4.4: the node was already bound to a DIFFERENT real entity than this write presented;
            # the write refused to re-bind or add a second owner. The caller must not treat it as bound.
            "binding_mismatch": bool(row.get("binding_mismatch")),
        }

    def backfill_assertion_embeddings(
        self, embed: Callable[[str], "list[float] | None"], *, embed_version: str,
        batch: int = 200, namespace: str | None = None,
    ) -> dict[str, int]:
        """Embed each `:TypedAssertion`'s `stated_span` into a `name_embedding` for observation recall
        (Phase 4a.1). This is the RESUMABLE, VERSIONED backfill of existing history -- "embed at write
        time" (a later slice) covers only NEW assertions.

        RESUMABLE BY CONSTRUCTION: only rows MISSING a current-version embedding are selected, and an
        embedded row drops out of the set, so repeated BOUNDED calls make monotone progress with no
        separate cursor to advance (unlike the scalar consolidation watermark, the "done" predicate IS
        the resume point). VERSIONED: `embed_version` stamps the model that produced the vector; bumping
        it (a model change) re-selects everything for re-embedding -- the caller MUST pass the version
        matching the `embed` callable, or the graph ends up with mixed-dimension vectors. BOUNDED by
        `batch` (a full batch means more may remain). `embed` is INJECTED and returns None on a provider
        error, so a failed embed leaves that row unembedded for a later retry -- never a partial/corrupt
        write. `stated_span` is guaranteed non-null on committed assertions (Phase-0 F6), so this never
        hits an unembeddable row. RECALL-NEUTRAL: it writes a property nothing reads until the
        observation lane (4a.2) indexes it. Returns {embedded, remaining} (remaining after this batch)."""
        ns_pred = " AND a.namespace = $ns" if namespace is not None else ""
        select_params: dict[str, Any] = {"version": str(embed_version)}
        if namespace is not None:
            select_params["ns"] = namespace
        candidates = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion)
            WHERE a.stated_span IS NOT NULL
              AND (a.name_embedding IS NULL OR coalesce(a.embed_version, '') <> $version){ns_pred}
            RETURN a.assertion_id AS id, a.stated_span AS text
            ORDER BY a.assertion_id
            LIMIT $batch
            """,
            params={**select_params, "batch": int(batch)},
        )
        embedded: list[dict[str, Any]] = []
        for c in candidates:
            vec = embed(str(c.get("text") or ""))
            if vec:
                embedded.append({"id": str(c.get("id")), "emb": list(vec)})
        if embedded:
            self._neo4j.execute(
                """
                UNWIND $rows AS row
                MATCH (a:TypedAssertion {assertion_id: row.id})
                SET a.name_embedding = row.emb, a.embed_version = $version
                """,
                params={"rows": embedded, "version": str(embed_version)},
            )
        remaining_rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion)
            WHERE a.stated_span IS NOT NULL
              AND (a.name_embedding IS NULL OR coalesce(a.embed_version, '') <> $version){ns_pred}
            RETURN count(a) AS remaining
            """,
            params=select_params,
        )
        remaining = int((remaining_rows[0].get("remaining") if remaining_rows else 0) or 0)
        return {"embedded": len(embedded), "remaining": remaining}

    # ---- read --------------------------------------------------------------------------------

    def assertions_for_entity(
        self, subject_uuid: str, *, include_superseded: bool = False,
        materializable_only: bool = False, namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Assertions bound to an entity, oldest-valid first. By default only the CURRENT version
        per claim (`superseded=false`); `include_superseded=True` returns every version.
        `materializable_only=True` additionally drops `binding_pending` rows — assertions whose
        entity/episode provenance was not bound must NOT feed a UUID-keyed View. `namespace` (C.4.4)
        scopes the read to ONE silo so a fold never mixes two tenants' assertions that happen to share
        a subject_uuid; None = all namespaces (unchanged behavior for existing callers)."""
        preds: list[str] = []
        params: dict[str, Any] = {"u": subject_uuid}
        if not include_superseded:
            preds.append("NOT coalesce(a.superseded, false)")
        if materializable_only:
            preds.append("NOT coalesce(a.binding_pending, false)")
        if namespace is not None:
            preds.append("a.namespace = $namespace")
            params["namespace"] = namespace
        where = ("WHERE " + " AND ".join(preds)) if preds else ""
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion {{subject_uuid: $u}})
            {where}
            OPTIONAL MATCH (te:TurnEvidence {{turn_id: a.episode_uuid}})
            OPTIONAL MATCH (source_ep:Episodic)-[:ADMITTED_ON]->(te)
            WITH a, te, collect(DISTINCT source_ep.uuid) AS admitted_episode_uuids
            RETURN a.assertion_id AS assertion_id, a.claim_key AS claim_key,
                   a.subject_uuid AS subject_uuid, a.subject_display AS subject_display,
                   a.attribute AS attribute, a.scope AS scope, a.value_kind AS value_kind,
                   a.unit AS unit, a.operation AS operation, a.value AS value,
                   a.value_json AS value_json, a.stated_span AS stated_span,
                   CASE WHEN te IS NULL THEN coalesce(a.episode_uuid, '')
                        ELSE coalesce(admitted_episode_uuids[0], '') END AS episode_uuid,
                   CASE WHEN te IS NULL THEN coalesce(a.episode_uuid, '')
                        ELSE coalesce(admitted_episode_uuids[0], '') END AS source_episode_uuid,
                   CASE WHEN te IS NULL THEN '' ELSE coalesce(te.turn_id, '') END AS turn_id,
                   a.episode_uuid AS grounding_id, toString(a.valid_at) AS valid_at,
                   toString(a.learned_at) AS learned_at, a.time_basis AS time_basis,
                   a.evidence_tier AS evidence_tier, a.perceiver_version AS perceiver_version,
                   coalesce(a.absolute_semantics, 'ordinary') AS absolute_semantics,
                   coalesce(a.superseded, false) AS superseded,
                   coalesce(a.binding_pending, false) AS binding_pending
            ORDER BY a.valid_at ASC, a.learned_at ASC
            """,
            params=params,
        )
        return [self._hydrate(dict(r)) for r in rows]

    def materializable_assertions_for_entity(
        self, subject_uuid: str, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Current, fully-bound assertions for an entity — the ONLY rows that may materialize a
        ScalarStateView (excludes binding_pending). `namespace` scopes the fold input to one silo
        (C.4.4); None = all. Explicit alias for the fold input."""
        return self.assertions_for_entity(
            subject_uuid, materializable_only=True, namespace=namespace)

    def pending_projection_repairs(
        self,
        *,
        operation_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return durable delete->View repair work in stable FIFO order.

        These receipts are created in the same Neo4j transaction that removes the assertions.  They
        therefore survive the assertions they describe and make a delete/rebuild crash recoverable.
        """
        preds = ["rr.status = 'pending'"]
        params: dict[str, Any] = {"limit": int(limit)}
        if operation_id is not None:
            preds.append("rr.operation_id = $operation_id")
            params["operation_id"] = operation_id
        if namespaces is not None:
            preds.append("rr.namespace IN $namespaces")
            params["namespaces"] = list(dict.fromkeys(namespaces))
        rows = self._neo4j.execute(
            f"""
            MATCH (rr:ScalarProjectionRepair)
            WHERE {' AND '.join(preds)}
            RETURN rr.repair_key AS repair_key, rr.operation_id AS operation_id,
                   rr.operation_kind AS operation_kind, rr.subject_uuid AS subject_uuid,
                   rr.namespace AS namespace, toString(rr.started_at) AS started_at
            ORDER BY rr.started_at ASC, rr.repair_key ASC
            LIMIT $limit
            """,
            params=params,
        )
        return [dict(r) for r in rows]

    def claim_due_scalar_activations(
        self,
        *,
        as_of: str,
        namespaces: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Atomically turn due future assertions into durable projection-repair work (G19).

        The fallback ``valid_at > recorded_at`` discovers future rows written before the explicit
        activation_pending property existed. Claiming clears the bit and creates the receipt in one
        transaction, so a crash can only leave replayable work, never a lost wake-up.
        """
        ns_pred = " AND a.namespace IN $namespaces" if namespaces is not None else ""
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion)
            WHERE NOT coalesce(a.superseded, false)
              AND NOT coalesce(a.binding_pending, false)
              AND a.valid_at <= datetime($as_of)
              AND coalesce(a.activation_pending, a.valid_at > a.recorded_at){ns_pred}
            WITH a ORDER BY a.valid_at ASC, a.assertion_id ASC LIMIT $limit
            SET a.activation_pending = false, a.activation_claimed_at = datetime()
            MERGE (rr:ScalarProjectionRepair {{
                repair_key: 'TIME_ACTIVATION\u001f' + a.assertion_id
            }})
            ON CREATE SET rr.operation_id = a.assertion_id,
                          rr.operation_kind = 'TIME_ACTIVATION',
                          rr.namespace = a.namespace, rr.subject_uuid = a.subject_uuid,
                          rr.status = 'pending', rr.started_at = datetime(),
                          rr.due_at = a.valid_at
            RETURN rr.repair_key AS repair_key, rr.operation_id AS operation_id,
                   rr.operation_kind AS operation_kind, rr.subject_uuid AS subject_uuid,
                   rr.namespace AS namespace, toString(rr.started_at) AS started_at
            """,
            params={"as_of": as_of, "namespaces": namespaces, "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    def fetch_assertion_contributors(
        self, *, assertion_ids: list[str], relations: dict[str, str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Hydrate a bounded contributor list for synthetic expiry/as-of verdicts (7.J)."""
        ordered_ids = list(dict.fromkeys(str(a) for a in assertion_ids if a))
        total = len(ordered_ids)
        bounded = ordered_ids[:max(1, min(int(limit), 50))]
        if not bounded:
            return {"contributors": [], "total": 0, "next_offset": None}
        rows = self._neo4j.execute(
            """
            UNWIND range(0, size($ids) - 1) AS ordinal
            MATCH (a:TypedAssertion {assertion_id: $ids[ordinal]})
            RETURN a.assertion_id AS assertion_id, a.operation AS operation,
                   coalesce(a.value, a.value_json) AS value, a.stated_span AS stated_span,
                   toString(a.valid_at) AS valid_at, a.evidence_tier AS evidence_tier,
                   a.episode_uuid AS episode_uuid, ordinal
            ORDER BY ordinal
            """,
            params={"ids": bounded},
        )
        by_id = {str(r.get("assertion_id")): dict(r) for r in rows}
        hydrated: list[dict[str, Any]] = []
        for assertion_id in bounded:
            row = by_id.get(assertion_id)
            if row is None:
                continue
            row.pop("ordinal", None)
            row["relation"] = (relations or {}).get(assertion_id, "CONTRIBUTOR")
            hydrated.append(row)
        return {
            "contributors": hydrated,
            "total": total,
            "next_offset": len(bounded) if len(bounded) < total else None,
        }

    def mark_projection_repair_complete(self, repair_key: str) -> bool:
        """Complete one deletion repair only after its subject was successfully rebuilt."""
        rows = self._neo4j.execute(
            """
            MATCH (rr:ScalarProjectionRepair {repair_key: $repair_key})
            WHERE rr.status = 'pending'
            SET rr.status = 'complete', rr.completed_at = datetime()
            RETURN count(rr) AS completed
            """,
            params={"repair_key": repair_key},
        )
        return bool(rows and int(rows[0].get("completed", 0) or 0))

    def suppressible_provenance(
        self, *, namespace: str, candidate_uuids: list[str]
    ) -> list[dict[str, Any]]:
        """Deterministic provenance for Step-7 View suppression (read-only, no fuzzy matching).

        ``candidate_uuids`` are RECALL CANDIDATE uuids -- `:Entity` semantic-memory nodes recall
        surfaced -- NOT episode uuids. Those two uuid spaces are DISJOINT, so the candidate is joined
        to the typed-assertion log through the SHARED SOURCE EPISODE, menhir's universal provenance
        edge `(:Episodic)-[:MENTIONS]->(:Entity)`: the value memory `n` and the stale typed assertion
        `c` both derive from the same ingest, so `c.episode_uuid` equals the uuid of an episode that
        MENTIONS `n`.

        Provenance link is SLOT IDENTITY among grounded typed assertions, joined to the candidate via
        that shared episode -- NOT a `SUPERSEDES` edge, and NOT text/slot matching on the entity.
        Cross-episode value replacement in menhir does NOT mark the older assertion superseded or draw
        a SUPERSEDES edge (that edge only forms on same-source-claim re-perception); the older value
        survives as a distinct `superseded=false` absolute assertion for the same slot that the fold
        did not select. So a candidate `n` is suppressible iff its source episode is the SOURCE
        (`episode_uuid`) of a grounded absolute assertion `c` whose exact slot 5-tuple
        (subject+attribute+scope+value_kind+unit) carries a current `scalar_state` View, where the
        authority value/valid_at/evidence come from the LATEST assertion `latest` of that slot (the
        fold's LWW selection). The gate re-checks everything (different value + strictly older +
        grounded tier + sole current View), so a candidate whose source episode backs the LATEST
        assertion is dropped as same-value. The returned ``candidate_uuid`` is the ENTITY uuid
        `n.uuid`, so the planner's suppression set stays in recall's candidate space. Empty candidate
        set -> no query."""
        if not candidate_uuids:
            return []
        rows = self._neo4j.execute(
            """
            MATCH (epi:Episodic)-[:MENTIONS]->(n:Entity)
            WHERE n.uuid IN $uuids
            MATCH (c:TypedAssertion)
            WHERE c.namespace = $ns AND NOT coalesce(c.binding_pending, false)
              AND c.operation = 'absolute' AND c.episode_uuid = epi.uuid
            CALL {
                WITH c
                MATCH (v:Entity {view_kind: 'scalar_state',
                                 view_subject_uuid: c.subject_uuid, group_id: $ns})
                WHERE v.ss_attribute = c.attribute
                  AND coalesce(v.ss_scope, '') = coalesce(c.scope, '')
                  AND v.ss_kind = c.value_kind
                  AND coalesce(v.ss_unit, '') = coalesce(c.unit, '')
                  AND coalesce(v.view_current, true)
                RETURN count(v) AS view_count
            }
            WITH n, c, view_count
            WHERE view_count >= 1
            CALL {
                WITH c
                MATCH (a:TypedAssertion)
                WHERE a.namespace = $ns AND NOT coalesce(a.binding_pending, false)
                  AND a.operation = 'absolute'
                  AND a.subject_uuid = c.subject_uuid AND a.attribute = c.attribute
                  AND coalesce(a.scope, '') = coalesce(c.scope, '')
                  AND a.value_kind = c.value_kind AND coalesce(a.unit, '') = coalesce(c.unit, '')
                RETURN a.value_json AS lv, toString(a.valid_at) AS lva,
                       coalesce(a.evidence_tier, '') AS lt
                ORDER BY a.valid_at DESC LIMIT 1
            }
            RETURN n.uuid AS candidate_uuid, c.subject_uuid AS subject_uuid,
                   c.attribute AS attribute, coalesce(c.scope, '') AS scope,
                   c.value_kind AS value_kind, coalesce(c.unit, '') AS unit,
                   lv AS view_value, lva AS view_valid_at, lt AS evidence_tier,
                   true AS view_current, (view_count = 1) AS is_sole_current,
                   c.value_json AS stale_value, toString(c.valid_at) AS stale_valid_at
            """,
            params={"ns": namespace, "uuids": list(candidate_uuids)},
        )
        return [dict(r) for r in rows]
