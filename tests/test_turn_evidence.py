"""Selective `:TurnEvidence` capture (ADR 0001, Claude MVP) — repository, Phase 3 preference, and the
deterministic-triage hook adapter.

No live Neo4j and no live server: a FakeNeo4j records/routes queries, and the hook is exercised as a
pure function. Proves the capture boundary is selective, LLM-free, and correct by construction.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import (
    TurnEvidenceRepository,
    derive_prompt_hash,
    derive_turn_key,
)


class FakeNeo4j:
    """Routes an executed cypher string to canned rows by substring match; records every call."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or []
        self.default = [] if default is None else default
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        for sub, rows in self.routes:
            if sub in query:
                return rows
        return self.default


def _load_hook():
    path = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "menhir_turn_evidence.py"
    spec = importlib.util.spec_from_file_location("menhir_turn_evidence", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- repository


@pytest.mark.unit
def test_record_evidence_creates_node_with_triage():
    fake = FakeNeo4j(routes=[("MERGE (t:TurnEvidence",
                              [{"turn_id": "tid-1", "created": True, "recorded_at": "2026-07-07T00:00:00Z"}])])
    out = TurnEvidenceRepository(fake).record_turn_evidence(
        text="I have 25 movies on my watch list.", session_id="s1", namespace="proj",
        source_kind="claude_code_hook", triage_reason=["number", "i_have"], triage_version="v1")
    assert out["turn_id"] == "tid-1" and out["created"] is True
    q, params = fake.executed[-1]
    assert "MERGE (t:TurnEvidence {turn_key:" in q
    assert ":Entity" not in q and ":Episodic" not in q  # never a recall-visible label
    assert params["role"] == "user" and params["declarant"] == "user"
    assert params["triage_reason"] == ["number", "i_have"] and params["triage_version"] == "v1"


@pytest.mark.unit
def test_record_evidence_stores_provenance_metadata():
    # Provenance branch: source_client/hook_version become node properties; prompt_hash is derived
    # server-side; free-form metadata (project_root/git_*) is JSON-serialized verbatim.
    fake = FakeNeo4j(routes=[("MERGE (t:TurnEvidence",
                              [{"turn_id": "tid-2", "created": True, "recorded_at": "x"}])])
    TurnEvidenceRepository(fake).record_turn_evidence(
        text="I have 25 movies on my watch list.", session_id="s1", namespace="proj",
        source_kind="claude_code_hook", source_client="claude_code",
        hook_version="menhir-turn-evidence-hook-v1", triage_reason=["number"],
        metadata={"project_root": "/repo", "git_branch": "main", "git_commit": "abc1234"})
    q, params = fake.executed[-1]
    assert "t.source_client = $source_client" in q and "t.hook_version = $hook_version" in q
    assert "t.prompt_hash = $prompt_hash" in q
    assert params["source_client"] == "claude_code"
    assert params["hook_version"] == "menhir-turn-evidence-hook-v1"
    assert params["prompt_hash"] == derive_prompt_hash("I have 25 movies on my watch list.")
    assert '"git_branch": "main"' in params["metadata_json"]
    assert '"project_root": "/repo"' in params["metadata_json"]


@pytest.mark.unit
def test_record_evidence_preserves_source_time_separately_from_receive_time():
    fake = FakeNeo4j(routes=[("MERGE (t:TurnEvidence", [{
        "turn_id": "tid-source-time",
        "created": True,
        "recorded_at": "2026-07-29T13:00:00Z",
        "occurred_at": "2023-11-30T20:25:00Z",
    }])])

    out = TurnEvidenceRepository(fake).record_turn_evidence(
        text="I have added 25 postcards.",
        session_id="s1",
        namespace="proj",
        source_kind="archolith-bench",
        occurred_at="2023-11-30T20:25:00Z",
    )

    query, params = fake.executed[-1]
    assert "t.recorded_at = datetime()" in query
    assert "t.occurred_at" in query
    assert params["occurred_at"] == "2023-11-30T20:25:00+00:00"
    assert out["recorded_at"] == "2026-07-29T13:00:00Z"
    assert out["occurred_at"] == "2023-11-30T20:25:00Z"


@pytest.mark.unit
def test_record_evidence_without_provenance_is_non_fatal():
    # Old-shape call: omit source_client/hook_version/metadata entirely -> stores nulls/{} and
    # still derives prompt_hash. Optional provenance never blocks capture.
    fake = FakeNeo4j(routes=[("MERGE (t:TurnEvidence",
                              [{"turn_id": "tid-3", "created": True, "recorded_at": "x"}])])
    out = TurnEvidenceRepository(fake).record_turn_evidence(
        text="I bought one bike for $50.", session_id="s2", source_kind="claude_code_hook")
    assert out["turn_id"] == "tid-3" and out["created"] is True
    _q, params = fake.executed[-1]
    assert params["source_client"] is None and params["hook_version"] is None
    assert params["prompt_hash"] == derive_prompt_hash("I bought one bike for $50.")
    assert params["metadata_json"] == "{}"


@pytest.mark.unit
def test_derive_prompt_hash_deterministic_and_content_sensitive():
    a = derive_prompt_hash("I have 25 movies on my watch list.")
    b = derive_prompt_hash("I have 25 movies on my watch list.")
    c = derive_prompt_hash("I have 26 movies on my watch list.")
    assert a == b and a != c
    # content fingerprint, NOT the idempotency key (no source/session/cwd folded in)
    assert a != derive_turn_key(source_kind="k", session_id="s",
                                text="I have 25 movies on my watch list.", cwd="/p")


@pytest.mark.unit
def test_record_evidence_requires_text_and_valid_role():
    repo = TurnEvidenceRepository(FakeNeo4j())
    with pytest.raises(ValueError):
        repo.record_turn_evidence(text="   ")
    with pytest.raises(ValueError):
        repo.record_turn_evidence(text="hi there", role="bogus")
    with pytest.raises(ValueError, match="occurred_at must be ISO-8601"):
        repo.record_turn_evidence(text="hi there", occurred_at="November-ish")


@pytest.mark.unit
def test_derive_turn_key_idempotent_and_content_sensitive():
    a = derive_turn_key(source_kind="claude_code_hook", session_id="s1", text="hello", cwd="/p")
    b = derive_turn_key(source_kind="claude_code_hook", session_id="s1", text="hello", cwd="/p")
    c = derive_turn_key(source_kind="claude_code_hook", session_id="s1", text="HELLO", cwd="/p")
    assert a == b and a != c


@pytest.mark.unit
def test_derive_turn_key_prefers_prompt_id_so_genuine_repetitions_stay_distinct():
    # G18: two GENUINE repetitions of the SAME text with DIFFERENT prompt_ids must key DIFFERENTLY
    # (distinct evidence sources) -- text-keying would collapse them and break the reinforcement model.
    k_a = derive_turn_key(source_kind="claude_code_hook", session_id="s1", text="I have 20 coins",
                          cwd="/p", prompt_id="pid-A")
    k_b = derive_turn_key(source_kind="claude_code_hook", session_id="s1", text="I have 20 coins",
                          cwd="/p", prompt_id="pid-B")
    assert k_a != k_b
    # a double-fired retry of the SAME submission (same prompt_id) is idempotent -> merges.
    assert k_a == derive_turn_key(source_kind="claude_code_hook", session_id="s1",
                                  text="I have 20 coins", cwd="/p", prompt_id="pid-A")
    # prompt_id DOMINATES the key: the text no longer contributes when a prompt_id is present.
    assert k_a == derive_turn_key(source_kind="claude_code_hook", session_id="s1",
                                  text="totally different text", cwd="/p", prompt_id="pid-A")


@pytest.mark.unit
def test_derive_turn_key_falls_back_to_text_without_prompt_id():
    # Backward-compat: None / blank prompt_id -> keyed on text exactly as before (older CC / producers
    # that supply no per-prompt id).
    base = derive_turn_key(source_kind="k", session_id="s", text="hello", cwd="/p")
    assert base == derive_turn_key(source_kind="k", session_id="s", text="hello", cwd="/p",
                                   prompt_id=None)
    assert base == derive_turn_key(source_kind="k", session_id="s", text="hello", cwd="/p",
                                   prompt_id="   ")


@pytest.mark.unit
def test_record_turn_evidence_stores_and_keys_on_prompt_id():
    fake = FakeNeo4j(routes=[("MERGE (t:TurnEvidence",
                              [{"turn_id": "tid", "created": True, "recorded_at": "2026-07-07T00:00:00Z"}])])
    TurnEvidenceRepository(fake).record_turn_evidence(
        text="I have 20 coins", source_kind="claude_code_hook", session_id="s1", cwd="/p",
        prompt_id="pid-A")
    _, params = fake.executed[-1]
    assert params["prompt_id"] == "pid-A"                    # stored on the node for provenance/correlation
    assert params["turn_key"] == derive_turn_key(           # idempotency keyed on prompt_id, not text
        source_kind="claude_code_hook", session_id="s1", text="I have 20 coins", cwd="/p",
        prompt_id="pid-A")


@pytest.mark.unit
def test_evidence_queries_filter_user_and_only_touch_evidence_nodes():
    fake = FakeNeo4j(default=[])
    repo = TurnEvidenceRepository(fake)
    repo.list_dirty_evidence_namespaces()
    repo.load_user_evidence("proj")
    for q, _ in fake.executed:
        assert ":TurnEvidence" in q and ":Episodic" not in q
        assert "t.role = 'user'" in q and "t.declarant = 'user'" in q


@pytest.mark.unit
def test_load_user_evidence_returns_raw_text():
    fake = FakeNeo4j(routes=[("MATCH (t:TurnEvidence {namespace:",
                              [{"uuid": "u1", "valid_at": "2026-07-07T00:00:00Z",
                                "content": "I have 25 movies on my watch list."}])])
    rows = TurnEvidenceRepository(fake).load_user_evidence("proj")
    assert rows[0]["content"] == "I have 25 movies on my watch list."  # raw, no 'user:' prefix
    query, _params = fake.executed[-1]
    assert "coalesce(t.occurred_at, t.recorded_at" in query
    assert "ORDER BY coalesce(t.occurred_at, t.recorded_at), t.recorded_at, t.turn_id" in query


@pytest.mark.unit
def test_scalar_loader_separates_world_time_from_processing_cursor():
    fake = FakeNeo4j(routes=[("OPTIONAL MATCH (w:ScalarConsolidationWatermark", [{
        "uuid": "turn-25",
        "valid_at": "2023-11-30T20:25:00Z",
        "cursor_at": "2026-07-29T13:00:00Z",
        "content": "I have added 25 postcards.",
    }])])

    rows = TurnEvidenceRepository(fake).load_next_scalar_evidence_batch(
        "proj", perceiver_version="v1",
    )

    assert rows[0]["valid_at"] == "2023-11-30T20:25:00Z"
    assert rows[0]["cursor_at"] == "2026-07-29T13:00:00Z"
    query, _params = fake.executed[-1]
    assert "coalesce(t.occurred_at, t.recorded_at)" in query
    assert "t.recorded_at AS ckey" in query


@pytest.mark.unit
def test_load_preceding_context_is_session_scoped_bounded_and_chronological():
    fake = FakeNeo4j(routes=[
        (
            "MATCH (current:TurnEvidence",
            [
                {
                    "role": "assistant",
                    "text": "Starting with 100 business cards sounds reasonable.",
                    "recorded_at": "2026-07-29T10:00:02Z",
                },
                {
                    "role": "user",
                    "text": "Should I order 100 or 200 business cards?",
                    "recorded_at": "2026-07-29T10:00:01Z",
                },
            ],
        )
    ])

    rows = TurnEvidenceRepository(fake).load_preceding_context("turn-current", limit=99)

    assert [row["role"] for row in rows] == ["user", "assistant"]
    query, params = fake.executed[-1]
    assert "prior.namespace = current.namespace" in query
    assert "prior.session_id = current.session_id" in query
    assert "prior.recorded_at < current.recorded_at" in query
    assert "prior.role IN ['user', 'assistant']" in query
    assert params == {"turn_id": "turn-current", "limit": 4}


@pytest.mark.unit
def test_evidence_exists_true_and_false():
    yes = FakeNeo4j(routes=[("RETURN count(t) AS c LIMIT 1", [{"c": 2}])])
    assert TurnEvidenceRepository(yes).evidence_exists() is True
    no = FakeNeo4j(routes=[("RETURN count(t) AS c LIMIT 1", [{"c": 0}])])
    assert TurnEvidenceRepository(no).evidence_exists() is False


@pytest.mark.unit
def test_evidence_stats_shape_with_triage():
    fake = FakeNeo4j(routes=[
        ("RETURN count(t) AS c", [{"c": 3}]),
        ("t.role AS role", [{"role": "user", "c": 3}]),
        ("t.source_kind AS sk", [{"sk": "claude_code_hook", "c": 3}]),
        ("t.triage_version AS v", [{"v": "claude-hook-v1", "c": 3}]),
        ("UNWIND t.triage_reason", [{"reason": "number", "c": 3}, {"reason": "i_have", "c": 2}]),
        ("max(t.recorded_at)", [{"latest": "2026-07-07T00:00:00Z"}]),
    ])
    stats = TurnEvidenceRepository(fake).evidence_stats()
    assert stats["turn_evidence_table_exists"] and stats["total_turn_evidence"] == 3
    assert stats["user_evidence"] == 3 and stats["claude_code_hook_evidence"] == 3
    assert stats["triage_reason_counts"]["number"] == 3
    assert stats["triage_version_counts"]["claude-hook-v1"] == 3


# ----------------------------------------------------------------------------- Phase 3 preference


class _StubEvidence:
    def __init__(self, exists, dirty=None, loaded=None):
        self._exists, self._dirty, self._loaded = exists, dirty or [], loaded or []
    def evidence_exists(self):
        return self._exists
    def list_dirty_evidence_namespaces(self, *, limit=200):
        return self._dirty
    def load_user_evidence(self, ns, *, limit=500):
        return self._loaded


class _StubLegacy:
    def list_dirty_namespaces(self, *, limit=200):
        return ["legacy-ns"]
    def load_user_episodes(self, ns, *, limit=500):
        return [{"uuid": "e0", "valid_at": "x", "content": "user: legacy"}]


@pytest.mark.unit
def test_phase3_prefers_evidence_when_present():
    adapter = MemoryGraphAdapter(FakeNeo4j())
    adapter._turn_evidence = _StubEvidence(exists=True, dirty=["proj"], loaded=[{"uuid": "u1", "content": "hi"}])
    adapter._personal_memory = _StubLegacy()
    assert adapter.list_dirty_namespaces() == ["proj"]
    assert adapter.load_user_episodes("proj") == [{"uuid": "u1", "content": "hi"}]


@pytest.mark.unit
def test_phase3_falls_back_to_legacy_without_evidence():
    adapter = MemoryGraphAdapter(FakeNeo4j())
    adapter._turn_evidence = _StubEvidence(exists=False)
    adapter._personal_memory = _StubLegacy()
    assert adapter.list_dirty_namespaces() == ["legacy-ns"]
    assert adapter.load_user_episodes("proj")[0]["content"] == "user: legacy"


# ----------------------------------------------------------------------------- triage (acceptance)


@pytest.mark.unit
def test_triage_drops_non_candidate_prompts():
    hook = _load_hook()
    for boring in ["Can you rewrite this?", "Explain this error.", "What do you think?",
                   "Make this shorter.", "Write the handoff.", "Continue."]:
        is_cand, reasons = hook.triage_user_prompt(boring)
        assert is_cand is False and reasons == []
        assert hook.build_evidence_payload({"prompt": boring, "cwd": "/x/proj"}) is None


@pytest.mark.unit
def test_triage_stores_number_prompt():
    hook = _load_hook()
    is_cand, reasons = hook.triage_user_prompt("I have 25 movies on my watch list.")
    assert is_cand is True
    assert "number" in reasons and "i_have" in reasons


@pytest.mark.unit
def test_triage_stores_preference_and_cessation():
    hook = _load_hook()
    is_cand, reasons = hook.triage_user_prompt("I no longer use soy sauce because it triggers me.")
    assert is_cand is True and "cessation" in reasons


@pytest.mark.unit
def test_triage_stores_decision():
    hook = _load_hook()
    is_cand, reasons = hook.triage_user_prompt("We decided to use Option B for Turn capture.")
    assert is_cand is True and "decision" in reasons


@pytest.mark.unit
def test_triage_is_deterministic_and_llm_free():
    hook = _load_hook()
    # pure regex/string rules: same input -> same output, no model client imported.
    r1 = hook.triage_user_prompt("The current threshold is 0.82.")
    r2 = hook.triage_user_prompt("The current threshold is 0.82.")
    assert r1 == r2 and r1[0] is True
    assert not hasattr(hook, "openai") and not hasattr(hook, "OpenAI")


@pytest.mark.unit
def test_hook_maps_candidate_to_user_evidence_payload(monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_hook()
    payload = hook.build_evidence_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "claude-abc",
        "transcript_path": "/tmp/t.jsonl", "cwd": "/home/u/IdeaProjects/menhir",
        "permission_mode": "default", "prompt": "I have 25 movies on my watch list.",
    })
    assert payload["role"] == "user" and payload["declarant"] == "user"
    assert payload["source_kind"] == "claude_code_hook" and payload["namespace"] == "menhir"
    assert "number" in payload["triage_reason"] and payload["triage_version"] == "claude-hook-v1"


