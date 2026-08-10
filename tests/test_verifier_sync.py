"""Graph-native verifier sync: executors read a source of truth, the sync loop refreshes the
register each verifier maintains (self-superseding) and flags referencing beliefs on change."""

from __future__ import annotations

import pytest

from menhir.services.verifier_sync import (
    DEFAULT_VERIFIER_SEEDS,
    VerifierContext,
    env_key_executor,
    seed_default_verifiers,
    sync_verifiers,
)


# --------------------------------------------------------------------------- executor unit tests

@pytest.mark.unit
def test_env_key_executor_bool_true_false(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_FLAG", "true")
    res = env_key_executor({"key": "MENHIR_TEST_FLAG"}, VerifierContext())
    assert res.ok and res.value == 1.0 and res.display == "true"

    monkeypatch.setenv("MENHIR_TEST_FLAG", "false")
    res = env_key_executor({"key": "MENHIR_TEST_FLAG"}, VerifierContext())
    assert res.ok and res.value == 0.0


@pytest.mark.unit
def test_env_key_executor_int_and_unset(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_PORT", "8090")
    res = env_key_executor({"key": "MENHIR_TEST_PORT", "as": "int"}, VerifierContext())
    assert res.ok and res.value == 8090.0

    monkeypatch.delenv("MENHIR_TEST_MISSING", raising=False)
    res = env_key_executor({"key": "MENHIR_TEST_MISSING"}, VerifierContext())
    assert res.ok is False  # unreadable source -> register left untouched


@pytest.mark.unit
def test_env_key_executor_settings_fallback(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_TEST_FALLBACK", raising=False)

    class _S:
        experience_counter_enabled = True

    res = env_key_executor(
        {"key": "MENHIR_TEST_FALLBACK", "settings_attr": "experience_counter_enabled"},
        VerifierContext(settings=_S()),
    )
    assert res.ok and res.value == 1.0


# --------------------------------------------------------------------------- sync fakes

class _FakeCounterGraph:
    def __init__(self, existing: float | None = None):
        self._existing = existing
        self.records: list[dict] = []

    def fetch_counter(self, *, subject, counter, namespace=None):
        if self._existing is None:
            return None
        return {"subject": subject, "counter": counter, "value": self._existing}

    def record_counter(self, **kwargs):
        self.records.append(kwargs)
        return {"uuid": "reg-uuid", "view_key": "k", "created": True}


class _FakeRepo:
    def __init__(self, verifiers):
        self._verifiers = verifiers
        self.edges: list[tuple[str, str]] = []
        self.stamps: list[dict] = []
        self.flag_calls: list[str] = []

    def list_verifiers(self):
        return self._verifiers

    def ensure_verified_edge(self, *, register_uuid, verifier_uuid):
        self.edges.append((register_uuid, verifier_uuid))

    def stamp_verifier(self, *, verifier_uuid, value, display, at):
        self.stamps.append({"uuid": verifier_uuid, "value": value, "display": display})

    def flag_referencing_beliefs(self, *, verifier_uuid, new_value, display, at):
        self.flag_calls.append(verifier_uuid)
        return 3


def _verifier(kind="env_key", params='{"key":"MENHIR_TEST_FLAG"}'):
    return {
        "uuid": "v1", "verifier_kind": kind, "verifier_params": params,
        "register_subject": "menhir-config", "register_counter": "experience_counter_enabled",
    }


# --------------------------------------------------------------------------- sync behavior

@pytest.mark.unit
def test_sync_changed_value_records_links_stamps_and_flags(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_FLAG", "true")
    graph = _FakeCounterGraph(existing=0.0)  # was false, now true -> changed
    repo = _FakeRepo([_verifier()])

    out = sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert len(graph.records) == 1 and graph.records[0]["value"] == 1.0
    assert graph.records[0]["source"] == "verifier-sync"
    assert repo.edges == [("reg-uuid", "v1")]           # register -[:VERIFIED_BY]-> verifier
    assert repo.stamps and repo.stamps[0]["display"] == "true"
    assert repo.flag_calls == ["v1"]                     # referencing beliefs flagged on change
    assert out[0]["status"] == "refreshed" and out[0]["changed"] is True
    assert out[0]["beliefs_flagged"] == 3


@pytest.mark.unit
def test_sync_unchanged_value_refreshes_without_flagging(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_FLAG", "true")
    graph = _FakeCounterGraph(existing=1.0)  # already true -> unchanged
    repo = _FakeRepo([_verifier()])

    out = sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert len(graph.records) == 1                       # still refreshed (freshness) ...
    assert repo.flag_calls == []                         # ... but no belief flagged
    assert out[0]["changed"] is False and out[0]["status"] == "refreshed"


@pytest.mark.unit
def test_sync_skips_unknown_kind_without_executing() -> None:
    graph = _FakeCounterGraph()
    repo = _FakeRepo([_verifier(kind="shell_command", params='{"cmd":"rm -rf /"}')])

    out = sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert out[0]["status"] == "skipped_unknown_kind"
    assert graph.records == [] and repo.flag_calls == []  # nothing executed or written


@pytest.mark.unit
def test_sync_unreadable_source_leaves_register_untouched(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_TEST_FLAG", raising=False)
    graph = _FakeCounterGraph(existing=1.0)
    repo = _FakeRepo([_verifier()])

    out = sync_verifiers(repo=repo, graph_adapter=graph, context=VerifierContext())

    assert out[0]["status"] == "source_unavailable"
    assert graph.records == []                            # never supersede on inability to read


@pytest.mark.unit
def test_seed_default_verifiers_upserts_each_binding() -> None:
    calls: list[dict] = []

    class _Repo:
        def upsert_verifier(self, **kw):
            calls.append(kw)
            return f"uuid-{len(calls)}"

    uuids = seed_default_verifiers(_Repo())
    assert len(uuids) == len(DEFAULT_VERIFIER_SEEDS)
    counters = {c["register_counter"] for c in calls}
    assert "experience_counter_enabled" in counters  # the motivating verifier is seeded


@pytest.mark.unit
def test_scheduler_registers_verifier_job_only_when_enabled_and_repo_present() -> None:
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    class _Ingest:
        def get_queue_depth(self): return 0

    common = dict(ingest_service=_Ingest(), graph_adapter=object())

    # disabled -> no job
    s_off = MaintenanceScheduler(**common, verifier_sync_enabled=False, verifier_repo=object())
    assert "sync_verifiers" not in s_off._jobs
    # enabled but no repo -> no job (nothing to run)
    s_norepo = MaintenanceScheduler(**common, verifier_sync_enabled=True, verifier_repo=None)
    assert "sync_verifiers" not in s_norepo._jobs
    # enabled + repo -> job registered
    s_on = MaintenanceScheduler(**common, verifier_sync_enabled=True, verifier_repo=object())
    assert "sync_verifiers" in s_on._jobs


@pytest.mark.unit
def test_sync_one_broken_executor_does_not_abort_the_rest(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_FLAG", "true")

    def _boom(params, ctx):
        raise RuntimeError("probe exploded")

    good = _verifier()
    bad = {**_verifier(), "uuid": "v2", "verifier_kind": "boom"}
    graph = _FakeCounterGraph(existing=0.0)
    repo = _FakeRepo([bad, good])

    out = sync_verifiers(
        repo=repo, graph_adapter=graph, context=VerifierContext(),
        executors={"env_key": env_key_executor, "boom": _boom},
    )

    statuses = {r["verifier"]: r["status"] for r in out}
    assert statuses["v2"] == "error"
    assert statuses["v1"] == "refreshed"                 # the good verifier still ran
    assert len(graph.records) == 1
