"""Unit tests for M4 decay decision logic.

Tests cover the static decision methods (should_compress, should_delete)
and the DecayResult dataclass. The actual decay job implementation is tested
separately once built.
"""

from __future__ import annotations

import pytest

from menhir.domain.memory_types import MEMORY_TYPE_POLICIES, get_policy
from menhir.services.lifecycle_service import (
    DecayResult,
    LifecycleService,
)


pytestmark = pytest.mark.unit

# Default SEMANTIC policy thresholds (used by most tests)
_SEMANTIC = MEMORY_TYPE_POLICIES["SEMANTIC"]
DECAY_COMPRESS_DAYS = _SEMANTIC.compress_days
DECAY_COMPRESS_SHARPNESS = _SEMANTIC.compress_sharpness
DECAY_COMPRESS_EDGE_COUNT = _SEMANTIC.compress_edge_count
DECAY_GONE_DAYS = _SEMANTIC.gone_days
DECAY_GONE_SHARPNESS = _SEMANTIC.gone_sharpness
DECAY_GONE_EDGE_COUNT = _SEMANTIC.gone_edge_count


def _policy_should_delete(node: dict) -> bool:
    """Exercise the type policy's delete thresholds directly. The LifecycleService wrapper is
    hotfix-disabled (2026-07-03, GONE disarmed while sharpness is an RRF rank artifact) but the
    policy logic stays tested so re-enabling is a one-line revert with coverage intact."""
    return get_policy(str(node.get("type") or "SEMANTIC")).should_delete(node)


def test_service_should_delete_disarmed_by_hotfix():
    # HOTFIX 2026-07-03 pin: the service wrapper never deletes, no matter how eligible the node
    # looks to its type policy (see reviews/menhir-lifecycle-scale-probe-2026-07-03.md).
    node = _compressed_node(days_ago=999, sharpness=0.0, edge_count=0)
    assert _policy_should_delete(node) is True          # the policy would delete...
    assert LifecycleService.should_delete(node) is False  # ...the disarmed wrapper never does


def _active_node(
    uuid: str = "n1",
    *,
    days_ago: float = 0,
    sharpness: float = 0.0,
    edge_count: int = 0,
    flagged: bool = False,
    scope: str = "PERSISTENT",
    memory_type: str = "SEMANTIC",
) -> dict:
    return {
        "uuid": uuid,
        "type": memory_type,
        "scope": scope,
        "freshness": "ACTIVE",
        "last_accessed_days_ago": days_ago,
        "sharpness": sharpness,
        "edge_count": edge_count,
        "user_flagged": flagged,
    }


def _compressed_node(
    uuid: str = "n1",
    *,
    days_ago: float = 0,
    sharpness: float = 0.0,
    edge_count: int = 0,
    flagged: bool = False,
    scope: str = "PERSISTENT",
    memory_type: str = "SEMANTIC",
) -> dict:
    return {
        "uuid": uuid,
        "type": memory_type,
        "scope": scope,
        "freshness": "COMPRESSED",
        "last_accessed_days_ago": days_ago,
        "sharpness": sharpness,
        "edge_count": edge_count,
        "user_flagged": flagged,
    }


# --- Constants match design doc ---

def test_compress_thresholds_match_design():
    assert DECAY_COMPRESS_DAYS == 30
    assert DECAY_COMPRESS_SHARPNESS == 0.3
    assert DECAY_COMPRESS_EDGE_COUNT == 5


def test_gone_thresholds_match_design():
    assert DECAY_GONE_DAYS == 90
    assert DECAY_GONE_SHARPNESS == 0.1
    assert DECAY_GONE_EDGE_COUNT == 3  # raised from 2 to enable bridging (A->B->C)


# --- should_compress: positive cases ---

def test_compress_old_low_sharpness_low_edges():
    node = _active_node(days_ago=31, sharpness=0.1, edge_count=2)
    assert LifecycleService.should_compress(node) is True


def test_compress_exactly_at_boundary_is_false():
    """All thresholds are strict inequalities -- exact boundary should NOT compress."""
    node = _active_node(
        days_ago=DECAY_COMPRESS_DAYS,
        sharpness=DECAY_COMPRESS_SHARPNESS,
        edge_count=DECAY_COMPRESS_EDGE_COUNT,
    )
    assert LifecycleService.should_compress(node) is False


def test_compress_just_past_boundary():
    node = _active_node(
        days_ago=DECAY_COMPRESS_DAYS + 0.01,
        sharpness=DECAY_COMPRESS_SHARPNESS - 0.01,
        edge_count=DECAY_COMPRESS_EDGE_COUNT - 1,
    )
    assert LifecycleService.should_compress(node) is True


# --- should_compress: negative cases (exemptions) ---

