"""Memory type policy registry — each type defines its own behavioral contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.domain.models import FreshnessState, NodeScope


# Cross-cutting constant: nodes rehydrated this many times skip further compression.
REHYDRATION_EXEMPT_COUNT = 3


@dataclass(frozen=True)
class MemoryTypePolicy:
    """Behavioral contract for a memory type.

    Each field controls how the system treats memories of this type
    during decay, scoring, and lifecycle transitions.
    """

    name: str

    # --- Decay thresholds ---
    compress_days: float
    compress_sharpness: float
    compress_edge_count: int
    gone_days: float
    gone_sharpness: float
    gone_edge_count: int

    # If True, the type is completely exempt from decay (IDENTITY).
    decay_exempt: bool = False

    # --- Scoring parameters ---
    # Per-type recency decay rate (higher = recency matters more).
    scoring_recency_lambda: float = 0.1
    # Flat additive bonus applied during relevance scoring.
    # Currently 0.0 for all types — reserved for future per-type boosting.
    scoring_boost: float = 0.0

    # --- Lifecycle variant ---
    # When True, decay is anchored to a target_date field instead of
    # last_accessed staleness (used by TEMPORAL).
    uses_target_date: bool = False

    # ------------------------------------------------------------------
    # Decay decisions
    # ------------------------------------------------------------------

    def should_compress(self, node: dict[str, Any]) -> bool:
        """Determine if an ACTIVE node should transition to COMPRESSED.

        Unknown sharpness (None) is protective — missing uniqueness claim never
        justifies compression. Fail-safe: unknown stays ACTIVE.
        """
        if self.decay_exempt:
            return False
        if not self._is_decay_eligible(node, FreshnessState.ACTIVE):
            return False
        if int(node.get("rehydration_count", 0)) >= REHYDRATION_EXEMPT_COUNT:
            return False

        if self.uses_target_date:
            return self._target_date_compress(node)

        days = float(node.get("last_accessed_days_ago", 0))
        # Protective gate: missing sharpness is not evidence for compression
        sharpness_raw = node.get("sharpness")
        if sharpness_raw is None:
            return False
        sharpness = float(sharpness_raw)
        edge_count = int(node.get("edge_count", 0))

        return (
            days > self.compress_days
            and sharpness < self.compress_sharpness
            and edge_count < self.compress_edge_count
        )

    def should_delete(self, node: dict[str, Any]) -> bool:
        """Determine if a COMPRESSED node should transition to GONE.

        Unknown sharpness (None) is protective — missing uniqueness claim never
        justifies deletion. Fail-safe: unknown stays COMPRESSED.
        """
        if self.decay_exempt:
            return False
        if not self._is_decay_eligible(node, FreshnessState.COMPRESSED):
            return False

        if self.uses_target_date:
            return self._target_date_delete(node)

        days = float(node.get("last_accessed_days_ago", 0))
        # Protective gate: missing sharpness is not evidence for deletion
        sharpness_raw = node.get("sharpness")
        if sharpness_raw is None:
            return False
        sharpness = float(sharpness_raw)
        edge_count = int(node.get("edge_count", 0))

        return (
            days > self.gone_days
            and sharpness < self.gone_sharpness
            and edge_count < self.gone_edge_count
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_decay_eligible(node: dict[str, Any], required_freshness: str) -> bool:
        """Common pre-checks shared by compress and delete paths."""
        if str(node.get("scope")) != NodeScope.PERSISTENT:
            return False
        if bool(node.get("user_flagged", False)):
            return False
        if str(node.get("freshness")) != required_freshness:
            return False
        return True

    def _target_date_compress(self, node: dict[str, Any]) -> bool:
        """TEMPORAL: compress once the target date has passed and the node is idle."""
        target_date_passed = bool(node.get("target_date_passed", False))
        if not target_date_passed:
            return False
        # After the target date, use a shorter idle window.
        days = float(node.get("last_accessed_days_ago", 0))
        return days > self.compress_days

    def _target_date_delete(self, node: dict[str, Any]) -> bool:
        """TEMPORAL: delete after target date + gone window of inactivity."""
        target_date_passed = bool(node.get("target_date_passed", False))
        if not target_date_passed:
            return False
        days = float(node.get("last_accessed_days_ago", 0))
        return days > self.gone_days


# ---------------------------------------------------------------------------
# Policy registry
# ---------------------------------------------------------------------------

MEMORY_TYPE_POLICIES: dict[str, MemoryTypePolicy] = {
    "EPISODIC": MemoryTypePolicy(
        name="EPISODIC",
        compress_days=30,
        compress_sharpness=0.3,
        compress_edge_count=5,
        gone_days=90,
        gone_sharpness=0.1,
        gone_edge_count=3,
    ),
    "SEMANTIC": MemoryTypePolicy(
        name="SEMANTIC",
        compress_days=30,
        compress_sharpness=0.3,
        compress_edge_count=5,
        gone_days=90,
        gone_sharpness=0.1,
        gone_edge_count=3,
    ),
    "PROCEDURAL": MemoryTypePolicy(
        name="PROCEDURAL",
        compress_days=45,
        compress_sharpness=0.25,
        compress_edge_count=4,
        gone_days=120,
        gone_sharpness=0.1,
        gone_edge_count=3,
        scoring_recency_lambda=0.05,
    ),
    "PREFERENCE": MemoryTypePolicy(
        name="PREFERENCE",
        compress_days=60,
        compress_sharpness=0.2,
        compress_edge_count=3,
        gone_days=180,
        gone_sharpness=0.1,
        gone_edge_count=2,
        scoring_recency_lambda=0.05,
    ),
    "IDENTITY": MemoryTypePolicy(
        name="IDENTITY",
        compress_days=0,
        compress_sharpness=0,
        compress_edge_count=0,
        gone_days=0,
        gone_sharpness=0,
        gone_edge_count=0,
        decay_exempt=True,
    ),
    "TEMPORAL": MemoryTypePolicy(
        name="TEMPORAL",
        compress_days=7,
        compress_sharpness=0.3,
        compress_edge_count=5,
        gone_days=30,
        gone_sharpness=0.1,
        gone_edge_count=3,
        uses_target_date=True,
    ),
    "SPATIAL": MemoryTypePolicy(
        name="SPATIAL",
        compress_days=90,
        compress_sharpness=0.2,
        compress_edge_count=4,
        gone_days=365,
        gone_sharpness=0.1,
        gone_edge_count=2,
        scoring_recency_lambda=0.03,
    ),
}

_DEFAULT_POLICY = MEMORY_TYPE_POLICIES["SEMANTIC"]


def get_policy(type_name: str) -> MemoryTypePolicy:
    """Look up policy by type name, falling back to SEMANTIC for unknown types."""
    return MEMORY_TYPE_POLICIES.get(type_name, _DEFAULT_POLICY)