@pytest.mark.unit
def test_hook_payload_carries_provenance(monkeypatch):
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_hook()
    payload = hook.build_evidence_payload({
        "session_id": "claude-abc", "cwd": "/home/u/IdeaProjects/menhir",
        "transcript_path": "/tmp/t.jsonl", "prompt": "I have 25 movies on my watch list.",
    })
    assert payload["source_client"] == hook.SOURCE_CLIENT == "claude_code"
    assert payload["hook_version"] == hook.HOOK_VERSION
    # prompt_hash is in the metadata envelope and matches the deterministic content fingerprint
    assert payload["metadata"]["prompt_hash"] == hook._prompt_hash("I have 25 movies on my watch list.")
    # provenance keys are always present (values may be None when git is unavailable)
    for key in ("project_root", "git_branch", "git_commit"):
        assert key in payload["metadata"]


@pytest.mark.unit
def test_hook_payload_carries_prompt_id_when_present_else_none(monkeypatch):
    # G18: the hook surfaces Claude Code's stable per-prompt id so the server can key idempotency on it.
    monkeypatch.delenv("MENHIR_TURN_NAMESPACE", raising=False)
    hook = _load_hook()
    with_id = hook.build_evidence_payload({
        "session_id": "claude-abc", "cwd": "/home/u/IdeaProjects/menhir",
        "transcript_path": "/tmp/t.jsonl", "prompt": "I have 25 movies on my watch list.",
        "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
    })
    assert with_id["prompt_id"] == "550e8400-e29b-41d4-a716-446655440000"
    # Older CC (< 2.1.196) / producers omit it -> None (server falls back to text-keying).
    without_id = hook.build_evidence_payload({
        "session_id": "claude-abc", "cwd": "/home/u/IdeaProjects/menhir",
        "transcript_path": "/tmp/t.jsonl", "prompt": "I have 25 movies on my watch list."})
    assert without_id["prompt_id"] is None


