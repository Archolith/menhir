"""Canonical self-entity binding for typed-scalar perception (C.4.3) — offline.

First-person subjects ("user"/"I"/"me"/...) bind to the namespace's ONE stable self entity via the
injected `resolve_self_subject` seam — never via lexical name-match and never as a per-episode node.
Named third parties never reach the self path (SELF_TOKENS is an exact allowlist), so they can never
bind to self. Covers: the pure classifier, `_resolve_subject` precedence, both binders (perceive +
repair) under an explicitly injected seam, production-service abstention, and the repo MERGE's
determinism + per-namespace isolation. No live Neo4j — every seam is a fake.
"""

from __future__ import annotations

import uuid as _uuidlib

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.services.typed_scalar_perception import (
    PERCEPTION_EVIDENCE_TIER,
    SELF_SUBJECT_DISPLAY,
    SELF_TOKENS,
    TypedScalarDecision,
    TypedScalarPerceptionService,
    TypedScalarProposal,
    _is_self_reference,
    _resolve_subject,
    advisory_subject_uuid,
    bind_and_persist_typed_scalars,
    repair_pending_bindings,
)

_FIXED_NOW = "2026-07-10T12:00:00+00:00"


def _prop(**over) -> TypedScalarProposal:
    base = dict(
        subject_text="user", attribute="wake", scope="", value_kind="clock_time",
        unit="", operation="absolute", value="07:30", stated_span="wake at 07:30",
        episode_uuid="ep-1", span_start=0, span_end=13, when="2026-07-01T00:00:00+00:00",
    )
    base.update(over)
    return TypedScalarProposal(**base)


def _decision(p: TypedScalarProposal, *, committed=True) -> TypedScalarDecision:
    return TypedScalarDecision(
        source_key=p.source_key, committed=committed, reason="unanimous", veto="commit",
        agreement=1.0, k=1, distribution={}, proposal=p,
    )


def _record_sink():
    """record_assertion seam mirroring the store's binding rule: a `unbound:` sentinel is pending,
    any real (namespace-deterministic self, or extracted) uuid is bound."""
    recorded: list = []

    def record(a):
        recorded.append(a)
        pending = a.subject_uuid.startswith("unbound:")
        return {"assertion_id": f"a{len(recorded)}", "binding_pending": pending,
                "binding_mismatch": False, "created": True}

    record.recorded = recorded
    return record


def _rebuild_sink():
    def rebuild(u, ns=None):
        rebuild.calls.append(u)
        rebuild.namespaces.append(ns)
        return {"subject_uuid": u, "written": 1}

    rebuild.calls = []
    rebuild.namespaces = []
    return rebuild


def _self_seam(uuid_for_ns: dict[str, str] | str):
    """A resolve_self_subject seam: maps namespace -> (uuid, display). Pass a dict for per-namespace
    uuids or a single string used for any namespace. Records the namespaces it was asked for."""
    def seam(ns):
        seam.asked.append(ns)
        if ns is None:
            return None
        u = uuid_for_ns[ns] if isinstance(uuid_for_ns, dict) else uuid_for_ns
        return (u, SELF_SUBJECT_DISPLAY) if u else None

    seam.asked = []
    return seam


# --------------------------------------------------------------------------- pure classifier

@pytest.mark.unit
@pytest.mark.parametrize("tok", ["user", "User", "  USER  ", "I", "i", "me", "myself", "the user"])
def test_self_tokens_recognized(tok):
    assert _is_self_reference(tok) is True


@pytest.mark.unit
@pytest.mark.parametrize("tok", ["my car", "Alice", "alice's coins", "", "   ", "users", "my"])
def test_non_self_subjects_rejected(tok):
    # possessed objects ("my car"), named third parties, blanks, and near-misses are NOT self.
    assert _is_self_reference(tok) is False


@pytest.mark.unit
def test_self_tokens_membership_is_exact_lowercase():
    assert SELF_TOKENS == frozenset({"user", "the user", "i", "me", "myself"})


# --------------------------------------------------------------------------- _resolve_subject precedence

@pytest.mark.unit
def test_resolve_prefers_self_seam_over_name_match_for_first_person():
    seam = _self_seam("self-ns-uuid")
    # even with a lexically-matching "user" entity present, the self identity wins (stable, not lexical).
    uid, disp = _resolve_subject("user", [{"uuid": "ent-x", "name": "user"}], "ns", seam)
    assert uid == "self-ns-uuid" and disp == SELF_SUBJECT_DISPLAY
    assert seam.asked == ["ns"]


