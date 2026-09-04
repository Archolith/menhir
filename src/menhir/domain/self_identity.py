"""Canonical self-identity primitives: one formula, one evidence contract.

Menhir must have exactly one authoritative human-self entity per logical namespace, and must
never ask semantic retrieval or an LLM to decide that identity. This module is the single source
of truth for *who the human is* and *what proves it*. It is pure: no infrastructure imports, no
I/O, no graph access, so every caller -- writer and reader alike -- can use it on a hot path.

Two rules carry the whole design:

1. **The name is never authority.** An entity called ``user`` is not the human because it is
   spelled that way. Only trusted, Menhir-owned episode metadata establishes the human, via
   :func:`eligible_self_evidence`. An ordinary software actor named ``user`` stays an ordinary
   semantic entity.
2. **One formula.** :func:`self_uuid_for_namespace` is the only permitted derivation of the
   canonical self UUID. Copies are how a split identity gets created; see the RCA at
   ``.agent/plans/menhir-scanner-generic-entity-recall-pollution-rca.md``.

The physical Graphiti partition is derived separately by
:func:`menhir.domain.namespace.namespace_to_group_id`. Logical ``default`` maps to physical
``""``, so code must carry the logical namespace explicitly and must never infer ``default``
from ``group_id == ""``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from menhir.domain.namespace import normalize_namespace

__all__ = [
    "SELF_ALIASES",
    "SelfEvidenceKind",
    "SelfIdentityContext",
    "SpeakerRole",
    "eligible_self_evidence",
    "is_self_alias",
    "normalize_logical_namespace",
    "self_uuid_for_namespace",
]


class SpeakerRole(StrEnum):
    """Who produced the episode, according to Menhir-owned turn/admission metadata.

    ``UNKNOWN`` is the safe default for any producer that cannot prove a role. It is never
    self-eligible on its own -- absence of evidence is not evidence of the human.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"
    UNKNOWN = "unknown"


#: Roles that can never establish the human, even paired with an explicit evidence kind. An
#: assistant turn asserting a fact about "the user" is the self-echo case, not the human speaking.
_NON_HUMAN_ROLES = frozenset({SpeakerRole.ASSISTANT, SpeakerRole.TOOL, SpeakerRole.SYSTEM})


class SelfEvidenceKind(StrEnum):
    """The only two signals that may establish the owning human."""

    #: Role came from Menhir-owned turn/admission metadata, not from parsing episode text.
    TRUSTED_USER_TURN = "trusted_user_turn"
    #: A trusted internal caller declares that this episode records a fact about the owner.
    EXPLICIT_SELF_SUBJECT = "explicit_self_subject"


#: Source kinds that are structurally incapable of carrying the human, regardless of what a
#: caller claims. Project-scan narrative discusses software users; it never speaks as the owner.
_NEVER_SELF_SOURCE_KINDS = frozenset({"project_scan", "project_ingest", "structure_scan"})


#: Normalized names that *may* denote the human, consulted ONLY after trusted evidence exists.
#: Membership here is never itself evidence -- see :func:`is_self_alias`.
#:
#: This mirrors the extraction-time set in ``graphiti_extraction_patches`` (third-person plus
#: first-person), which governs the same seam this identity contract binds at. Two other
#: divergent sets exist today -- ``typed_scalar_rules.SELF_TOKENS`` (no ``my``/``mine``) and
#: ``event_consolidation._SELF_TOKENS`` (adds ``speaker``). They are deliberately NOT rewired
#: here: consolidating them changes behavior in three subsystems and is out of scope for this
#: change. Recorded in the Phase 0 baseline as follow-up work.
SELF_ALIASES = frozenset({"user", "the user", "i", "me", "my", "mine", "myself"})


@dataclass(frozen=True, slots=True)
class SelfIdentityContext:
    """Immutable, task-local statement of what the ingestion boundary actually proved.

    Constructed at the boundary that owns the turn metadata and carried into extraction. It
    records evidence; it does not decide identity. :func:`eligible_self_evidence` decides.
    """

    #: Logical namespace, already normalized. NOT the physical Graphiti group.
    namespace: str
    #: Trusted speaker role. Never inferred from episode content.
    speaker_role: SpeakerRole = SpeakerRole.UNKNOWN
    #: ``None`` means no trusted self signal was supplied -- the common, correct case.
    evidence_kind: SelfEvidenceKind | None = None
    #: Producer identifier, used to fail closed on structurally non-self sources.
    source_kind: str = ""
    #: The episode this evidence belongs to. Evidence never outlives its episode.
    episode_uuid: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", normalize_logical_namespace(self.namespace))

    @property
    def self_uuid(self) -> str:
        """The canonical self UUID for this context's logical namespace."""
        return self_uuid_for_namespace(self.namespace)


def normalize_logical_namespace(value: Any) -> str:
    """Resolve any namespace-shaped value to the logical silo that owns identity.

    Delegates to :func:`menhir.domain.namespace.normalize_namespace` rather than reimplementing
    it -- safety invariant 3 requires one mapping. ``None``, empty and whitespace-only all
    collapse to ``default``.

    Note this is ``normalize_namespace``, not ``stamped_namespace``. The two agree on every
    realistic input (``None``, ``""`` at the MCP boundary, ``"default"``, and named namespaces)
    and differ only for whitespace-only strings, which ``stamped_namespace`` passes through
    verbatim. Such a value is already invalid as a Graphiti ``group_id``; folding it to the
    default silo here is deliberate, and prevents minting a self node under a blank name.
    """
    return normalize_namespace(value)


def self_uuid_for_namespace(namespace: Any) -> str:
    """The deterministic canonical self UUID for *namespace*.

    THE one derivation. Pure and I/O-free by contract: recall resolves a first-person query's
    subject with this and performs no database read, so any caller may use it on a hot path.

    Byte-identical to the pre-existing ``uuid5(NAMESPACE_URL, f"menhir-self:<ns>")`` contract
    that ``ensure_self_entity`` and both recall paths derived independently.
    """
    return str(uuid5(NAMESPACE_URL, f"menhir-self:{normalize_logical_namespace(namespace)}"))


def is_self_alias(name: Any) -> bool:
    """Whether *name* is a recognized self alias, after normalization.

    **Not evidence.** This answers "could this extracted node be the human", never "is it".
    Callers must establish :func:`eligible_self_evidence` first; a node passing this check
    without trusted evidence stays an ordinary semantic entity.
    """
    if name is None:
        return False
    return " ".join(str(name).strip().lower().split()) in SELF_ALIASES


def eligible_self_evidence(context: SelfIdentityContext | None) -> bool:
    """Whether *context* positively establishes the owning human. Fails closed.

    Requires a trusted evidence kind AND a role consistent with it. Everything else -- unknown
    role, assistant/tool/system turns, project-scan narrative, a bare entity name, a ``user:``
    prefix inside arbitrary content -- is insufficient by design.
    """
    if context is None or context.evidence_kind is None:
        return False
    if context.source_kind.strip().lower() in _NEVER_SELF_SOURCE_KINDS:
        return False
    if context.speaker_role in _NON_HUMAN_ROLES:
        return False
    if context.evidence_kind is SelfEvidenceKind.TRUSTED_USER_TURN:
        # The evidence IS the trusted role, so the role must actually be the human.
        return context.speaker_role is SpeakerRole.USER
    # EXPLICIT_SELF_SUBJECT: a trusted internal caller vouches for the subject, so an
    # unknown-role episode is admissible -- but a non-human role above already disqualified it.
    return True
