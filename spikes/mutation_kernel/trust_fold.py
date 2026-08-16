"""Trust-aware investigative folding for the mutation-kernel research spike.

Experiment 14 proved that one admitted assertion can resolve to different effective authority for
different purposes. This module pushes that result into actual belief formation.

The generic trust engine still owns the hard admission ceiling. The extension fold owns domain
semantics: which value is being claimed, how purpose-specific trust selects a winner, and how to
classify lower-trust evidence as redundant support vs counterevidence.

A normal kernel ``View`` remains the derived belief. ``TrustFoldTrace`` is an immutable audit sidecar
that records every participating assertion, its purpose-specific trust resolution, and whether it was
included, excluded, or retained as counterevidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any, Mapping, Sequence

from .investigation import OWNERSHIP_ASSERTION
from .kernel import Abstention, Assertion, View, authority_rank, current_assertions
from .trust import (
    PurposeTrustEngine,
    PurposeTrustResolver,
    TrustProfile,
    TrustResolution,
    _hash_parts,
    _stable_json,
)


INVESTIGATION_CONCLUSION_VIEW = "investigation.ownership_conclusion_view"


@dataclass(frozen=True)
class FoldParticipation:
    assertion_id: str
    profile_id: str
    claimed_value: str
    role: str
    effective_authority: str
    requested_authority: str
    trust_reason: str

    def __post_init__(self) -> None:
        if self.role not in {"included", "excluded", "counterevidence", "contested"}:
            raise ValueError("FoldParticipation.role is invalid")
        for name in (
            "assertion_id",
            "profile_id",
            "claimed_value",
            "effective_authority",
            "requested_authority",
            "trust_reason",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"FoldParticipation.{name} must be non-empty")


@dataclass(frozen=True)
class TrustFoldTrace:
    trace_id: str
    view_type: str
    subject_id: str
    scope: str
    key: str
    purpose: str
    resolver_id: str
    resolver_version: int
    status: str
    winning_value: str | None
    included_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    counterevidence_ids: tuple[str, ...]
    participations: tuple[FoldParticipation, ...]

    def __post_init__(self) -> None:
        if self.status not in {"view", "abstention"}:
            raise ValueError("TrustFoldTrace.status must be view or abstention")
        for name in ("trace_id", "view_type", "subject_id", "scope", "key", "purpose", "resolver_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"TrustFoldTrace.{name} must be non-empty")
        if self.resolver_version < 1:
            raise ValueError("TrustFoldTrace.resolver_version must be >= 1")
        for ids in (self.included_ids, self.excluded_ids, self.counterevidence_ids):
            if ids != tuple(sorted(set(ids))):
                raise ValueError("TrustFoldTrace ID sets must be canonical")
        all_role_ids = {
            participant.assertion_id for participant in self.participations
        }
        if all_role_ids != set(self.included_ids) | set(self.excluded_ids) | set(self.counterevidence_ids):
            # Contested abstentions intentionally put the top conflict set into counterevidence_ids;
            # every current assertion must still be represented exactly once in the trace.
            raise ValueError("TrustFoldTrace roles do not cover the canonical ID sets")
        if len(all_role_ids) != len(self.participations):
            raise ValueError("TrustFoldTrace cannot contain duplicate assertion participations")
        if self.status == "view" and not self.winning_value:
            raise ValueError("view trace requires a winning value")
        if self.status == "abstention" and self.winning_value is not None:
            raise ValueError("abstention trace cannot claim a winning value")


@dataclass(frozen=True)
class TrustFoldResult:
    outcome: View | Abstention
    trace: TrustFoldTrace


class InvestigationConclusionTrustResolver:
    """Purpose resolver that uses extension-owned evidentiary role facets.

    This is intentionally separate from Experiment 14's reference resolver so that the earlier
    experiment stays frozen. The role facet is extension semantics; generic trust infrastructure
    only preserves and clamps it.
    """

    resolver_id = "investigation.conclusion_trust"
    version = 1

    def resolve(self, profile: TrustProfile, purpose: str):
        from .trust import TrustResolutionProposal

        role = profile.facet("investigation.evidentiary_role", "unspecified")
        source_kinds = set(profile.source_kinds)
        used = ("core.source_kinds", "investigation.evidentiary_role")

        if purpose == "recorded_title":
            if role == "title_record" and source_kinds & {"deed", "official_filing", "court_record"}:
                return TrustResolutionProposal(
                    authority="trusted_tool",
                    reason="direct_title_record",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="indirect_for_recorded_title",
                used_facets=used,
            )

        if purpose == "beneficial_owner":
            if role == "beneficial_owner_synthesis" and "investigator_note" in source_kinds:
                return TrustResolutionProposal(
                    authority="manual",
                    reason="operator_synthesis_directly_addresses_beneficial_owner",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="indirect_for_beneficial_owner",
                used_facets=used,
            )

        return TrustResolutionProposal(
            authority="agent",
            reason="unknown_investigation_conclusion_purpose",
            used_facets=used,
        )


def _claimed_owner(assertion: Assertion) -> str:
    if assertion.assertion_type != OWNERSHIP_ASSERTION:
        raise ValueError("trust-aware ownership fold received a non-ownership assertion")
    if not isinstance(assertion.value, dict):
        raise ValueError("ownership assertion value must be a mapping")
    owner = str(assertion.value.get("owner_entity_id") or "").strip()
    if not owner:
        raise ValueError("ownership assertion is missing owner_entity_id")
    return owner


def _trace_id(payload: dict[str, Any]) -> str:
    return _hash_parts("trust-fold-trace", _stable_json(payload))


class TrustAwareOwnershipFold:
    """Highest-purpose-trust wins; lower-trust evidence remains auditable.

    For a unique winning value at the highest effective authority rank:
    * same-value assertions at that top rank are included contributors;
    * same-value lower-rank assertions are excluded as redundant lower-trust support;
    * every conflicting lower-rank assertion remains counterevidence.

    If more than one distinct value reaches the same highest rank, the fold abstains rather than
    breaking the tie by count or recency.
    """

    def __init__(
        self,
        *,
        purpose: str,
        resolver: PurposeTrustResolver | None = None,
        engine: PurposeTrustEngine | None = None,
    ) -> None:
        if not purpose.strip():
            raise ValueError("TrustAwareOwnershipFold.purpose must be non-empty")
        self.purpose = purpose
        self.resolver = resolver or InvestigationConclusionTrustResolver()
        self.engine = engine or PurposeTrustEngine()

    def fold(
        self,
        assertions: Sequence[Assertion],
        *,
        profiles: Mapping[str, TrustProfile],
    ) -> TrustFoldResult:
        rows = current_assertions(assertions)
        if not rows:
            raise ValueError("trust-aware ownership fold requires at least one current assertion")

        subject_id = rows[0].subject_id
        scope = rows[0].scope
        key = rows[0].key
        for row in rows:
            if row.assertion_type != OWNERSHIP_ASSERTION:
                raise ValueError("trust-aware ownership fold received a non-ownership assertion")
            if row.subject_id != subject_id or row.scope != scope or row.key != key:
                raise ValueError("trust-aware ownership fold received more than one assertion slot")
            profile = profiles.get(row.assertion_id)
            if profile is None:
                raise ValueError(f"missing trust profile for assertion {row.assertion_id}")
            if profile.assertion_id != row.assertion_id or profile.source_key != row.source_key:
                raise ValueError("trust profile does not match assertion identity/source")

        resolved: list[tuple[Assertion, str, TrustResolution]] = []
        for row in rows:
            profile = profiles[row.assertion_id]
            resolution = self.engine.resolve(
                profile,
                purpose=self.purpose,
                resolver=self.resolver,
            )
            resolved.append((row, _claimed_owner(row), resolution))

        top_rank = max(authority_rank(resolution.effective_authority) for _, _, resolution in resolved)
        values_at_top = {
            claimed
            for _row, claimed, resolution in resolved
            if authority_rank(resolution.effective_authority) == top_rank
        }
        dimensions = (("purpose", self.purpose),)

        if len(values_at_top) != 1:
            contested_ids = tuple(
                sorted(
                    row.assertion_id
                    for row, _claimed, resolution in resolved
                    if authority_rank(resolution.effective_authority) == top_rank
                )
            )
            excluded_ids = tuple(
                sorted(
                    row.assertion_id
                    for row, _claimed, resolution in resolved
                    if authority_rank(resolution.effective_authority) < top_rank
                )
            )
            participations = tuple(
                sorted(
                    (
                        FoldParticipation(
                            assertion_id=row.assertion_id,
                            profile_id=resolution.profile_id,
                            claimed_value=claimed,
                            role=(
                                "contested"
                                if authority_rank(resolution.effective_authority) == top_rank
                                else "excluded"
                            ),
                            effective_authority=resolution.effective_authority,
                            requested_authority=resolution.requested_authority,
                            trust_reason=resolution.reason,
                        )
                        for row, claimed, resolution in resolved
                    ),
                    key=lambda item: item.assertion_id,
                )
            )
            # Store the top conflict set in counterevidence_ids so the standard Abstention plus trace
            # still exposes the exact competing evidence while lower-trust rows remain excluded.
            payload = {
                "view_type": INVESTIGATION_CONCLUSION_VIEW,
                "subject_id": subject_id,
                "scope": scope,
                "key": key,
                "purpose": self.purpose,
                "resolver_id": self.resolver.resolver_id,
                "resolver_version": int(self.resolver.version),
                "status": "abstention",
                "winning_value": None,
                "included_ids": [],
                "excluded_ids": list(excluded_ids),
                "counterevidence_ids": list(contested_ids),
                "participations": [participant.__dict__ for participant in participations],
            }
            trace = TrustFoldTrace(
                trace_id=_trace_id(payload),
                view_type=INVESTIGATION_CONCLUSION_VIEW,
                subject_id=subject_id,
                scope=scope,
                key=key,
                purpose=self.purpose,
                resolver_id=self.resolver.resolver_id,
                resolver_version=int(self.resolver.version),
                status="abstention",
                winning_value=None,
                included_ids=(),
                excluded_ids=excluded_ids,
                counterevidence_ids=contested_ids,
                participations=participations,
            )
            return TrustFoldResult(
                outcome=Abstention(
                    view_type=INVESTIGATION_CONCLUSION_VIEW,
                    subject_id=subject_id,
                    scope=scope,
                    key=key,
                    reason="contested_top_trust",
                    assertion_ids=contested_ids,
                    dimensions=dimensions,
                ),
                trace=trace,
            )

        winning_value = next(iter(values_at_top))
        included: list[tuple[Assertion, TrustResolution]] = []
        excluded: list[tuple[Assertion, TrustResolution]] = []
        counter: list[tuple[Assertion, TrustResolution]] = []
        participations: list[FoldParticipation] = []

        for row, claimed, resolution in resolved:
            rank = authority_rank(resolution.effective_authority)
            if claimed == winning_value and rank == top_rank:
                role = "included"
                included.append((row, resolution))
            elif claimed == winning_value:
                role = "excluded"
                excluded.append((row, resolution))
            else:
                role = "counterevidence"
                counter.append((row, resolution))
            participations.append(
                FoldParticipation(
                    assertion_id=row.assertion_id,
                    profile_id=resolution.profile_id,
                    claimed_value=claimed,
                    role=role,
                    effective_authority=resolution.effective_authority,
                    requested_authority=resolution.requested_authority,
                    trust_reason=resolution.reason,
                )
            )

        included_ids = tuple(sorted(row.assertion_id for row, _ in included))
        excluded_ids = tuple(sorted(row.assertion_id for row, _ in excluded))
        counter_ids = tuple(sorted(row.assertion_id for row, _ in counter))
        included_authority = min(
            (resolution.effective_authority for _row, resolution in included),
            key=authority_rank,
        )
        confidence = min(row.confidence for row, _resolution in included)
        participations_tuple = tuple(sorted(participations, key=lambda item: item.assertion_id))

        payload = {
            "view_type": INVESTIGATION_CONCLUSION_VIEW,
            "subject_id": subject_id,
            "scope": scope,
            "key": key,
            "purpose": self.purpose,
            "resolver_id": self.resolver.resolver_id,
            "resolver_version": int(self.resolver.version),
            "status": "view",
            "winning_value": winning_value,
            "included_ids": list(included_ids),
            "excluded_ids": list(excluded_ids),
            "counterevidence_ids": list(counter_ids),
            "participations": [participant.__dict__ for participant in participations_tuple],
        }
        trace = TrustFoldTrace(
            trace_id=_trace_id(payload),
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            subject_id=subject_id,
            scope=scope,
            key=key,
            purpose=self.purpose,
            resolver_id=self.resolver.resolver_id,
            resolver_version=int(self.resolver.version),
            status="view",
            winning_value=winning_value,
            included_ids=included_ids,
            excluded_ids=excluded_ids,
            counterevidence_ids=counter_ids,
            participations=participations_tuple,
        )
        return TrustFoldResult(
            outcome=View(
                view_type=INVESTIGATION_CONCLUSION_VIEW,
                subject_id=subject_id,
                scope=scope,
                key=key,
                value={"owner_entity_id": winning_value, "purpose": self.purpose},
                confidence=confidence,
                contributor_ids=included_ids,
                counterevidence_ids=counter_ids,
                effective_authority=included_authority,
                rationale=(
                    f"purpose={self.purpose}",
                    f"resolver={self.resolver.resolver_id}@{self.resolver.version}",
                    f"excluded_lower_trust_support={len(excluded_ids)}",
                    f"counterevidence={len(counter_ids)}",
                    f"trace_id={trace.trace_id}",
                ),
                dimensions=dimensions,
            ),
            trace=trace,
        )


class Neo4jTrustFoldTraceStore:
    """Immutable storage for trust-aware fold participation traces."""

    def __init__(self, neo4j: Any, *, namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("Neo4jTrustFoldTraceStore.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_trust_fold_trace_storage_key IF NOT EXISTS "
            "FOR (t:MutationTrustFoldTrace) REQUIRE t.storage_key IS UNIQUE"
        )

    @staticmethod
    def _payload(trace: TrustFoldTrace) -> dict[str, Any]:
        return {
            "trace_id": trace.trace_id,
            "view_type": trace.view_type,
            "subject_id": trace.subject_id,
            "scope": trace.scope,
            "key": trace.key,
            "purpose": trace.purpose,
            "resolver_id": trace.resolver_id,
            "resolver_version": trace.resolver_version,
            "status": trace.status,
            "winning_value": trace.winning_value,
            "included_ids": list(trace.included_ids),
            "excluded_ids": list(trace.excluded_ids),
            "counterevidence_ids": list(trace.counterevidence_ids),
            "participations": [participant.__dict__ for participant in trace.participations],
        }

    def record(self, trace: TrustFoldTrace) -> dict[str, object]:
        payload = self._payload(trace)
        payload_hash = _hash_parts(_stable_json(payload))
        storage_key = _hash_parts(self.namespace, "trust-fold-trace", trace.trace_id)
        write_token = uuid.uuid4().hex
        rows = self._neo4j.execute(
            """
            MERGE (trace:MutationTrustFoldTrace {storage_key:$storage_key})
              ON CREATE SET trace.namespace=$namespace,
                            trace.trace_id=$trace_id,
                            trace.view_type=$view_type,
                            trace.subject_id=$subject_id,
                            trace.scope=$scope,
                            trace.key=$key,
                            trace.purpose=$purpose,
                            trace.resolver_id=$resolver_id,
                            trace.resolver_version=$resolver_version,
                            trace.status=$status,
                            trace.payload_json=$payload_json,
                            trace.payload_hash=$payload_hash,
                            trace.created_token=$write_token,
                            trace.created_at=datetime()
            RETURN trace.payload_hash AS payload_hash,
                   trace.created_token=$write_token AS created
            """,
            {
                "storage_key": storage_key,
                "namespace": self.namespace,
                "trace_id": trace.trace_id,
                "view_type": trace.view_type,
                "subject_id": trace.subject_id,
                "scope": trace.scope,
                "key": trace.key,
                "purpose": trace.purpose,
                "resolver_id": trace.resolver_id,
                "resolver_version": int(trace.resolver_version),
                "status": trace.status,
                "payload_json": _stable_json(payload),
                "payload_hash": payload_hash,
                "write_token": write_token,
            },
        )
        if not rows:
            raise RuntimeError("trust fold trace write returned no result")
        if str(rows[0].get("payload_hash") or "") != payload_hash:
            raise ValueError("trust fold trace identity collision: persisted trace differs")
        return {"storage_key": storage_key, "created": bool(rows[0].get("created"))}

    def load(self, trace_id: str) -> TrustFoldTrace | None:
        rows = self._neo4j.execute(
            """
            MATCH (trace:MutationTrustFoldTrace {namespace:$namespace, trace_id:$trace_id})
            RETURN trace.payload_json AS payload_json
            """,
            {"namespace": self.namespace, "trace_id": trace_id},
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("trust fold trace ID resolved to more than one row")
        payload = json.loads(str(rows[0]["payload_json"]))
        return TrustFoldTrace(
            trace_id=str(payload["trace_id"]),
            view_type=str(payload["view_type"]),
            subject_id=str(payload["subject_id"]),
            scope=str(payload["scope"]),
            key=str(payload["key"]),
            purpose=str(payload["purpose"]),
            resolver_id=str(payload["resolver_id"]),
            resolver_version=int(payload["resolver_version"]),
            status=str(payload["status"]),
            winning_value=(None if payload.get("winning_value") is None else str(payload["winning_value"])),
            included_ids=tuple(str(value) for value in payload.get("included_ids", [])),
            excluded_ids=tuple(str(value) for value in payload.get("excluded_ids", [])),
            counterevidence_ids=tuple(str(value) for value in payload.get("counterevidence_ids", [])),
            participations=tuple(
                FoldParticipation(
                    assertion_id=str(row["assertion_id"]),
                    profile_id=str(row["profile_id"]),
                    claimed_value=str(row["claimed_value"]),
                    role=str(row["role"]),
                    effective_authority=str(row["effective_authority"]),
                    requested_authority=str(row["requested_authority"]),
                    trust_reason=str(row["trust_reason"]),
                )
                for row in payload.get("participations", [])
            ),
        )

    def count(self) -> int:
        rows = self._neo4j.execute(
            "MATCH (t:MutationTrustFoldTrace {namespace:$namespace}) RETURN count(t) AS n",
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0