@pytest.mark.unit
def test_resolve_named_subject_never_consults_self_seam():
    seam = _self_seam("self-ns-uuid")
    uid, disp = _resolve_subject("Alice", [{"uuid": "ent-a", "name": "Alice"}], "ns", seam)
    assert uid == "ent-a" and disp == "Alice"
    assert seam.asked == []                                   # third party never reaches the self path


@pytest.mark.unit
def test_resolve_falls_through_when_no_seam():
    uid, disp = _resolve_subject("user", [{"uuid": "ent-u", "name": "user"}], "ns", None)
    assert uid == "ent-u" and disp == "user"                 # ordinary name-match, unchanged


@pytest.mark.unit
def test_resolve_falls_through_when_seam_returns_none():
    # seam declines (e.g. no namespace) -> ordinary binding; here no candidate -> unbound.
    seam = _self_seam({})
    uid, disp = _resolve_subject("user", [], None, seam)
    assert uid is None and disp is None


# --------------------------------------------------------------------------- bind_and_persist under seam

@pytest.mark.unit
def test_first_person_binds_to_self_with_no_linked_entity():
    # THE core fix: "user" with ZERO linked entities was advisory; with the self seam it binds + rebuilds.
    rec, reb = _record_sink(), _rebuild_sink()
    seam = _self_seam("self-uuid-1")
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [],            # nothing linked to the episode
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        namespace="ns", resolve_self_subject=seam,
    )
    assert out["bound"] == 1 and out["advisory"] == 0 and out["rebuilt"] == 1
    assert reb.calls == ["self-uuid-1"]
    a = rec.recorded[0]
    assert a.subject_uuid == "self-uuid-1" and a.subject_display == SELF_SUBJECT_DISPLAY


@pytest.mark.unit
def test_named_third_party_stays_advisory_never_binds_to_self():
    rec, reb = _record_sink(), _rebuild_sink()
    seam = _self_seam("self-uuid-1")
    d = _decision(_prop(subject_text="Alice"))
    out = bind_and_persist_typed_scalars(
        [d], linked_entities_for_episode=lambda e: [],       # Alice not linked -> advisory
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        namespace="ns", resolve_self_subject=seam,
    )
    assert out["bound"] == 0 and out["advisory"] == 1 and reb.calls == []
    assert rec.recorded[0].subject_uuid == advisory_subject_uuid(d.source_key)
    assert seam.asked == []                                   # self seam never consulted for a name


@pytest.mark.unit
def test_self_binding_is_idempotent_same_uuid_same_key():
    rec, reb = _record_sink(), _rebuild_sink()
    seam = _self_seam("self-uuid-1")
    for _ in range(2):
        bind_and_persist_typed_scalars(
            [_decision(_prop(subject_text="user"))],
            linked_entities_for_episode=lambda e: [], record_assertion=rec,
            rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
            namespace="ns", resolve_self_subject=seam)
    assert [a.subject_uuid for a in rec.recorded] == ["self-uuid-1", "self-uuid-1"]
    assert rec.recorded[0].assertion_key == rec.recorded[1].assertion_key


# --------------------------------------------------------------------------- repair under seam

@pytest.mark.unit
def test_repair_rebinds_existing_user_advisory_to_self():
    # the repairability constraint: an advisory written BEFORE the self entity existed re-binds to self
    # on the next repair pass (repair uses the same seam), with no migration.
    rec, reb = _record_sink(), _rebuild_sink()
    seam = _self_seam({"ns": "self-uuid-ns"})
    row = dict(
        assertion_id="a-sk-1", source_key="sk-1", subject_uuid="unbound:sk-1", subject_display="user",
        binding_pending=True, projection_pending=False, attribute="wake", scope="",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        stated_span="wake at 07:30", episode_uuid="ep-1", span_start=0, span_end=13,
        claim_ordinal=0, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", time_basis="explicit", perceiver_version="v1",
        namespace="ns",
    )
    out = repair_pending_bindings(
        [row], linked_entities_for_episode=lambda e: [],     # still nothing linked; self seam rescues it
        record_assertion=rec, rebuild_scalar_state=reb, resolve_self_subject=seam,
    )
    assert out["repaired"] == 1 and out["still_pending"] == 0 and reb.calls == ["self-uuid-ns"]
    assert seam.asked == ["ns"]                               # resolved against the ROW's namespace
    a = rec.recorded[0]
    assert a.subject_uuid == "self-uuid-ns" and a.value == "07:30"   # interpretation preserved


# --------------------------------------------------------------------------- service wiring

class _SelfFakeService:
    def __init__(self):
        self.rebuilt = []

    def rebuild_scalar_state(self, subject_uuid, *, namespace=None, as_of=None):
        self.rebuilt.append(subject_uuid)
        return {"subject_uuid": subject_uuid, "written": 1}


