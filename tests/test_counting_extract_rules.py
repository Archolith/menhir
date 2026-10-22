"""Counting extraction rules (span rule "+s", captured states "+c"): both flags default off and are
prompt-only versioned changes. Offline: pure functions and a fake Neo4j only."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.config import MemorySettings
from menhir.config.feature_flags import FEATURES
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.services.counting_consolidation import (
    COUNTING_CAPTURED_STATES_ADDENDUM,
    COUNTING_MATCH_PROMPT_ADDENDUM,
    COUNTING_SPAN_RULE_ADDENDUM,
    COUNTING_SYSTEM_PROMPT,
    COUNTING_SYSTEM_PROMPT_V2,
    CountingConsolidationConfig,
    build_counting_context,
    counting_system_prompt,
    effective_counting_perceiver_version,
)

SPAN = "personal_memory_counting_span_rule_enabled"
CAPT = "personal_memory_counting_captured_states_enabled"


@dataclass
class _Ep:
    uuid: str
    content: str


def _claims():
    return [{
        "episode_uuid": "e0", "subject": "user", "attribute": "repair_status",
        "operation": "absolute", "value_kind": "status", "value_json": json.dumps("fixed"),
        "stated_span": "fixed the leg",
    }, {
        "episode_uuid": "e0", "subject": "user", "attribute": "table_count",
        "operation": "absolute", "value_json": "3", "stated_span": "3 tables",
    }]


# ----------------------------------------------------------------------- flags off: unchanged

@pytest.mark.unit
def test_defaults_off_prompt_version_and_context_unchanged():
    assert counting_system_prompt(False) == COUNTING_SYSTEM_PROMPT
    assert counting_system_prompt(True) == COUNTING_SYSTEM_PROMPT_V2
    assert counting_system_prompt(False, True) == COUNTING_SYSTEM_PROMPT + COUNTING_MATCH_PROMPT_ADDENDUM
    assert counting_system_prompt(True, True) == (
        COUNTING_SYSTEM_PROMPT_V2 + COUNTING_MATCH_PROMPT_ADDENDUM)
    assert effective_counting_perceiver_version("v1", False) == "v1"
    assert effective_counting_perceiver_version("v1", True) == "v1+x2"
    assert effective_counting_perceiver_version("v1", False, True) == "v1+m"
    assert effective_counting_perceiver_version("v1", True, True) == "v1+x2+m"
    config = CountingConsolidationConfig()
    assert config.span_rule is False and config.captured_states is False
    episodes = [_Ep("e0", "I fixed the leg")]
    assert build_counting_context(episodes, _claims()) == build_counting_context(
        episodes, _claims(), kinds=False)
    assert "kind=" not in build_counting_context(episodes, _claims())
    assert "kind=" not in build_counting_context(episodes, _claims(), fenced=True)


# ----------------------------------------------------------------------- prompt addenda

@pytest.mark.unit
def test_each_flag_adds_exactly_its_addendum():
    base = COUNTING_SYSTEM_PROMPT_V2 + COUNTING_MATCH_PROMPT_ADDENDUM
    assert counting_system_prompt(True, True, span_rule=True) == base + COUNTING_SPAN_RULE_ADDENDUM
    assert counting_system_prompt(True, True, captured_states=True) == (
        base + COUNTING_CAPTURED_STATES_ADDENDUM)
    assert counting_system_prompt(True, True, True, True) == (
        base + COUNTING_SPAN_RULE_ADDENDUM + COUNTING_CAPTURED_STATES_ADDENDUM)
    assert counting_system_prompt(False, span_rule=True) == (
        COUNTING_SYSTEM_PROMPT + COUNTING_SPAN_RULE_ADDENDUM)
    assert counting_system_prompt(False, captured_states=True) == (
        COUNTING_SYSTEM_PROMPT + COUNTING_CAPTURED_STATES_ADDENDUM)


@pytest.mark.unit
def test_addenda_are_ascii_and_generic():
    for text in (COUNTING_SPAN_RULE_ADDENDUM, COUNTING_CAPTURED_STATES_ADDENDUM):
        text.encode("ascii")
        assert "plant" not in text.lower() and "furniture" not in text.lower()
    assert "along with" in COUNTING_SPAN_RULE_ADDENDUM
    assert "kind=count" in COUNTING_CAPTURED_STATES_ADDENDUM


# ----------------------------------------------------------------------- version suffixes

@pytest.mark.unit
def test_version_suffixes_and_order():
    f = effective_counting_perceiver_version
    assert f("v1", False, span_rule=True) == "v1+s"
    assert f("v1", False, captured_states=True) == "v1+c"
    assert f("v1", False, span_rule=True, captured_states=True) == "v1+s+c"
    assert f("v1", True, span_rule=True) == "v1+x2+s"
    assert f("v1", True, True) == "v1+x2+m"
    assert f("v1", True, True, True, True) == "v1+x2+s+c+m"
    assert f("v1", False, True, False, True) == "v1+c+m"


@pytest.mark.unit
@pytest.mark.parametrize("extract_v2", [False, True])
@pytest.mark.parametrize("span_rule", [False, True])
@pytest.mark.parametrize("captured_states", [False, True])
@pytest.mark.parametrize("match", [False, True])
def test_match_stamp_detected_under_every_flag_combination(extract_v2, span_rule, captured_states,
                                                           match):
    # Required because status_of / _counted detect a match-stamped row by a "+m" ending; a suffix
    # after "+m" would make an undecided match row read as counted under a pre-match version.
    from menhir.domain.count_match import COUNTED, UNCERTAIN, status_of
    version = effective_counting_perceiver_version(
        "v1", extract_v2, match, span_rule=span_rule, captured_states=captured_states)
    row = {"count_status": None, "perceiver_version": version}
    assert status_of(row) == (UNCERTAIN if match else COUNTED)


# ----------------------------------------------------------------------- context kinds

@pytest.mark.unit
def test_kinds_rendered_only_when_requested():
    episodes = [_Ep("e0", "I fixed the leg")]
    plain = build_counting_context(episodes, _claims())
    with_kinds = build_counting_context(episodes, _claims(), kinds=True)
    assert "kind=" not in plain
    assert "operation=absolute kind=status value=fixed" in with_kinds
    # a claim with no value_kind renders an empty kind
    assert "operation=absolute kind= value=3" in with_kinds
    assert with_kinds.replace(" kind=status", "").replace(" kind=", "") == plain


@pytest.mark.unit
def test_kinds_rendered_in_fenced_block():
    episodes = [_Ep("e0", "I fixed the leg")]
    out = build_counting_context(
        episodes, _claims(), fenced=True, nonce_fn=lambda: "abc123", kinds=True)
    assert out.startswith("<<<CAPTURED-abc123>>>\n")
    assert out.endswith("<<<END-CAPTURED-abc123>>>")
    assert "operation=absolute kind=status value=fixed" in out
    plain = build_counting_context(
        episodes, _claims(), fenced=True, nonce_fn=lambda: "abc123")
    assert "kind=" not in plain


# ----------------------------------------------------------------------- registry + settings

@pytest.mark.unit
@pytest.mark.parametrize("name", [SPAN, CAPT])
def test_feature_registry_versioned_and_requires_counting(name):
    flag = FEATURES[name]
    assert flag.version_bump is True
    assert flag.default is False
    assert flag.requires == ("personal_memory_counting_enabled",)
    assert flag.requirement_scope


@pytest.mark.unit
@pytest.mark.parametrize("name,env", [
    (SPAN, "MENHIR_PERSONAL_MEMORY_COUNTING_SPAN_RULE_ENABLED"),
    (CAPT, "MENHIR_PERSONAL_MEMORY_COUNTING_CAPTURED_STATES_ENABLED"),
])
def test_settings_default_false_and_parse_env(monkeypatch, name, env):
    assert FEATURES[name].env_var == env
    monkeypatch.delenv(env, raising=False)
    assert getattr(MemorySettings(), name) is False
    assert getattr(MemorySettings.from_env(), name) is False
    monkeypatch.setenv(env, "true")
    assert getattr(MemorySettings.from_env(), name) is True
    monkeypatch.setenv(env, "false")
    assert getattr(MemorySettings.from_env(), name) is False


# ----------------------------------------------------------------------- claims query

class _FakeNeo4j:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return [{"episode_uuid": "t1", "value_kind": "status"}]


@pytest.mark.unit
def test_scalar_claims_for_turns_returns_value_kind():
    neo4j = _FakeNeo4j()
    repo = TurnEvidenceRepository(neo4j)
    rows = repo.scalar_claims_for_turns("ns", ["t1"], limit=10)
    assert rows[0]["value_kind"] == "status"
    query = neo4j.calls[0][0]
    assert "a.value_kind AS value_kind" in query
    assert "ORDER BY a.episode_uuid, a.span_start, a.attribute" in query