def test_compress_skips_promoted_node():
    node = _active_node(days_ago=365, sharpness=0.0, edge_count=0, scope="PROMOTED")
    assert LifecycleService.should_compress(node) is False


def test_compress_skips_flagged_node():
    node = _active_node(days_ago=365, sharpness=0.0, edge_count=0, flagged=True)
    assert LifecycleService.should_compress(node) is False


def test_compress_skips_already_compressed():
    node = _compressed_node(days_ago=365, sharpness=0.0, edge_count=0)
    assert LifecycleService.should_compress(node) is False


def test_compress_skips_session_scope():
    node = _active_node(days_ago=365, sharpness=0.0, edge_count=0, scope="SESSION")
    # SESSION nodes go through consolidation, not decay
    assert LifecycleService.should_compress(node) is False


# --- should_compress: individual threshold gates ---

def test_compress_blocked_by_recent_access():
    node = _active_node(days_ago=10, sharpness=0.0, edge_count=0)
    assert LifecycleService.should_compress(node) is False


def test_compress_blocked_by_high_sharpness():
    node = _active_node(days_ago=60, sharpness=0.5, edge_count=0)
    assert LifecycleService.should_compress(node) is False


def test_compress_blocked_by_high_edge_count():
    """High edge_count (prominence) acts as a brake against decay."""
    node = _active_node(days_ago=60, sharpness=0.1, edge_count=10)
    assert LifecycleService.should_compress(node) is False


def test_compress_blocked_by_edge_count_at_threshold():
    node = _active_node(days_ago=60, sharpness=0.1, edge_count=DECAY_COMPRESS_EDGE_COUNT)
    assert LifecycleService.should_compress(node) is False


# --- should_delete: positive cases ---

def test_delete_old_compressed_low_signals():
    node = _compressed_node(days_ago=91, sharpness=0.05, edge_count=1)
    assert _policy_should_delete(node) is True


def test_delete_exactly_at_boundary_is_false():
    node = _compressed_node(
        days_ago=DECAY_GONE_DAYS,
        sharpness=DECAY_GONE_SHARPNESS,
        edge_count=DECAY_GONE_EDGE_COUNT,
    )
    assert _policy_should_delete(node) is False


def test_delete_just_past_boundary():
    node = _compressed_node(
        days_ago=DECAY_GONE_DAYS + 0.01,
        sharpness=DECAY_GONE_SHARPNESS - 0.01,
        edge_count=DECAY_GONE_EDGE_COUNT - 1,
    )
    assert _policy_should_delete(node) is True


# --- should_delete: negative cases (exemptions) ---

def test_delete_skips_promoted():
    node = _compressed_node(days_ago=365, sharpness=0.0, edge_count=0, scope="PROMOTED")
    assert _policy_should_delete(node) is False


def test_delete_skips_flagged():
    node = _compressed_node(days_ago=365, sharpness=0.0, edge_count=0, flagged=True)
    assert _policy_should_delete(node) is False


def test_delete_skips_active_nodes():
    """Only COMPRESSED nodes are eligible for deletion."""
    node = _active_node(days_ago=365, sharpness=0.0, edge_count=0)
    assert _policy_should_delete(node) is False


# --- should_delete: individual threshold gates ---

def test_delete_blocked_by_recent_access():
    node = _compressed_node(days_ago=45, sharpness=0.0, edge_count=0)
    assert _policy_should_delete(node) is False


def test_delete_blocked_by_sharpness():
    node = _compressed_node(days_ago=120, sharpness=0.2, edge_count=0)
    assert _policy_should_delete(node) is False


def test_delete_blocked_by_edge_count():
    node = _compressed_node(days_ago=120, sharpness=0.0, edge_count=5)
    assert _policy_should_delete(node) is False


def test_delete_blocked_by_edge_count_at_threshold():
    node = _compressed_node(days_ago=120, sharpness=0.0, edge_count=DECAY_GONE_EDGE_COUNT)
    assert _policy_should_delete(node) is False


# --- DecayResult ---

def test_decay_result_fields():
    r = DecayResult(
        edge_counts_synced=10,
        sharpness_recalculated=8,
        compressed=3,
        deleted=1,
        edges_bridged=2,
        orphan_subgraphs_cleaned=0,
    )
    assert r.edge_counts_synced == 10
    assert r.sharpness_recalculated == 8
    assert r.compressed == 3
    assert r.deleted == 1
    assert r.edges_bridged == 2
    assert r.orphan_subgraphs_cleaned == 0


def test_decay_result_is_frozen():
    r = DecayResult(0, 0, 0, 0, 0, 0)
    with pytest.raises(AttributeError):
        r.compressed = 5


# --- Idempotency principle ---