class _SelfFakeAdapter:
    """Fake that exposes self creation so production-service tests can prove it is never called."""

    def __init__(self):
        self.canonical_self_binding_mode = "enforce"
        self.calls: list[str] = []
        self.recorded: list = []
        self.ensured: list[str] = []
        self.pending_rows: list[dict] = []
        self.attempted: list[str] = []

    def activate_scalar_state(self):
        self.calls.append("activate")
        return {"queries_executed": 3}

    def scalar_state_schema_ready(self):
        return True

    def ensure_self_entity(self, namespace):
        self.calls.append("ensure_self")
        self.ensured.append(namespace)
        return f"self::{namespace}"

    def fetch_linked_entities_for_episode(self, episode_uuid):
        return []                                            # nothing linked -> only self can bind

    def record_typed_assertion(self, assertion):
        self.calls.append("record")
        self.recorded.append(assertion)
        pending = assertion.subject_uuid.startswith("unbound:")
        return {"assertion_id": f"a{len(self.recorded)}", "binding_pending": pending, "created": True}

    def pending_advisory_assertions(self, *, namespaces=None, limit=200):
        self.calls.append("pending")
        rows = self.pending_rows
        if namespaces is not None:
            rows = [row for row in rows if row["namespace"] in namespaces]
        return rows[:limit]

    def mark_binding_repair_attempted(self, assertion_ids, *, at):
        self.calls.append("mark_attempted")
        self.attempted.extend(assertion_ids)
        return len(assertion_ids)

    def mark_projection_complete(self, assertion_ids):
        self.calls.append("mark_projection_complete")
        return len(assertion_ids)


class _Ep:
    def __init__(self, uuid, content):
        self.uuid = uuid
        self.content = content


def _llm(rows):
    import json

    def complete(system, user):
        return json.dumps(rows)
    return complete


@pytest.mark.unit
@pytest.mark.parametrize("canonical_self", [False, True])
def test_service_perceive_keeps_model_self_proposal_advisory(canonical_self):
    adapter = _SelfFakeAdapter()
    svc = _SelfFakeService()
    coord = TypedScalarPerceptionService(adapter, svc)
    eps = [_Ep("ep-1", "I wake at 07:30 every day.")]
    row = dict(episode=0, subject="user", attribute="wake", scope="", value_kind="clock_time",
               unit="", operation="absolute", value="07:30", when="2026-07-01",
               stated_span="I wake at 07:30")
    out = coord.perceive_and_persist(
        eps, _llm([row]), k=1, threshold=1.0, namespace="ns-A",
        canonical_self=canonical_self,
    )
    assert out["bound"] == 0 and out["advisory"] == 1
    assert out["decisions"] == 1 and out["committed"] == 1
    assert adapter.ensured == []
    assert svc.rebuilt == []
    assertion = adapter.recorded[0]
    assert assertion.subject_uuid == advisory_subject_uuid(assertion.source_key)
    assert assertion.stated_span == "I wake at 07:30"


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["off", "observe"])
def test_service_non_enforcing_modes_preserve_legacy_self_binding(mode) -> None:
    adapter = _SelfFakeAdapter()
    adapter.canonical_self_binding_mode = mode
    svc = _SelfFakeService()
    coord = TypedScalarPerceptionService(adapter, svc)
    eps = [type("E", (), {"uuid": "ep-1", "content": "I wake at 07:30"})()]
    row = dict(
        episode=0, subject="user", attribute="wake", scope="", value_kind="clock_time",
        unit="", operation="absolute", value="07:30", when="2026-07-01",
        stated_span="I wake at 07:30",
    )

    out = coord.perceive_and_persist(
        eps, _llm([row]), k=1, threshold=1.0, namespace="ns-A"
    )

    assert out["bound"] == 1 and out["advisory"] == 0
    assert adapter.ensured == ["ns-A"]
    assert adapter.recorded[0].subject_uuid == "self::ns-A"


@pytest.mark.unit
def test_service_repair_keeps_self_advisory_without_owner_confirmation():
    adapter = _SelfFakeAdapter()
    adapter.pending_rows = [dict(
        assertion_id="a-sk-1", source_key="sk-1", subject_uuid="unbound:sk-1",
        subject_display="user", binding_pending=True, projection_pending=False,
        attribute="wake", scope="", value_kind="clock_time", unit="", operation="absolute",
        value="07:30", stated_span="I wake at 07:30", episode_uuid="ep-1", span_start=0,
        span_end=15, claim_ordinal=0, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", time_basis="explicit",
        perceiver_version="v1", namespace="ns-A",
    )]
    svc = _SelfFakeService()
    coord = TypedScalarPerceptionService(adapter, svc)

    out = coord.repair_pending_bindings(namespaces=["ns-A"])

    assert out["repaired"] == 0 and out["still_pending"] == 1
    assert adapter.ensured == []
    assert svc.rebuilt == []


