"""Exact owner-authorization contract for semantic assertions about canonical self.

Identity and assertion authority are deliberately separate.  A trusted turn can establish which
namespace owns the speaker, but it cannot make an LLM-produced relationship true.  This module
defines the byte-stable proposal that an owner signs out of band; no text classifier, model vote,
caller flag, or confidence score can manufacture that signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping

from menhir.domain.self_identity import normalize_logical_namespace, self_uuid_for_namespace

SELF_ASSERTION_SCHEMA_VERSION = 1
SELF_ASSERTION_POLICY_VERSION = "menhir-canonical-self-authority-v1"
SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY = "menhir_self_authority_payload_json"

__all__ = [
    "SELF_ASSERTION_POLICY_VERSION",
    "SELF_ASSERTION_SCHEMA_VERSION",
    "SELF_ASSERTION_EDGE_PAYLOAD_PROPERTY",
    "SelfAssertionProposal",
    "SelfAuthorizationDecision",
    "UnconfirmedSelfAssertionError",
    "canonical_json_bytes",
    "canonical_temporal_value",
    "is_canonical_self_subject",
    "make_self_assertion_proposal",
    "proposal_from_confirmation_payload",
    "proposal_matches_persisted_edge",
]


class UnconfirmedSelfAssertionError(ValueError):
    """A durable assertion writer was asked to attach unverified semantics to self."""


def is_canonical_self_subject(subject_uuid: Any, namespace: Any) -> bool:
    """Recognize the deterministic canonical subject at final typed-assertion write seams."""

    subject = str(subject_uuid or "").strip()
    return bool(subject) and subject == self_uuid_for_namespace(namespace)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode signed material with one deterministic JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_temporal_value(value: Any) -> str | None:
    """Normalize persisted/model temporal values for signed equality checks."""

    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    rendered = str(isoformat() if callable(isoformat) else value).strip()
    if not rendered:
        return None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        return rendered
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def _bounded_text(value: Any, field: str, *, required: bool = True, limit: int = 16_384) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"self-assertion proposal requires {field}")
    if len(text) > limit or (text and not text.isprintable()):
        raise ValueError(f"self-assertion proposal has invalid {field}")
    return text


def _canonical_object(value: Mapping[str, Any], field: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"self-assertion {field} must be a mapping")
    encoded = canonical_json_bytes(dict(value))
    if len(encoded) > 65_536:
        raise ValueError(f"self-assertion {field} is too large")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError(f"self-assertion {field} must encode an object")
    return encoded.decode("utf-8")


@dataclass(frozen=True, slots=True)
class SelfAssertionProposal:
    """One exact semantic proposal that may be authorized for canonical self.

    ``assertion_json`` and ``temporal_scope_json`` are already canonicalized.  Keeping the signed
    object as immutable text prevents a mutable model payload from changing between verification
    and binding.
    """

    principal_id: str
    namespace: str
    episode_uuid: str
    turn_evidence_uuid: str
    evidence_sha256: str
    lane: str
    direction: str
    polarity: str
    assertion_json: str
    temporal_scope_json: str
    claim_revision: int = 1
    schema_version: int = SELF_ASSERTION_SCHEMA_VERSION
    policy_version: str = SELF_ASSERTION_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal_id", _bounded_text(self.principal_id, "principal_id"))
        object.__setattr__(self, "namespace", normalize_logical_namespace(self.namespace))
        object.__setattr__(self, "episode_uuid", _bounded_text(self.episode_uuid, "episode_uuid"))
        object.__setattr__(
            self,
            "turn_evidence_uuid",
            _bounded_text(self.turn_evidence_uuid, "turn_evidence_uuid"),
        )
        digest = _bounded_text(self.evidence_sha256, "evidence_sha256").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("self-assertion evidence_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "evidence_sha256", digest)
        object.__setattr__(self, "lane", _bounded_text(self.lane, "lane", limit=64))
        object.__setattr__(self, "direction", _bounded_text(self.direction, "direction", limit=64))
        object.__setattr__(self, "polarity", _bounded_text(self.polarity, "polarity", limit=64))
        if type(self.claim_revision) is not int or self.claim_revision < 1:
            raise ValueError("self-assertion claim_revision must be positive")
        if (
            type(self.schema_version) is not int
            or self.schema_version != SELF_ASSERTION_SCHEMA_VERSION
        ):
            raise ValueError("unsupported self-assertion schema version")
        if self.policy_version != SELF_ASSERTION_POLICY_VERSION:
            raise ValueError("unsupported self-assertion policy version")
        for field in ("assertion_json", "temporal_scope_json"):
            raw = getattr(self, field)
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"self-assertion {field} is not valid JSON") from exc
            if not isinstance(decoded, dict) or canonical_json_bytes(decoded).decode("utf-8") != raw:
                raise ValueError(f"self-assertion {field} is not a canonical JSON object")

    def unsigned_payload(self) -> dict[str, Any]:
        """Return the complete assertion tuple, excluding its derived digest."""

        return {
            "assertion": json.loads(self.assertion_json),
            "claim_revision": self.claim_revision,
            "direction": self.direction,
            "episode_uuid": self.episode_uuid,
            "evidence_sha256": self.evidence_sha256,
            "lane": self.lane,
            "namespace": self.namespace,
            "policy_version": self.policy_version,
            "polarity": self.polarity,
            "principal_id": self.principal_id,
            "schema_version": self.schema_version,
            "temporal_scope": json.loads(self.temporal_scope_json),
            "turn_evidence_uuid": self.turn_evidence_uuid,
        }

    @property
    def claim_digest(self) -> str:
        return sha256(canonical_json_bytes(self.unsigned_payload())).hexdigest()

    def confirmation_payload(self) -> dict[str, Any]:
        """Return the exact object whose canonical JSON bytes the owner signs."""

        return {**self.unsigned_payload(), "claim_digest": self.claim_digest}

    def audit_record(self, decision: "SelfAuthorizationDecision") -> dict[str, Any]:
        """Bounded durable proposal receipt; never includes a signature or private material."""

        return {
            **self.confirmation_payload(),
            "authorization": {
                "authorized": decision.authorized,
                "authority_key_id": decision.authority_key_id,
                "reason": decision.reason,
            },
        }


@dataclass(frozen=True, slots=True)
class SelfAuthorizationDecision:
    """Result of checking one proposal against the nondelegated owner authority."""

    authorized: bool
    reason: str
    authority_key_id: str = ""


def make_self_assertion_proposal(
    *,
    principal_id: Any,
    namespace: Any,
    episode_uuid: Any,
    turn_evidence_uuid: Any,
    evidence_text: str,
    lane: Any,
    direction: Any,
    polarity: Any,
    assertion: Mapping[str, Any],
    temporal_scope: Mapping[str, Any] | None = None,
    claim_revision: int = 1,
) -> SelfAssertionProposal:
    """Canonicalize an exact structured assertion and bind it to its evidence lineage."""

    if not isinstance(evidence_text, str):
        raise TypeError("self-assertion evidence_text must be a string")
    return SelfAssertionProposal(
        principal_id=str(principal_id or ""),
        namespace=str(namespace or ""),
        episode_uuid=str(episode_uuid or ""),
        turn_evidence_uuid=str(turn_evidence_uuid or ""),
        evidence_sha256=sha256(evidence_text.encode("utf-8")).hexdigest(),
        lane=str(lane or ""),
        direction=str(direction or ""),
        polarity=str(polarity or ""),
        assertion_json=_canonical_object(assertion, "assertion"),
        temporal_scope_json=_canonical_object(temporal_scope or {}, "temporal_scope"),
        claim_revision=claim_revision,
    )


def proposal_from_confirmation_payload(value: Mapping[str, Any]) -> SelfAssertionProposal:
    """Rehydrate a persisted signed payload without trusting any field or derived digest.

    Graphiti stores the exact payload on an authorized fact edge so recall can re-check the
    external owner confirmation.  Equality against ``confirmation_payload()`` rejects omitted,
    extra, retyped, stale-policy, and digest-tampered records before signature verification.
    """

    if not isinstance(value, Mapping):
        raise TypeError("self-assertion confirmation payload must be a mapping")
    try:
        proposal = SelfAssertionProposal(
            principal_id=value["principal_id"],
            namespace=value["namespace"],
            episode_uuid=value["episode_uuid"],
            turn_evidence_uuid=value["turn_evidence_uuid"],
            evidence_sha256=value["evidence_sha256"],
            lane=value["lane"],
            direction=value["direction"],
            polarity=value["polarity"],
            assertion_json=_canonical_object(value["assertion"], "assertion"),
            temporal_scope_json=_canonical_object(
                value["temporal_scope"], "temporal_scope"
            ),
            claim_revision=value["claim_revision"],
            schema_version=value["schema_version"],
            policy_version=value["policy_version"],
        )
    except KeyError as exc:
        raise ValueError(
            f"self-assertion confirmation payload is missing {exc.args[0]}"
        ) from exc
    expected = proposal.confirmation_payload()
    if dict(value) != expected:
        raise ValueError("self-assertion confirmation payload is not exact")
    return proposal


def proposal_matches_persisted_edge(
    proposal: SelfAssertionProposal,
    *,
    expected_self_uuid: str,
    source_node_uuid: Any,
    target_node_uuid: Any,
    counterpart_name: Any,
    counterpart_labels: Any,
    predicate: Any,
    fact: Any,
    valid_at: Any,
    invalid_at: Any,
    expired_at: Any,
) -> bool:
    """Require the actual relationship to equal the signed semantic and temporal tuple."""

    expected = str(expected_self_uuid or "").strip()
    source = str(source_node_uuid or "").strip()
    target = str(target_node_uuid or "").strip()
    if not expected or self_uuid_for_namespace(proposal.namespace) != expected:
        return False
    if proposal.direction == "self_to_entity":
        if source != expected or target == expected:
            return False
    elif proposal.direction == "entity_to_self":
        if target != expected or source == expected:
            return False
    else:
        return False
    assertion = json.loads(proposal.assertion_json)
    counterpart = assertion.get("counterpart")
    signed_labels = counterpart.get("labels") if isinstance(counterpart, dict) else None
    actual_labels = counterpart_labels if isinstance(counterpart_labels, (list, tuple)) else None
    if (
        not isinstance(counterpart, dict)
        or not isinstance(signed_labels, list)
        or any(not isinstance(label, str) for label in signed_labels)
        or actual_labels is None
        or any(not isinstance(label, str) for label in actual_labels)
        or tuple(sorted(signed_labels)) != tuple(sorted(actual_labels))
        or str(counterpart.get("name") or "").strip().casefold()
        != str(counterpart_name or "").strip().casefold()
    ):
        return False
    if str(assertion.get("predicate") or "") != str(predicate or ""):
        return False
    if str(assertion.get("fact") or "") != str(fact or ""):
        return False
    signed_temporal = json.loads(proposal.temporal_scope_json)
    actual_temporal = {
        "expired_at": canonical_temporal_value(expired_at),
        "invalid_at": canonical_temporal_value(invalid_at),
        "valid_at": canonical_temporal_value(valid_at),
    }
    return signed_temporal == actual_temporal
