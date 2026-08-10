"""Milestone 0 scope lock and success metric contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MilestoneZeroScope:
    """Contract for what v1 ships in phase zero/one."""

    frozen_features: tuple[str, ...] = (
        "node_edge_model",
        "two_phase_retrieval",
        "v1_lifecycle",
        "session_consolidation",
        "conflict_handling",
        "explainability_payload",
    )

    deferred_features: tuple[str, ...] = (
        "confidence_weighted_emotions",
        "emotional_queries_primary_mode",
        "lifecycle_stage_stale_archived",
        "automated_skill_promotion",
        "automated_hook_promotion",
    )

    success_metrics: tuple[str, ...] = (
        "retrieval_relevance_vs_raw_vector",
        "rehydration_no_silent_data_loss",
        "conflict_no_silent_overwrite",
        "session_duplicate_growth_control",
    )

    def is_deferred(self, feature_name: str) -> bool:
        """Return True when feature is intentionally deferred to post-v0.5 planning."""
        return feature_name in self.deferred_features


def load_scope() -> MilestoneZeroScope:
    """Return the active v1 scope lock contract."""
    return MilestoneZeroScope()
