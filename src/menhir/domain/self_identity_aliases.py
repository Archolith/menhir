"""Self-alias name shapes for the canonical self-identity contract.

Extracted verbatim from :mod:`menhir.domain.self_identity`, which re-exports every public name
defined here; import them from the facade module. This module holds only name-shape vocabulary:
the evidence contract, the derivation formula, and the declaration/binding primitives stay in the
facade module. Nothing here is identity authority on its own.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "FIRST_PERSON_SELF_ALIASES",
    "SELF_ALIASES",
    "THIRD_PERSON_SELF_ALIASES",
    "is_first_person_alias",
    "is_self_alias",
]


#: Normalized names that *may* denote the human, consulted ONLY after trusted evidence exists.
#: Membership here is never itself evidence -- see :func:`is_self_alias`.
#:
#: DOMAIN: extracted entity NAMES, mirroring ``graphiti_extraction_patches``, which governs the
#: same seam this contract binds at.
#:
#: Two other self-token sets exist and are **deliberately different, not drift**. They answer
#: different questions over different inputs, so unifying them would widen admission in three
#: subsystems -- each widening a chance to bind something that is not the human:
#:
#: - ``typed_scalar_rules.SELF_TOKENS`` -- scalar-proposal ``subject_text``. Omits ``my``/``mine``
#:   because those are plausible entity names but malformed subjects.
#: - ``event_consolidation._SELF_TOKENS`` -- event-proposal subject. Adds ``speaker``, which only
#:   that producer emits; admitting it here would bind an entity named "speaker" to the human.
#:
#: Do not merge them.
#: First-person references. These are self-LIKE and, like every other name shape, NOT authority.
#:
#: A previous revision treated first-person grammar as node-level proof: inside a turn whose author
#: was proven, an extracted `I` was taken to name that author. That is wrong for the same reason
#: the literal name `user` is wrong -- grammatical person is a property of the extracted STRING,
#: not of where the string came from. The counterexample is reported speech: a proven human turn
#: reading `She told me, "I will handle it"` extracts an `I` that is a different person, and by the
#: time binding sees the payload there is no quote boundary, source span, or speaker attribution
#: left to distinguish the two. Richer provenance may improve attribution, but model-produced
#: spans or speaker labels still do not create identity authority. The automatic-memory path
#: instead declares its own author node before extraction. See ``proves_self_subject``.
FIRST_PERSON_SELF_ALIASES = frozenset({"i", "me", "my", "mine", "myself"})

#: Third-person labels for the human. These are self-LIKE and never self-PROVING on their own: a
#: human turn can discuss an application or RBAC ``user`` distinct from the speaker ("I gave the
#: user read access"), and an entity named ``user`` in a scan is ordinary software vocabulary.
#: Like every alias, they bind only under :attr:`SelfEvidenceKind.EXPLICIT_SELF_SUBJECT`, where a
#: trusted internal caller has vouched that the episode's subject IS the owner.
THIRD_PERSON_SELF_ALIASES = frozenset({"user", "the user"})

SELF_ALIASES = FIRST_PERSON_SELF_ALIASES | THIRD_PERSON_SELF_ALIASES


def is_self_alias(name: Any) -> bool:
    """Whether *name* is a recognized self alias, after normalization.

    **Not evidence.** This answers "could this extracted node be the human", never "is it".
    Callers must establish :func:`eligible_self_evidence` first; a node passing this check
    without trusted evidence stays an ordinary semantic entity.
    """
    return _normalize_alias(name) in SELF_ALIASES


def is_first_person_alias(name: Any) -> bool:
    """Whether *name* is a first-person self alias.

    **Not authority** -- see :func:`proves_self_subject`. This exists only so binding can COUNT
    the first-person nodes it declines. That count is an upper bound on what richer per-node
    provenance might resolve, because quoted or reported speech can still be non-self.
    """
    return _normalize_alias(name) in FIRST_PERSON_SELF_ALIASES


def _normalize_alias(name: Any) -> str:
    """Fold an extracted entity name for alias comparison. ``""`` for anything unusable."""
    if name is None:
        return ""
    return " ".join(str(name).strip().lower().split())
