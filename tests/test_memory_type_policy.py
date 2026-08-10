"""Unit tests for MemoryTypePolicy contract and registry."""

from __future__ import annotations

import pytest

from menhir.domain.memory_types import (
    MEMORY_TYPE_POLICIES,
    MemoryTypePolicy,
    REHYDRATION_EXEMPT_COUNT,
    get_policy,
)
from menhir.domain.models import MemoryType


pytestmark = pytest.mark.unit


# --- Registry completeness ---

def test_every_enum_value_has_a_policy():
    """Each MemoryType enum member must have a corresponding policy."""
    for mt in MemoryType:
        assert mt.value in MEMORY_TYPE_POLICIES, f"Missing policy for {mt.value}"


def test_policy_names_match_keys():
    for key, policy in MEMORY_TYPE_POLICIES.items():
        assert policy.name == key


def test_get_policy_returns_correct_policy():
    for name in MEMORY_TYPE_POLICIES:
        assert get_policy(name).name == name


def test_get_policy_unknown_falls_back_to_semantic():
    assert get_policy("BOGUS").name == "SEMANTIC"
    assert get_policy("").name == "SEMANTIC"


# --- Policy is frozen ---

def test_policy_is_immutable():
    policy = get_policy("SEMANTIC")
    with pytest.raises(AttributeError):
        policy.compress_days = 999


# --- IDENTITY decay exemption ---

def test_identity_is_decay_exempt():
    assert get_policy("IDENTITY").decay_exempt is True


def test_non_identity_types_are_not_exempt():
    for name, policy in MEMORY_TYPE_POLICIES.items():
        if name == "IDENTITY":
            continue
        assert policy.decay_exempt is False, f"{name} should not be decay_exempt"


# --- TEMPORAL uses target_date ---

def test_temporal_uses_target_date():
    assert get_policy("TEMPORAL").uses_target_date is True


def test_non_temporal_types_do_not_use_target_date():
    for name, policy in MEMORY_TYPE_POLICIES.items():
        if name == "TEMPORAL":
            continue
        assert policy.uses_target_date is False, f"{name} should not use target_date"


# --- Scoring parameters ---

def test_procedural_has_slower_recency_decay():
    semantic = get_policy("SEMANTIC")
    procedural = get_policy("PROCEDURAL")
    assert procedural.scoring_recency_lambda < semantic.scoring_recency_lambda


def test_spatial_has_slowest_recency_decay():
    spatial = get_policy("SPATIAL")
    for name, policy in MEMORY_TYPE_POLICIES.items():
        if name == "SPATIAL":
            continue
        assert spatial.scoring_recency_lambda <= policy.scoring_recency_lambda


def test_default_scoring_boost_is_zero():
    """All current policies have zero scoring_boost (reserved for future use)."""
    for policy in MEMORY_TYPE_POLICIES.values():
        assert policy.scoring_boost == 0.0


# --- should_compress method ---

def _base_active_node(**overrides) -> dict:
    node = {
        "uuid": "test",
        "type": "SEMANTIC",
        "scope": "PERSISTENT",
        "freshness": "ACTIVE",
        "last_accessed_days_ago": 60,
        "sharpness": 0.1,
        "edge_count": 2,
        "user_flagged": False,
        "rehydration_count": 0,
    }
    node.update(overrides)
    return node


def _base_compressed_node(**overrides) -> dict:
    node = _base_active_node(**overrides)
    node["freshness"] = "COMPRESSED"
    return node


def test_should_compress_respects_scope():
    policy = get_policy("SEMANTIC")
    for scope in ("SESSION", "PROMOTED"):
        node = _base_active_node(scope=scope)
        assert policy.should_compress(node) is False


def test_should_compress_respects_user_flagged():
    policy = get_policy("SEMANTIC")
    node = _base_active_node(user_flagged=True)
    assert policy.should_compress(node) is False


def test_should_compress_respects_rehydration_count():
    policy = get_policy("SEMANTIC")
    node = _base_active_node(rehydration_count=REHYDRATION_EXEMPT_COUNT)
    assert policy.should_compress(node) is False


def test_should_delete_respects_scope():
    policy = get_policy("SEMANTIC")
    node = _base_compressed_node(scope="PROMOTED", last_accessed_days_ago=999)
    assert policy.should_delete(node) is False


def test_should_delete_respects_user_flagged():
    policy = get_policy("SEMANTIC")
    node = _base_compressed_node(user_flagged=True, last_accessed_days_ago=999)
    assert policy.should_delete(node) is False