def test_compress_decision_is_idempotent():
    """Calling should_compress twice on the same node gives the same answer."""
    node = _active_node(days_ago=60, sharpness=0.1, edge_count=2)
    assert LifecycleService.should_compress(node) == LifecycleService.should_compress(node)


def test_delete_decision_is_idempotent():
    node = _compressed_node(days_ago=120, sharpness=0.05, edge_count=0)
    assert _policy_should_delete(node) == _policy_should_delete(node)


# --- Cross-state: a node cannot be both compress and delete eligible ---

def test_active_node_never_eligible_for_delete():
    """ACTIVE nodes must compress first, never skip to GONE."""
    node = _active_node(days_ago=365, sharpness=0.0, edge_count=0)
    assert LifecycleService.should_compress(node) is True
    assert _policy_should_delete(node) is False


def test_compressed_node_never_eligible_for_compress():
    """COMPRESSED nodes can only go to GONE, not re-compress."""
    node = _compressed_node(days_ago=365, sharpness=0.0, edge_count=0)
    assert LifecycleService.should_compress(node) is False
    assert _policy_should_delete(node) is True


# --- Type-specific decay behavior ---

def test_identity_never_compresses():
    node = _active_node(days_ago=9999, sharpness=0.0, edge_count=0, memory_type="IDENTITY")
    assert LifecycleService.should_compress(node) is False


def test_identity_never_deletes():
    node = _compressed_node(days_ago=9999, sharpness=0.0, edge_count=0, memory_type="IDENTITY")
    assert _policy_should_delete(node) is False


def test_procedural_decays_slower_than_semantic():
    """PROCEDURAL has higher compress_days than SEMANTIC."""
    procedural = get_policy("PROCEDURAL")
    semantic = get_policy("SEMANTIC")
    assert procedural.compress_days > semantic.compress_days
    assert procedural.gone_days > semantic.gone_days


def test_spatial_decays_slowest_of_standard_types():
    """SPATIAL has the longest compress window of non-exempt types."""
    spatial = get_policy("SPATIAL")
    for name, policy in MEMORY_TYPE_POLICIES.items():
        if policy.decay_exempt or name == "SPATIAL":
            continue
        assert spatial.compress_days >= policy.compress_days


def test_preference_decays_slower_than_episodic():
    pref = get_policy("PREFERENCE")
    episodic = get_policy("EPISODIC")
    assert pref.compress_days > episodic.compress_days


def test_temporal_requires_target_date_for_compress():
    """TEMPORAL nodes only compress after their target date passes."""
    # Before target date: should not compress even if very old
    node = _active_node(
        days_ago=999, sharpness=0.0, edge_count=0, memory_type="TEMPORAL",
    )
    node["target_date_passed"] = False
    assert LifecycleService.should_compress(node) is False

    # After target date + idle window: should compress
    node["target_date_passed"] = True
    node["last_accessed_days_ago"] = 8  # > TEMPORAL compress_days (7)
    assert LifecycleService.should_compress(node) is True


def test_temporal_requires_target_date_for_delete():
    node = _compressed_node(
        days_ago=999, sharpness=0.0, edge_count=0, memory_type="TEMPORAL",
    )
    node["target_date_passed"] = False
    assert _policy_should_delete(node) is False

    node["target_date_passed"] = True
    node["last_accessed_days_ago"] = 31  # > TEMPORAL gone_days (30)
    assert _policy_should_delete(node) is True


def test_unknown_type_falls_back_to_semantic():
    """Unknown type names get SEMANTIC policy."""
    policy = get_policy("NONEXISTENT")
    semantic = get_policy("SEMANTIC")
    assert policy is semantic


def test_fetch_decay_candidates_projects_all_lifecycle_policy_inputs():
    """The decay projection must expose every field MemoryTypePolicy reads.

    Regression guard for the 2026-07-13 hotfix: `type`, `rehydration_count`, and
    `target_date_passed` were previously omitted, so every candidate was evaluated
    under the SEMANTIC fallback and the rehydration-exempt protection never fired.
    """
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    captured: dict[str, str] = {}

    class _CaptureNeo4j:
        def execute(self, query, params=None):
            captured["query"] = query
            return []

    repo = ConsolidationRepository(neo4j=_CaptureNeo4j())
    repo.fetch_decay_candidates(
        "ACTIVE", min_days_since_accessed=30, max_edge_count=5
    )

    query = captured["query"]
    required_aliases = (
        "AS type",
        "AS rehydration_count",
        "AS target_date_passed",
        "AS scope",
        "AS freshness",
        "AS user_flagged",
        "AS sharpness",
        "AS edge_count",
        "AS last_accessed",
    )
    for alias in required_aliases:
        assert alias in query, f"decay projection is missing '{alias}'"
