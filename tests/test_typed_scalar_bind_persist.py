"""Typed-scalar binding + persistence + rebuild + activation ordering (ScalarStateView C.4.3) — offline.

No live Neo4j: the store, entity-binding lookup, and View rebuild are injected as fakes, so this
suite proves the DETERMINISTIC contract only — NOT uniqueness constraints, MERGE atomicity, or real
concurrency (those are the C.4.4 live-Neo4j gate). Covered here:
  * unique name binding -> fully-bound assertion + View rebuild;
  * zero OR multiple candidates -> binding_pending advisory, NO rebuild (no UUID-keyed View);
  * an intended-bound row the store still flags binding_pending -> NOT rebuilt;
  * an abstained/uncommitted decision -> never persisted;
  * re-perception is idempotent (same assertion_key at the store);
  * time-basis selection (explicit / episode_reference / learned_fallback);
  * activation ordering — activate + schema-ready check BEFORE the first record; refuse over a
    legacy store or an unready schema records nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.typed_assertion_repository import ScalarStateActivationError
from menhir.services.typed_scalar_perception import (
    PERCEPTION_EVIDENCE_TIER,
    ScalarStateNotActivatedError,
    TypedScalarDecision,
    TypedScalarPerceptionService,
    TypedScalarProposal,
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
        source_key=p.source_key, committed=committed,
        reason="unanimous" if committed else "scattered",
        veto="commit" if committed else "self_consistency",
        agreement=1.0 if committed else 0.5, k=1, distribution={}, proposal=p,
    )


def _record_sink():
    """A record_assertion seam that mirrors the store's binding_pending rule: an advisory sentinel
    subject (or an explicitly-forced uuid) is unbound; a real entity uuid is bound. `force_mismatch`
    simulates the C.4.4 already-bound-to-a-different-entity rejection."""
    recorded: list = []
    force_pending: set[str] = set()
    force_mismatch: set[str] = set()

    def record(a):
        recorded.append(a)
        pending = a.subject_uuid.startswith("unbound:") or a.subject_uuid in force_pending
        mismatch = a.subject_uuid in force_mismatch
        return {"assertion_id": f"a{len(recorded)}", "binding_pending": pending,
                "binding_mismatch": mismatch, "created": True}

    record.recorded = recorded
    record.force_pending = force_pending
    record.force_mismatch = force_mismatch
    return record


def _rebuild_sink():
    def rebuild(u, ns=None):                 # repair passes (u, namespace); bind_and_persist passes (u,)
        rebuild.calls.append(u)
        rebuild.namespaces.append(ns)
        return {"subject_uuid": u, "written": 1}

    rebuild.calls = []
    rebuild.namespaces = []
    return rebuild


# ------------------------------------------------------------------------------- binding outcomes

@pytest.mark.unit
def test_unique_binding_persists_bound_and_rebuilds():
    rec, reb = _record_sink(), _rebuild_sink()
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "User"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
    )
    assert out["persisted"] == 1 and out["bound"] == 1 and out["advisory"] == 0
    assert out["rebuilt"] == 1 and reb.calls == ["ent-1"]
    a = rec.recorded[0]
    assert a.subject_uuid == "ent-1" and a.subject_display == "User"   # display from the entity
    assert out["results"][0]["bound"] and not out["results"][0]["binding_pending"]


@pytest.mark.unit
def test_history_failure_keeps_projection_pending_marker():
    rec = _record_sink()
    marked: list[list[str]] = []

    def incomplete_rebuild(_subject):
        return {
            "complete": False,
            "state": {"complete": True},
            "history": {"complete": False, "error": True},
        }

    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "User"}],
        record_assertion=rec, rebuild_scalar_state=incomplete_rebuild,
        mark_projection_complete=lambda ids: marked.append(ids), now=lambda: _FIXED_NOW,
    )
    assert out["bound"] == 1
    assert out["rebuilt"] == 0
    assert out["results"][0]["projection_incomplete"] is True
    assert marked == []


@pytest.mark.unit
def test_write_time_embedding_stamps_name_embedding():
    # 4a.1: with an embed seam + version, the assertion carries a name_embedding from its stated_span
    # (the user's own words) and the embed_version, so the observation is searchable at write time.
    rec, reb = _record_sink(), _rebuild_sink()
    seen: list = []
    bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user", stated_span="I own 20 rare coins"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "User"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        embed=lambda text: seen.append(text) or [0.1, 0.2, 0.3], embed_version="m1",
    )
    a = rec.recorded[0]
    assert seen == ["I own 20 rare coins"]                       # embedded the user's own words
    assert a.name_embedding == [0.1, 0.2, 0.3] and a.embed_version == "m1"


@pytest.mark.unit
def test_no_embedder_leaves_name_embedding_none():
    # No embed seam (or no version) -> name_embedding stays None; the resumable backfill fills it later.
    rec, reb = _record_sink(), _rebuild_sink()
    bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "User"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        embed=lambda text: [0.1], embed_version=None,             # embedder present but NO version -> skip
    )
    a = rec.recorded[0]
    assert a.name_embedding is None and a.embed_version is None


@pytest.mark.unit
def test_embed_failure_leaves_none_and_still_persists():
    # An embed provider error must NEVER drop the assertion write -> name_embedding None, row persisted.
    rec, reb = _record_sink(), _rebuild_sink()

    def _boom(text):
        raise RuntimeError("provider down")

    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "User"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        embed=_boom, embed_version="m1",
    )
    a = rec.recorded[0]
    assert a.name_embedding is None and a.embed_version is None   # embed failure -> no embedding
    assert out["bound"] == 1                                      # ... but the write still happened


@pytest.mark.unit
def test_zero_candidates_is_advisory_no_rebuild():
    rec, reb = _record_sink(), _rebuild_sink()
    d = _decision(_prop(subject_text="nobody"))
    out = bind_and_persist_typed_scalars(
        [d], linked_entities_for_episode=lambda e: [],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
    )
    assert out["bound"] == 0 and out["advisory"] == 1 and out["rebuilt"] == 0
    assert reb.calls == []
    a = rec.recorded[0]
    assert a.subject_uuid == advisory_subject_uuid(d.source_key)   # deterministic sentinel
    assert out["results"][0]["binding_pending"] and not out["results"][0]["bound"]


@pytest.mark.unit
def test_multiple_candidates_is_advisory():
    rec, reb = _record_sink(), _rebuild_sink()
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        # two surviving entities both normalize to "user" -> ambiguous -> abstain from authority
        linked_entities_for_episode=lambda e: [
            {"uuid": "ent-1", "name": "user"}, {"uuid": "ent-2", "name": "User"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
    )
    assert out["advisory"] == 1 and out["bound"] == 0 and reb.calls == []
    assert rec.recorded[0].subject_uuid.startswith("unbound:")


# ------------------------------------------------------ same-namespace lookup fallback (C.4.3)

def _lookup_seam(rows_by_spelling):
    """A look-alike `lookup_namespace_entities` seam recording the exact spellings it was asked for."""
    calls: list[list[str]] = []

    def lookup(namespace, spellings):
        calls.append((namespace, list(spellings)))
        return list(rows_by_spelling)
    lookup.calls = calls
    return lookup


@pytest.mark.unit
def test_namespace_lookup_binds_only_after_local_failure():
    # exact local episode matching FIRST: when the episode already yields a unique local survivor, the
    # same-namespace repository is never consulted (byte-compatible local-first ordering).
    rec, reb = _record_sink(), _rebuild_sink()
    lookup = _lookup_seam([{"uuid": "ent-ns", "name": "black shoes"}])
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="my new black shoes"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-local", "name": "black shoes"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        namespace="ns", lookup_namespace_entities=lookup,
    )
    assert out["bound"] == 1 and reb.calls == ["ent-local"]
    assert lookup.calls == []                                  # local won; the fallback never ran


@pytest.mark.unit
def test_namespace_lookup_unique_fallback_binds():
    # local fails (no episode-linked survivor); the optional same-namespace lookup returns a UNIQUE
    # entity for the constrained spelling "black shoes" (from "my new black shoes") -> bound.
    rec, reb = _record_sink(), _rebuild_sink()
    lookup = _lookup_seam([{"uuid": "ent-ns", "name": "black shoes"}])
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="my new black shoes"))],
        linked_entities_for_episode=lambda e: [],              # local abstains
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        namespace="ns", lookup_namespace_entities=lookup,
    )
    assert out["bound"] == 1 and out["advisory"] == 0 and reb.calls == ["ent-ns"]
    assert rec.recorded[0].subject_uuid == "ent-ns"
    # the exact normalized spelling list was handed to the repository (bounded, target-first)
    assert lookup.calls[0][0] == "ns"
    assert lookup.calls[0][1][0] == "my new black shoes"       # exact first
    assert "black shoes" in lookup.calls[0][1]                 # constrained variant included


@pytest.mark.unit
def test_namespace_lookup_ambiguity_abstains():
    # the same-namespace fallback is fed through the SAME unique fail-closed binder: two entities for
    # one spelling -> abstain from authority (advisory), never a guess.
    rec, reb = _record_sink(), _rebuild_sink()
    lookup = _lookup_seam([
        {"uuid": "ent-a", "name": "black shoes"},
        {"uuid": "ent-b", "name": "Black Shoes"},
    ])
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="my new black shoes"))],
        linked_entities_for_episode=lambda e: [],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        namespace="ns", lookup_namespace_entities=lookup,
    )
    assert out["bound"] == 0 and out["advisory"] == 1 and reb.calls == []
    assert rec.recorded[0].subject_uuid.startswith("unbound:")


@pytest.mark.unit
def test_namespace_lookup_skipped_without_nonblank_namespace():
    # repository lookup requires a NONBLANK namespace: with namespace=None the seam is never called and
    # the result stays advisory, not a cross-tenant guess.
    rec, reb = _record_sink(), _rebuild_sink()
    lookup = _lookup_seam([{"uuid": "ent-ns", "name": "black shoes"}])
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="my new black shoes"))],
        linked_entities_for_episode=lambda e: [],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
        namespace=None, lookup_namespace_entities=lookup,
    )
    assert out["advisory"] == 1 and out["bound"] == 0 and lookup.calls == []


@pytest.mark.unit
def test_repair_uses_namespace_lookup_fallback():
    # pending-binding repair falls back to the same-namespace repository lookup (in the ROW's OWN
    # namespace) when exact local episode matching still fails, so a now-resolvable advisory binds.
    rec, reb = _record_sink(), _rebuild_sink()
    lookup = _lookup_seam([{"uuid": "ent-ns", "name": "black shoes"}])
    out = repair_pending_bindings(
        [_pending_row(subject_display="my new black shoes", namespace="tenant-a")],
        linked_entities_for_episode=lambda e: [],       # local still abstains
        record_assertion=rec, rebuild_scalar_state=reb,
        lookup_namespace_entities=lookup,
    )
    assert out["repaired"] == 1 and out["still_pending"] == 0 and reb.calls == ["ent-ns"]
    assert reb.namespaces == ["tenant-a"]               # rebuilt in the row's namespace
    assert rec.recorded[0].subject_uuid == "ent-ns"
    assert lookup.calls[0][0] == "tenant-a"             # consulted in the ROW's namespace


# ----------------------------------------------------------- constrained syntactic variant

def test_constrained_variant_strips_determiner_and_new():
    from menhir.services.typed_scalar_rules import _subject_variants
    # "my new black shoes" -> determiner-stripped "new black shoes" FIRST, then "black shoes"; the
    # new-keeping spelling always precedes the new-stripped one.
    variants = _subject_variants("my new black shoes")
    assert variants[:2] == ["new black shoes", "black shoes"]


def test_constrained_variant_never_strips_new_from_arbitrary_names():
    from menhir.services.typed_scalar_rules import _subject_variants
    # a bare leading "new" with NO determiner is an arbitrary name and must never be rewritten.
    assert _subject_variants("New York") == []                # no determiner -> no rewrite at all
    # the plain article "the" strips the determiner but NOT "new": "the new house" keeps "new house"
    # (the article+new shape is far more often a proper name than a possessed object).
    assert _subject_variants("the new house") == ["new house"]
    # a POSSESSIVE determiner DOES permit the constrained new-strip (after the new-keeping form).
    assert _subject_variants("my new black shoes")[:2] == ["new black shoes", "black shoes"]
    # an ordinary determiner-only subject is unaffected.
    assert _subject_variants("my boots") == ["boots"]


@pytest.mark.unit
def test_already_bound_mismatch_is_not_rebuilt_and_is_surfaced():
    # C.4.4: the store reports the claim is already bound to a DIFFERENT entity (binding_mismatch).
    # bind_and_persist must NOT count it bound, NOT rebuild for the presented subject, and surface it.
    rec, reb = _record_sink(), _rebuild_sink()
    rec.force_mismatch.add("ent-1")
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
    )
    assert out["bound"] == 0 and out["advisory"] == 0 and out["mismatched"] == 1
    assert reb.calls == []                                   # no rebuild for the mismatched entity
    assert out["results"][0]["binding_mismatch"] and not out["results"][0]["bound"]


@pytest.mark.unit
def test_intended_bound_but_store_pending_is_not_rebuilt():
    # unique name match, but the store reports binding_pending (episode/entity node absent at write):
    # the slot is NOT materialized here — left for the repair pass — so no View rebuild fires.
    rec, reb = _record_sink(), _rebuild_sink()
    rec.force_pending.add("ent-1")
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(subject_text="user"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
    )
    assert out["bound"] == 0 and out["advisory"] == 1 and reb.calls == []
    assert out["results"][0]["binding_pending"]


# ------------------------------------------------------------------------------- abstain / idempotent

@pytest.mark.unit
def test_uncommitted_decision_is_never_persisted():
    rec, reb = _record_sink(), _rebuild_sink()
    out = bind_and_persist_typed_scalars(
        [_decision(_prop(), committed=False)],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW,
    )
    assert out["persisted"] == 0 and rec.recorded == [] and reb.calls == []


@pytest.mark.unit
def test_reperception_is_idempotent_same_assertion_key():
    # binding + persist twice over the same claim yields the SAME assertion_key, so the durable store
    # collapses it to one node (idempotent re-perception). Identity is source_key-anchored.
    rec, reb = _record_sink(), _rebuild_sink()
    ents = lambda e: [{"uuid": "ent-1", "name": "user"}]  # noqa: E731
    for _ in range(2):
        bind_and_persist_typed_scalars(
            [_decision(_prop(subject_text="user"))],
            linked_entities_for_episode=ents, record_assertion=rec,
            rebuild_scalar_state=reb, now=lambda: _FIXED_NOW)
    assert len(rec.recorded) == 2
    assert rec.recorded[0].assertion_key == rec.recorded[1].assertion_key


# ------------------------------------------------------------------- authority (forced agent tier)

@pytest.mark.unit
def test_evidence_tier_is_forced_to_agent_regardless_of_decision():
    # a directly-constructed decision requesting a higher tier must NOT grant itself authority: the
    # persistence boundary forces PERCEPTION_EVIDENCE_TIER ("agent") for this probabilistic path.
    rec, reb = _record_sink(), _rebuild_sink()
    d = _decision(_prop(subject_text="user"))
    d = TypedScalarDecision(
        source_key=d.source_key, committed=True, reason=d.reason, veto=d.veto,
        agreement=d.agreement, k=d.k, distribution=d.distribution, proposal=d.proposal,
        evidence_tier="user",                        # attempt to smuggle a high tier
    )
    bind_and_persist_typed_scalars(
        [d], linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb, now=lambda: _FIXED_NOW)
    assert rec.recorded[0].evidence_tier == PERCEPTION_EVIDENCE_TIER == "agent"


# ------------------------------------------------------------------- binding candidate view exclusion

class _RowsNeo4j:
    """Returns canned rows for any query; records the executed query text."""

    def __init__(self, rows):
        self._rows = rows
        self.queries: list[str] = []

    def execute(self, query, params=None):
        self.queries.append(query)
        return self._rows


@pytest.mark.unit
def test_binding_candidates_exclude_view_entities():
    # a real entity and three derived View :Entity nodes are all linked to the episode; only the real
    # entity may be a binding candidate. (Views are stored as :Entity with is_view/view_kind, counters
    # with is_quantstate, and the scheduler writes counter Views BEFORE scalar binding runs.)
    rows = [
        {"uuid": "ent-real", "name": "user", "is_view": False, "is_quantstate": False,
         "view_kind": None},
        {"uuid": "v-scalar", "name": "user", "is_view": True, "is_quantstate": False,
         "view_kind": "scalar_state"},
        {"uuid": "v-counter", "name": "user", "is_view": True, "is_quantstate": True,
         "view_kind": "counter"},
        {"uuid": "v-legacy", "name": "user", "is_view": False, "is_quantstate": True,
         "view_kind": None},
    ]
    fake = _RowsNeo4j(rows)
    repo = EpisodeRepository(neo4j=fake)
    got = repo.fetch_linked_entities_for_episode("ep-1")
    assert got == [{"uuid": "ent-real", "name": "user"}]     # Python defense-in-depth filter
    # the exclusion is ALSO expressed in the Cypher at source, not only in the Python guard
    executed = fake.queries[0]
    assert "is_view" in executed and "is_quantstate" in executed and "view_kind IS NULL" in executed


# ------------------------------------------------------------------------------- time basis

@pytest.mark.unit
def test_time_basis_explicit_uses_stated_when():
    rec = _record_sink()
    bind_and_persist_typed_scalars(
        [_decision(_prop(when="2026-07-01T00:00:00+00:00"))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=_rebuild_sink(), now=lambda: _FIXED_NOW)
    a = rec.recorded[0]
    assert a.time_basis == "explicit" and a.valid_at == "2026-07-01T00:00:00+00:00"


@pytest.mark.unit
def test_time_basis_episode_reference_when_no_stated_when():
    rec = _record_sink()
    bind_and_persist_typed_scalars(
        [_decision(_prop(when=None))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=_rebuild_sink(),
        episode_reference_time=lambda e: "2026-06-15T00:00:00+00:00", now=lambda: _FIXED_NOW)
    a = rec.recorded[0]
    assert a.time_basis == "episode_reference" and a.valid_at == "2026-06-15T00:00:00+00:00"


@pytest.mark.unit
def test_time_basis_learned_fallback_when_no_when_and_no_reference():
    rec = _record_sink()
    bind_and_persist_typed_scalars(
        [_decision(_prop(when=None))],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=_rebuild_sink(), now=lambda: _FIXED_NOW)
    a = rec.recorded[0]
    assert a.time_basis == "learned_fallback" and a.valid_at == _FIXED_NOW


# ------------------------------------------------------------------------------- activation ordering


@dataclass
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


class _FakeService:
    def __init__(self):
        self.rebuilt = []

    def rebuild_scalar_state(self, subject_uuid, *, namespace=None, as_of=None):
        self.rebuilt.append(subject_uuid)
        return {"subject_uuid": subject_uuid, "written": 1}


class _FakeAdapter:
    """Records call ORDER so we can prove activation precedes the first record."""

    def __init__(self, *, entities, schema_ready=True, activate_raises=None, pending=None):
        self._entities = entities
        self._schema_ready = schema_ready
        self._activate_raises = activate_raises
        self._pending = pending or []
        self.calls: list[str] = []
        self.recorded: list = []

    def activate_scalar_state(self):
        self.calls.append("activate")
        if self._activate_raises is not None:
            raise self._activate_raises
        return {"queries_executed": 3}

    def scalar_state_schema_ready(self):
        self.calls.append("schema_ready")
        return self._schema_ready

    def fetch_linked_entities_for_episode(self, episode_uuid):
        self.calls.append("fetch")
        return list(self._entities)

    def record_typed_assertion(self, assertion):
        self.calls.append("record")
        self.recorded.append(assertion)
        real = any(e["uuid"] == assertion.subject_uuid for e in self._entities)
        return {"assertion_id": f"a{len(self.recorded)}", "binding_pending": not real, "created": True}

    def pending_advisory_assertions(self, *, namespaces=None, limit=200):
        self.calls.append("pending")
        self.pending_namespaces = namespaces
        rows = self._pending
        if namespaces is not None:
            rows = [r for r in rows if r.get("namespace") in namespaces]
        return list(rows)

    def mark_binding_repair_attempted(self, assertion_ids, *, at):
        self.calls.append("mark_attempted")
        self.marked_attempted = list(assertion_ids)
        return len(assertion_ids)

    def mark_projection_complete(self, assertion_ids):
        self.calls.append("mark_projection_complete")
        self.projection_cleared = list(assertion_ids)
        return len(assertion_ids)


def _row(**over):
    base = dict(episode=0, subject="user", attribute="wake", scope="", value_kind="clock_time",
                unit="", operation="absolute", value="07:30", when="2026-07-01",
                stated_span="I wake at 07:30")
    base.update(over)
    return base


@pytest.mark.unit
def test_activation_runs_and_verifies_before_first_record():
    adapter = _FakeAdapter(entities=[{"uuid": "ent-user", "name": "user"}])
    svc = _FakeService()
    coord = TypedScalarPerceptionService(adapter, svc)
    eps = [_Ep(uuid="ep-1", content="I wake at 07:30 every day.")]
    out = coord.perceive_and_persist(eps, _llm([_row()]), k=1, threshold=1.0)

    assert out["committed"] == 1 and out["bound"] == 1
    assert adapter.calls[0] == "activate"                      # activate is first
    assert adapter.calls[1] == "schema_ready"                  # schema verified right after
    assert adapter.calls.index("activate") < adapter.calls.index("record")
    assert svc.rebuilt == ["ent-user"]


@pytest.mark.unit
def test_activation_is_run_once_across_calls():
    adapter = _FakeAdapter(entities=[{"uuid": "ent-user", "name": "user"}])
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    eps = [_Ep(uuid="ep-1", content="I wake at 07:30 every day.")]
    coord.perceive_and_persist(eps, _llm([_row()]), k=1)
    coord.perceive_and_persist(eps, _llm([_row()]), k=1)
    assert adapter.calls.count("activate") == 1               # activated once per service instance


@pytest.mark.unit
def test_legacy_store_refuses_and_records_nothing():
    adapter = _FakeAdapter(
        entities=[{"uuid": "ent-user", "name": "user"}],
        activate_raises=ScalarStateActivationError(1, 0))
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    eps = [_Ep(uuid="ep-1", content="I wake at 07:30 every day.")]
    with pytest.raises(ScalarStateActivationError):
        coord.perceive_and_persist(eps, _llm([_row()]), k=1)
    assert adapter.recorded == [] and "record" not in adapter.calls


@pytest.mark.unit
def test_unready_schema_fails_closed_and_records_nothing():
    adapter = _FakeAdapter(entities=[{"uuid": "ent-user", "name": "user"}], schema_ready=False)
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    eps = [_Ep(uuid="ep-1", content="I wake at 07:30 every day.")]
    with pytest.raises(ScalarStateNotActivatedError):
        coord.perceive_and_persist(eps, _llm([_row()]), k=1)
    assert adapter.recorded == [] and "record" not in adapter.calls


@pytest.mark.unit
@pytest.mark.parametrize("bad_k", [0, -1, True, 1.0, "1"])
def test_invalid_k_fails_closed_before_activation(bad_k):
    # k must be a real integer >= 1; a bad k raises BEFORE activation/record (fail closed, not a
    # silent collapse to a single sample).
    adapter = _FakeAdapter(entities=[{"uuid": "ent-user", "name": "user"}])
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    eps = [_Ep(uuid="ep-1", content="I wake at 07:30 every day.")]
    with pytest.raises(ValueError):
        coord.perceive_and_persist(eps, _llm([_row()]), k=bad_k)
    assert adapter.calls == [] and adapter.recorded == []


@pytest.mark.unit
def test_invalid_threshold_fails_closed():
    adapter = _FakeAdapter(entities=[{"uuid": "ent-user", "name": "user"}])
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    eps = [_Ep(uuid="ep-1", content="I wake at 07:30 every day.")]
    with pytest.raises(ValueError):
        coord.perceive_and_persist(eps, _llm([_row()]), k=1, threshold=0.0)
    assert adapter.recorded == []


# ------------------------------------------------- pending-binding repair pass (C.4.4)

def _pending_row(**over):
    base = dict(
        assertion_id="a-sk-1", source_key="sk-1", subject_uuid="unbound:sk-1", subject_display="user",
        binding_pending=True, projection_pending=False, attribute="wake", scope="",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        stated_span="wake at 07:30", episode_uuid="ep-1", span_start=0, span_end=13,
        claim_ordinal=0, valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", time_basis="explicit", perceiver_version="v1",
        namespace="ns",
    )
    base.update(over)
    return base


@pytest.mark.unit
def test_repair_marks_every_scanned_row_attempted_regardless_of_outcome():
    # fairness: EVERY examined row (bound, unbindable, mismatch, store-pending) is passed to
    # mark_attempted so the repository can advance the retry frontier — else the same first page
    # would be rescanned forever and newer advisories would starve.
    rec, reb = _record_sink(), _rebuild_sink()
    rec.force_mismatch.add("ent-mis")
    marked: list = []
    rows = [
        _pending_row(assertion_id="a1", source_key="s1", subject_display="user"),      # binds
        _pending_row(assertion_id="a2", source_key="s2", subject_display="nobody"),    # unbindable
    ]
    out = repair_pending_bindings(
        rows,
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb,
        mark_attempted=lambda ids: marked.extend(ids),
    )
    assert out["repaired"] == 1 and out["still_pending"] == 1
    assert marked == ["a1", "a2"]                            # BOTH stamped, not just the repaired one


@pytest.mark.unit
def test_repair_binds_now_resolvable_advisory_and_rebuilds():
    rec, reb = _record_sink(), _rebuild_sink()
    out = repair_pending_bindings(
        [_pending_row(subject_display="user")],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb,
    )
    assert out["scanned"] == 1 and out["repaired"] == 1 and out["rebuilt"] == 1
    assert out["still_pending"] == 0 and out["mismatched"] == 0
    assert reb.calls == ["ent-1"]
    a = rec.recorded[0]
    assert a.subject_uuid == "ent-1"                          # re-recorded with the resolved subject
    assert a.evidence_tier == PERCEPTION_EVIDENCE_TIER        # authority stays agent on repair
    assert a.value == "07:30" and a.perceiver_version == "v1"  # interpretation preserved (same node)


@pytest.mark.unit
def test_repair_leaves_still_unbindable_advisory_pending():
    rec, reb = _record_sink(), _rebuild_sink()
    for entities in ([], [{"uuid": "e1", "name": "user"}, {"uuid": "e2", "name": "User"}]):
        out = repair_pending_bindings(
            [_pending_row()], linked_entities_for_episode=lambda e, _ents=entities: _ents,
            record_assertion=rec, rebuild_scalar_state=reb)
        assert out["repaired"] == 0 and out["still_pending"] == 1
    assert rec.recorded == [] and reb.calls == []            # nothing recorded/rebuilt


@pytest.mark.unit
def test_repair_resolved_but_store_still_pending_is_not_counted():
    # resolved a unique subject, but the store reports binding_pending (episode/entity node absent):
    # not repaired, not rebuilt.
    rec, reb = _record_sink(), _rebuild_sink()
    rec.force_pending.add("ent-1")
    out = repair_pending_bindings(
        [_pending_row()], linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb)
    assert out["repaired"] == 0 and out["still_pending"] == 1 and reb.calls == []


@pytest.mark.unit
def test_repair_mismatch_is_surfaced_not_rebuilt():
    # the claim was bound to a different entity since the advisory was written -> mismatch, left to owner.
    rec, reb = _record_sink(), _rebuild_sink()
    rec.force_mismatch.add("ent-1")
    out = repair_pending_bindings(
        [_pending_row()], linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb)
    assert out["mismatched"] == 1 and out["repaired"] == 0 and reb.calls == []


@pytest.mark.unit
def test_coordinator_repair_enforces_activation_and_pulls_pending():
    adapter = _FakeAdapter(
        entities=[{"uuid": "ent-user", "name": "user"}],
        pending=[_pending_row(subject_display="user", episode_uuid="ep-1")])
    svc = _FakeService()
    coord = TypedScalarPerceptionService(adapter, svc)
    out = coord.repair_pending_bindings()
    assert adapter.calls[0] == "activate" and "pending" in adapter.calls
    assert out["repaired"] == 1 and svc.rebuilt == ["ent-user"]
    assert "mark_attempted" in adapter.calls                # frontier advanced
    assert adapter.marked_attempted == ["a-sk-1"]           # the scanned advisory's id


@pytest.mark.unit
def test_coordinator_repair_refused_over_legacy_store_records_nothing():
    adapter = _FakeAdapter(
        entities=[{"uuid": "ent-user", "name": "user"}],
        pending=[_pending_row()], activate_raises=ScalarStateActivationError(1, 0))
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    with pytest.raises(ScalarStateActivationError):
        coord.repair_pending_bindings()
    assert "pending" not in adapter.calls and adapter.recorded == []


@pytest.mark.unit
def test_repair_rebuilds_in_row_namespace_not_a_fixed_outer_one():
    # blocker 1: a globally-selected tenant-a advisory must rebuild its View in tenant-a, never the
    # default/None silo. The repair passes each row's OWN namespace to the rebuild seam.
    rec, reb = _record_sink(), _rebuild_sink()
    repair_pending_bindings(
        [_pending_row(namespace="tenant-a", subject_display="user")],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb)
    assert reb.calls == ["ent-1"] and reb.namespaces == ["tenant-a"]   # row namespace, not None


@pytest.mark.unit
def test_repair_fail_closed_skips_row_outside_allowlist():
    rec, reb = _record_sink(), _rebuild_sink()
    out = repair_pending_bindings(
        [_pending_row(namespace="tenant-b", subject_display="user")],
        linked_entities_for_episode=lambda e: [{"uuid": "ent-1", "name": "user"}],
        record_assertion=rec, rebuild_scalar_state=reb, allowed_namespaces={"tenant-a"})
    assert out["repaired"] == 0 and rec.recorded == [] and reb.calls == []


@pytest.mark.unit
def test_repair_projection_only_row_rebuilds_without_reresolving():
    # crash-recovery category: already bound (binding_pending False) but projection_pending -> just
    # rebuild the stored real subject; no entity re-resolution, no re-record.
    rec, reb = _record_sink(), _rebuild_sink()
    fetch_calls = []
    out = repair_pending_bindings(
        [_pending_row(binding_pending=False, projection_pending=True, subject_uuid="ent-real",
                      namespace="ns")],
        linked_entities_for_episode=lambda e: fetch_calls.append(e) or [],
        record_assertion=rec, rebuild_scalar_state=reb)
    assert out["repaired"] == 1 and reb.calls == ["ent-real"] and reb.namespaces == ["ns"]
    assert rec.recorded == [] and fetch_calls == []          # no re-record, no re-resolution


@pytest.mark.unit
def test_repair_row_level_exception_is_isolated_and_frontier_still_advances():
    # blocker 3: a rebuild that raises must not abort the batch or block the fairness stamp; the row
    # is counted errored and left for retry, and OTHER rows still process.
    rec = _record_sink()
    marked: list = []

    def reb(u, ns=None):
        if u == "ent-bad":
            raise RuntimeError("rebuild boom")
        reb.ok.append(u)
        return {"subject_uuid": u}
    reb.ok = []

    rows = [
        _pending_row(assertion_id="a1", source_key="s1", subject_display="bad", episode_uuid="epb"),
        _pending_row(assertion_id="a2", source_key="s2", subject_display="good", episode_uuid="epg"),
    ]

    def _ents(e):
        return [{"uuid": "ent-bad", "name": "bad"}] if e == "epb" else [{"uuid": "ent-ok", "name": "good"}]

    out = repair_pending_bindings(
        rows, linked_entities_for_episode=_ents, record_assertion=rec, rebuild_scalar_state=reb,
        mark_attempted=lambda ids: marked.extend(ids))
    assert out["errored"] == 1 and out["repaired"] == 1 and reb.ok == ["ent-ok"]
    assert marked == ["a1", "a2"]                            # BOTH stamped despite the row-1 exception


@pytest.mark.unit
@pytest.mark.parametrize("bad_limit", [-1, True, 1.5, "5"])
def test_coordinator_repair_validates_limit(bad_limit):
    adapter = _FakeAdapter(entities=[], pending=[])
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    with pytest.raises(ValueError):
        coord.repair_pending_bindings(limit=bad_limit)


@pytest.mark.unit
def test_coordinator_repair_dedupes_namespace_allowlist():
    adapter = _FakeAdapter(entities=[], pending=[])
    coord = TypedScalarPerceptionService(adapter, _FakeService())
    coord.repair_pending_bindings(namespaces=["a", "a", "b"])
    assert sorted(adapter.pending_namespaces) == ["a", "b"]  # deduped allowlist to a single query