# --- TEMPORAL target_date decay paths ---

def _temporal_active_node(**overrides) -> dict:
    node = {
        "uuid": "temporal-test",
        "type": "TEMPORAL",
        "scope": "PERSISTENT",
        "freshness": "ACTIVE",
        "last_accessed_days_ago": 14,
        "sharpness": 0.1,
        "edge_count": 2,
        "user_flagged": False,
        "rehydration_count": 0,
        "target_date_passed": True,
    }
    node.update(overrides)
    return node


def _temporal_compressed_node(**overrides) -> dict:
    node = _temporal_active_node(**overrides)
    node["freshness"] = "COMPRESSED"
    return node


class TestTemporalTargetDateCompress:
    """TEMPORAL compress uses target_date_passed + idle window, not sharpness/edge_count."""

    policy = get_policy("TEMPORAL")

    def test_compress_when_target_date_passed_and_idle(self):
        node = _temporal_active_node(last_accessed_days_ago=14)
        assert self.policy.should_compress(node) is True

    def test_no_compress_when_target_date_not_passed(self):
        node = _temporal_active_node(target_date_passed=False, last_accessed_days_ago=999)
        assert self.policy.should_compress(node) is False

    def test_no_compress_when_idle_below_threshold(self):
        node = _temporal_active_node(last_accessed_days_ago=3)
        assert self.policy.should_compress(node) is False

    def test_compress_ignores_sharpness(self):
        """TEMPORAL compress doesn't check sharpness — only target_date + idle days."""
        node = _temporal_active_node(sharpness=0.99, last_accessed_days_ago=14)
        assert self.policy.should_compress(node) is True

    def test_compress_ignores_edge_count(self):
        """TEMPORAL compress doesn't check edge_count — only target_date + idle days."""
        node = _temporal_active_node(edge_count=100, last_accessed_days_ago=14)
        assert self.policy.should_compress(node) is True

    def test_compress_respects_rehydration_exempt(self):
        node = _temporal_active_node(rehydration_count=REHYDRATION_EXEMPT_COUNT)
        assert self.policy.should_compress(node) is False

    def test_compress_respects_user_flagged(self):
        node = _temporal_active_node(user_flagged=True)
        assert self.policy.should_compress(node) is False


class TestTemporalTargetDateDelete:
    """TEMPORAL delete uses target_date_passed + gone window of inactivity."""

    policy = get_policy("TEMPORAL")

    def test_delete_when_target_date_passed_and_long_idle(self):
        node = _temporal_compressed_node(last_accessed_days_ago=60)
        assert self.policy.should_delete(node) is True

    def test_no_delete_when_target_date_not_passed(self):
        node = _temporal_compressed_node(target_date_passed=False, last_accessed_days_ago=999)
        assert self.policy.should_delete(node) is False

    def test_no_delete_when_idle_below_gone_threshold(self):
        node = _temporal_compressed_node(last_accessed_days_ago=15)
        assert self.policy.should_delete(node) is False

    def test_delete_ignores_sharpness(self):
        node = _temporal_compressed_node(sharpness=0.99, last_accessed_days_ago=60)
        assert self.policy.should_delete(node) is True

    def test_delete_ignores_edge_count(self):
        node = _temporal_compressed_node(edge_count=100, last_accessed_days_ago=60)
        assert self.policy.should_delete(node) is True

    def test_delete_at_exact_boundary(self):
        """Exactly at gone_days should NOT delete (requires strictly greater)."""
        node = _temporal_compressed_node(last_accessed_days_ago=30)
        assert self.policy.should_delete(node) is False

    def test_delete_just_past_boundary(self):
        node = _temporal_compressed_node(last_accessed_days_ago=31)
        assert self.policy.should_delete(node) is True


# --- Decay ordering: types with longer windows are harder to compress ---

def test_decay_ordering():
    """Verify the intended decay speed ordering across types."""
    episodic = get_policy("EPISODIC")
    semantic = get_policy("SEMANTIC")
    procedural = get_policy("PROCEDURAL")
    preference = get_policy("PREFERENCE")
    spatial = get_policy("SPATIAL")

    assert episodic.compress_days == semantic.compress_days
    assert procedural.compress_days > semantic.compress_days
    assert preference.compress_days > procedural.compress_days
    assert spatial.compress_days > preference.compress_days