# --------------------------------------------------------------------------- repo MERGE (determinism)

class _CaptureNeo4j:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        return []


@pytest.mark.unit
def test_ensure_self_entity_is_deterministic_and_namespace_scoped():
    repo = EpisodeRepository(neo4j=_CaptureNeo4j())
    a1 = repo.ensure_self_entity("ns-A")
    a2 = repo.ensure_self_entity("ns-A")
    b = repo.ensure_self_entity("ns-B")
    # deterministic per namespace (uuid5), stable across calls, distinct across namespaces
    assert a1 == a2
    assert a1 == str(_uuidlib.uuid5(_uuidlib.NAMESPACE_URL, "menhir-self:ns-A"))
    assert a1 != b


@pytest.mark.unit
def test_ensure_self_entity_merges_plain_entity_with_self_marker():
    neo = _CaptureNeo4j()
    repo = EpisodeRepository(neo4j=neo)
    repo.ensure_self_entity("ns-A")
    query, params = neo.calls[0]
    assert "MERGE (n:Entity {uuid: $self_uuid})" in query
    assert "n.is_self = true" in query
    # scoped to the namespace and NOT a view/quantstate node (so it is a valid binding candidate)
    assert params["namespace"] == "ns-A" and params["name"] == "user"
    assert "is_view" not in query and "view_kind" not in query


@pytest.mark.unit
def test_ensure_self_entity_rejects_empty_namespace():
    repo = EpisodeRepository(neo4j=_CaptureNeo4j())
    with pytest.raises(ValueError):
        repo.ensure_self_entity("")


# --------------------------------------------------------------------- syntactic subject variants
#
# The perceiver sometimes emits a subject spelled differently from the entity that represents it:
# a leading determiner ("my boots" where the entity is "boots"), or a snake_case SLOT key where a
# surface form belongs ("shift_schedule"). Exact-match binding abstained on those, so the assertion
# stayed advisory even when the right entity was linked to the episode. Measured on the LME
# multismoke corpus: 3 of 5 unbound assertions were this, not a real ambiguity.
#
# The rule being protected: normalize the QUERY, keep the MATCH exact and unique.

from menhir.services.typed_scalar_rules import _bind_subject, _subject_variants


def test_exact_match_still_wins_and_is_tried_first():
    """Behaviour-preserving: a subject that already bound must bind identically, via no variant."""
    ents = [{"uuid": "e-1", "name": "boots"}, {"uuid": "e-2", "name": "my boots"}]
    uid, disp = _bind_subject("my boots", ents)
    assert (uid, disp) == ("e-2", "my boots")


def test_leading_determiner_stripped_as_fallback():
    ents = [{"uuid": "e-1", "name": "boots"}, {"uuid": "e-2", "name": "Zara"}]
    uid, disp = _bind_subject("my boots", ents)
    assert (uid, disp) == ("e-1", "boots")


def test_snake_case_slot_key_maps_to_surface_form():
    ents = [{"uuid": "e-1", "name": "shift schedule"}]
    uid, disp = _bind_subject("shift_schedule", ents)
    assert (uid, disp) == ("e-1", "shift schedule")


def test_variant_that_is_ambiguous_still_abstains():
    """A fallback gets no license the primary lacks: 2 matches is 2 matches."""
    ents = [{"uuid": "e-1", "name": "boots"}, {"uuid": "e-2", "name": "boots"}]
    assert _bind_subject("my boots", ents) == (None, None)


def test_variant_never_reaches_self_by_lexical_match():
    """Self binds through the canonical-self seam ONLY. If a determiner strip could land on a 'user'
    entity, a per-episode node would become the self subject -- the exact failure the seam exists to
    prevent."""
    ents = [{"uuid": "e-user", "name": "user"}]
    assert _bind_subject("my user", ents) == (None, None)


def test_no_variant_no_match_still_abstains():
    ents = [{"uuid": "e-1", "name": "something else"}]
    assert _bind_subject("my boots", ents) == (None, None)


def test_variants_exclude_the_original_and_dedupe():
    assert "my boots" not in _subject_variants("my boots")
    assert _subject_variants("boots") == []
    v = _subject_variants("my shift_schedule")
    assert len(v) == len(set(v))


def test_determiner_strip_does_not_apply_mid_string():
    """Only a LEADING determiner is syntactic noise; 'army boots' must not become 'boots'."""
    ents = [{"uuid": "e-1", "name": "boots"}]
    assert _bind_subject("army boots", ents) == (None, None)
