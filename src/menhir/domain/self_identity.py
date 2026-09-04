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
3. **Authorship is not subjecthood.** Proving who wrote an episode does not prove which extracted
   entity that author is. :func:`eligible_self_evidence` answers the first question and
   :func:`proves_self_subject` the second; binding requires both. No property of the extracted
   NAME -- not the literal string, not its grammatical person -- can answer the second, because
   the name is not provenance. Only a declaration naming the exact in-memory subject node can;
   the binding primitive exists, but the queued Graphiti lifecycle has no structured producer or
   durable declaration transport yet.

The physical Graphiti partition is derived separately by
:func:`menhir.domain.namespace.namespace_to_group_id`. Logical ``default`` maps to physical
``""``, so code must carry the logical namespace explicitly and must never infer ``default``
from ``group_id == ""``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from menhir.domain.namespace import normalize_namespace

__all__ = [
    "FIRST_PERSON_SELF_ALIASES",
    "GATE_APPROVED_HUMAN_SOURCES",
    "SELF_ALIASES",
    "THIRD_PERSON_SELF_ALIASES",
    "SelfEvidenceKind",
    "SelfIdentityContext",
    "SpeakerRole",
    "declare_self_subject",
    "eligible_self_evidence",
    "is_first_person_alias",
    "is_self_alias",
    "normalize_logical_namespace",
    "proves_self_subject",
    "self_context_for_pending_episode",
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
    #: A trusted internal caller declares one exact in-memory node as the turn's human subject.
    EXPLICIT_SELF_SUBJECT = "explicit_self_subject"


#: Source kinds that are structurally incapable of carrying the human, regardless of what a
#: caller claims. Project-scan narrative discusses software users; it never speaks as the owner.
#:
#: Spellings are the literal values production writes -- ``project-scan``
#: (``project_ingest.py``) and ``document-ingest`` (``ingest_document.py``). Compared after
#: folding ``-``/``_`` so a renamed producer cannot slip past on punctuation alone.
#:
#: This is defense in depth, not the primary control: none of these reach
#: :data:`GATE_APPROVED_HUMAN_SOURCES`, so they already fail closed. It exists to stop a caller
#: that hand-builds a context with evidence attached to scan narrative.
_NEVER_SELF_SOURCE_KINDS = frozenset(
    {"project-scan", "project-ingest", "structure-scan", "document-ingest"}
)


def _fold_source_kind(value: str) -> str:
    return value.strip().lower().replace("_", "-")


#: Persisted ``source`` values that prove the admission gate granted a human-authored turn.
#:
#: This set is trustworthy ONLY because of where the value comes from. It is NOT the caller's
#: requested source. ``ingest_intake.queue_episode_for_enrichment`` runs every ``user``/``manual``
#: claim through ``evaluate_user_tier_claim``, which requires Menhir-owned turn evidence whose
#: ``role`` is ``user``, a matching session/namespace, and text actually grounded in that turn.
#: An ungrounded claim -- or any error evaluating it -- is downgraded to ``agent_inference``
#: BEFORE persistence. So a *persisted* ``user``/``manual`` source is a gate receipt, not a
#: caller's assertion.
#:
#: **This is load-bearing and fragile.** It holds only while `create_pending_episode` has exactly
#: one production writer, which is that gated intake. A second writer that persisted a raw
#: caller-supplied source would turn this back into name-only authority -- the precise defect this
#: whole change exists to remove. ``test_self_identity`` pins the single-writer invariant.
GATE_APPROVED_HUMAN_SOURCES = frozenset({"user", "manual"})


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
#: left to distinguish the two. Restoring first-person as authority requires extraction to return
#: per-node provenance (the span each node came from, plus whether that span is inside quoted or
#: reported speech). See ``proves_self_subject``.
FIRST_PERSON_SELF_ALIASES = frozenset({"i", "me", "my", "mine", "myself"})

#: Third-person labels for the human. These are self-LIKE and never self-PROVING on their own: a
#: human turn can discuss an application or RBAC ``user`` distinct from the speaker ("I gave the
#: user read access"), and an entity named ``user`` in a scan is ordinary software vocabulary.
#: Like every alias, they bind only under :attr:`SelfEvidenceKind.EXPLICIT_SELF_SUBJECT`, where a
#: trusted internal caller has vouched that the episode's subject IS the owner.
THIRD_PERSON_SELF_ALIASES = frozenset({"user", "the user"})

SELF_ALIASES = FIRST_PERSON_SELF_ALIASES | THIRD_PERSON_SELF_ALIASES


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
    #: Exact in-memory node identifier selected by a trusted structured assertion. Graphiti types
    #: identifiers as strings (its normal producer uses UUIDs). This is the bridge
    #: from episode authorship to node subjecthood: names, aliases, and grammatical person never
    #: fill it. A caller that already owns the final in-memory payload may set it only by promoting
    #: a TRUSTED_USER_TURN through :func:`declare_self_subject` after constructing the subject
    #: node/edge. The current queued Graphiti path exposes no such caller.
    subject_node_uuid: str | None = None

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


def declare_self_subject(
    context: SelfIdentityContext,
    *,
    subject_node_uuid: Any,
) -> SelfIdentityContext:
    """Promote trusted turn evidence to an exact, node-scoped self declaration.

    The caller must already own the final payload and subject assignment: for example, a structured
    memory writer that constructed an ``EntityEdge`` with the turn author as its source. This
    function does not inspect text, names, edge facts, or grammatical person. It binds the
    declaration to the current episode and the exact in-memory node UUID the caller constructed.

    This is a binding primitive, not yet a production transport. Menhir's queued Graphiti path
    reconstructs only episode-level context before Graphiti allocates node UUIDs, so a future
    producer also needs a durable structured payload and a post-repair/pre-dedup injection point.

    A normal user turn is deliberately insufficient. Only a trusted user-turn context with an
    episode UUID can be promoted, and the selected node UUID must be non-blank. Invalid inputs fail
    before extraction or graph mutation.
    """
    selected = str(subject_node_uuid or "").strip()
    if context.evidence_kind is not SelfEvidenceKind.TRUSTED_USER_TURN:
        raise ValueError("self subject declaration requires trusted user-turn evidence")
    if context.speaker_role is not SpeakerRole.USER:
        raise ValueError("self subject declaration requires the trusted user role")
    if not str(context.episode_uuid or "").strip():
        raise ValueError("self subject declaration requires an episode UUID")
    if not selected:
        raise ValueError("self subject declaration requires a non-blank node UUID")
    if len(selected) > 256 or not selected.isprintable():
        raise ValueError("self subject declaration requires a bounded printable node UUID")
    if _fold_source_kind(context.source_kind) in _NEVER_SELF_SOURCE_KINDS:
        raise ValueError("self subject declaration refuses a structurally non-self source")
    return replace(
        context,
        evidence_kind=SelfEvidenceKind.EXPLICIT_SELF_SUBJECT,
        subject_node_uuid=selected,
    )


def proves_self_subject(node_uuid: Any, context: SelfIdentityContext | None) -> bool:
    """Whether *node_uuid*, in an episode with *context*, proves THIS NODE is the owning human.

    This is the node-level half of the contract, and it exists because the episode-level half is
    not sufficient. :func:`eligible_self_evidence` proves who AUTHORED an episode; it says nothing
    about which extracted entity is that author. Treating authorship as node-level authority binds
    whatever self-like entity happens to appear -- an RBAC ``user``, a `users` table, the customer
    a support turn is about -- into the canonical human identity, which no later migration can
    separate again.

    Exactly one thing proves it: :attr:`SelfEvidenceKind.EXPLICIT_SELF_SUBJECT` naming this exact
    in-memory node UUID, produced by :func:`declare_self_subject` from trusted user-turn evidence.
    The declaration is the authority; nothing here infers it from text.

    **No name shape qualifies, first-person included.** Two successive revisions tried to promote
    one: first the literal name ``user``, then first-person grammar. Both are properties of the
    extracted string rather than of its provenance, and both have counterexamples inside a
    perfectly valid human turn -- an RBAC ``user`` for the first, reported speech
    (``She told me, "I will handle it"``) for the second. By the time binding runs, extraction has
    discarded the quote boundaries, source spans and speaker attribution that would separate them.

    **Consequence, stated where it cannot be missed:** the exact-node binding primitive now exists,
    but no production producer or durable transport calls it today, so this returns ``False`` for
    every real Graphiti episode and binding remains inert. Ordinary extracted text cannot supply
    this declaration.
    """
    if not eligible_self_evidence(context):
        return False
    assert context is not None  # narrowed by eligible_self_evidence
    if context.evidence_kind is not SelfEvidenceKind.EXPLICIT_SELF_SUBJECT:
        return False
    selected = str(context.subject_node_uuid or "").strip()
    return bool(selected) and str(node_uuid or "").strip() == selected


def self_context_for_pending_episode(
    *,
    source: Any,
    namespace: Any,
    episode_uuid: str | None = None,
    source_kind: str = "",
) -> SelfIdentityContext:
    """Reconstruct the identity context for a claimed pending episode.

    The evidence survives the asynchronous queue in the episode's persisted ``source``, so no new
    field and no schema change is required -- see :data:`GATE_APPROVED_HUMAN_SOURCES` for why that
    value is a gate receipt rather than a caller's claim.

    A retry, repair or replay of the same episode reads the same persisted source and therefore
    reconstructs the same evidence. It cannot infer stronger evidence than the original episode
    carried, which is the Phase 2 requirement that evidence never strengthens on retry.

    Anything outside the gate-approved set yields ``UNKNOWN`` role and no evidence: agent-authored
    memories, hook and scanner output, imports, and every other producer are not the human.
    """
    normalized_source = str(source or "").strip().lower()
    if normalized_source in GATE_APPROVED_HUMAN_SOURCES:
        return SelfIdentityContext(
            namespace=namespace,
            speaker_role=SpeakerRole.USER,
            evidence_kind=SelfEvidenceKind.TRUSTED_USER_TURN,
            source_kind=source_kind or normalized_source,
            episode_uuid=episode_uuid,
            subject_node_uuid=None,
        )
    return SelfIdentityContext(
        namespace=namespace,
        speaker_role=SpeakerRole.UNKNOWN,
        evidence_kind=None,
        source_kind=source_kind or normalized_source,
        episode_uuid=episode_uuid,
        subject_node_uuid=None,
    )


def eligible_self_evidence(context: SelfIdentityContext | None) -> bool:
    """Whether *context* positively establishes the owning human. Fails closed.

    Requires a trusted evidence kind AND a role consistent with it. Everything else -- unknown
    role, assistant/tool/system turns, project-scan narrative, a bare entity name, a ``user:``
    prefix inside arbitrary content -- is insufficient by design.
    """
    if context is None or context.evidence_kind is None:
        return False
    if _fold_source_kind(context.source_kind) in _NEVER_SELF_SOURCE_KINDS:
        return False
    if context.speaker_role in _NON_HUMAN_ROLES:
        return False
    if context.evidence_kind is SelfEvidenceKind.TRUSTED_USER_TURN:
        # The evidence IS the trusted role, so the role must actually be the human.
        return context.speaker_role is SpeakerRole.USER
    # EXPLICIT_SELF_SUBJECT is valid only as the node-scoped promotion of a trusted human turn.
    # Requiring the episode and exact node UUID prevents an episode-wide declaration from falling
    # back to the same name-shaped inference this contract exists to remove.
    return bool(
        context.speaker_role is SpeakerRole.USER
        and str(context.episode_uuid or "").strip()
        and str(context.subject_node_uuid or "").strip()
    )
