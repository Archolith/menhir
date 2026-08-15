"""Persistence codec owned by the personality reference extension.

Generic mutation-kernel persistence stores opaque JSON payloads. This module is the extension-side
inverse that maps personality value objects to/from those payloads, keeping personality semantics out
of the Neo4j store.
"""

from __future__ import annotations

from dataclasses import dataclass

from .personality import ContinuousSignal, PolicyDecision, PolicySignal


@dataclass(frozen=True)
class PersonalityValueCodec:
    """Round-trip the value types used by personality Assertions and Views."""

    _TAG = "__personality_value__"

    def encode(self, value: object) -> object:
        if isinstance(value, ContinuousSignal):
            return {
                self._TAG: "continuous_signal",
                "score": value.score,
                "weight": value.weight,
                "explanation": value.explanation,
            }
        if isinstance(value, PolicySignal):
            return {
                self._TAG: "policy_signal",
                "action": value.action,
                "outcome": value.outcome,
                "weight": value.weight,
                "explanation": value.explanation,
            }
        if isinstance(value, PolicyDecision):
            return {
                self._TAG: "policy_decision",
                "action": value.action,
                "action_scores": [list(item) for item in value.action_scores],
                "evidence_counts": [list(item) for item in value.evidence_counts],
            }
        if value is None or isinstance(value, (str, int, float, bool)):
            return {self._TAG: "scalar", "value": value}
        raise TypeError(f"unsupported personality value for persistence: {type(value).__name__}")

    def decode(self, payload: object) -> object:
        if not isinstance(payload, dict):
            raise TypeError("personality persistence payload must be an object")
        kind = payload.get(self._TAG)
        if kind == "continuous_signal":
            return ContinuousSignal(
                score=float(payload["score"]),
                weight=float(payload["weight"]),
                explanation=str(payload.get("explanation") or ""),
            )
        if kind == "policy_signal":
            return PolicySignal(
                action=str(payload["action"]),
                outcome=float(payload["outcome"]),
                weight=float(payload["weight"]),
                explanation=str(payload.get("explanation") or ""),
            )
        if kind == "policy_decision":
            return PolicyDecision(
                action=str(payload["action"]),
                action_scores=tuple(
                    (str(name), float(score))
                    for name, score in payload.get("action_scores", [])
                ),
                evidence_counts=tuple(
                    (str(name), int(count))
                    for name, count in payload.get("evidence_counts", [])
                ),
            )
        if kind == "scalar":
            return payload.get("value")
        raise ValueError(f"unknown personality persistence payload kind: {kind!r}")


PERSONALITY_VALUE_CODEC = PersonalityValueCodec()