@pytest.mark.unit
def test_hook_git_provenance_is_fail_open(monkeypatch):
    # When git errors/absent, _git returns None and capture proceeds unchanged.
    hook = _load_hook()
    monkeypatch.setattr(hook, "_git", lambda args, cwd: None)
    payload = hook.build_evidence_payload({
        "session_id": "s", "cwd": "/not/a/repo", "prompt": "I bought one bike for $50."})
    assert payload is not None  # capture still happens
    assert payload["metadata"]["project_root"] is None
    assert payload["metadata"]["git_branch"] is None and payload["metadata"]["git_commit"] is None
    assert payload["metadata"]["prompt_hash"]  # hash still present


@pytest.mark.unit
def test_hook_junk_prompt_still_dropped_with_provenance(monkeypatch):
    # Provenance code must not change triage: a non-candidate returns None (nothing posted).
    hook = _load_hook()
    # even if git would work, junk short-circuits before provenance is gathered
    monkeypatch.setattr(hook, "_git", lambda args, cwd: "should-not-be-used")
    assert hook.build_evidence_payload({"prompt": "write the handoff", "cwd": "/x/proj"}) is None


@pytest.mark.unit
def test_hook_ignores_missing_or_blank_prompt():
    hook = _load_hook()
    assert hook.build_evidence_payload({"session_id": "x"}) is None
    assert hook.build_evidence_payload({"prompt": "   "}) is None


@pytest.mark.unit
def test_hook_does_not_block_when_menhir_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_TURN_HOOK_LOG", str(tmp_path / "hook.log"))
    hook = _load_hook()
    ok = hook.post_evidence({"text": "I have 25 things", "namespace": "proj", "session_id": "s",
                             "triage_reason": ["number"]},
                            url="http://127.0.0.1:9/turn-evidence", timeout=1.0)
    # `is None`, not `is False`: post_evidence returns the server's turn_id now, so failure is the
    # absence of an id rather than a boolean. Fail-open behaviour below is unchanged.
    assert ok is None
    entry = json.loads((tmp_path / "hook.log").read_text().strip())
    assert entry["prompt_len"] == len("I have 25 things") and "text" not in entry
