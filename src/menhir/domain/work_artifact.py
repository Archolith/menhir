"""WorkArtifact — engineering documents as semantic objects.

Plans, reviews, investigations and implementation reports. Git owns the bytes;
menhir owns identity, lifecycle, relationships and provenance. This module is
the pure domain half: types, states, legal transitions, and embodiment media.

Named ``WorkArtifact`` rather than ``Artifact`` because ``domain/artifacts.py``
already owns that word for the L4 institutional loop (Decision / Failure /
Incident backed by :Evidence, with a trust policy). Those answer "should I
believe this?"; a plan answers "is this current?". ``Document`` was also taken
-- ``ingest_document`` writes ``structure_role='document'`` entities.

See `model.primitives` in .agent/data_models.md for the modeling rules this
follows, and .agent/plans/menhir-artifact-semantic-model.md for the design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Bumped when artifact normalization rules change.
ARTIFACT_SCHEMA_VERSION = 1

#: Shared silo, matching the :Todo invariant -- namespace is never null.
DEFAULT_ARTIFACT_NAMESPACE = "default"


class ArtifactType:
    PLAN = "plan"
    REVIEW = "review"
    INVESTIGATION = "investigation"
    IMPLEMENTATION_REPORT = "implementation_report"
    HANDOFF = "handoff"


#: Deliberately not generalized further yet. Deferred: adr, migration, rfc,
#: orientation.
#:
#: HANDOFF was on that deferred list until the corpus answered the question: 14
#: handoff documents exist across two repos, and without a type they were being
#: recorded as implementation reports and failing a wrapup contract that was
#: never theirs. A type earns its place by having instances, not by being
#: imaginable.
ARTIFACT_TYPES: frozenset[str] = frozenset({
    ArtifactType.PLAN,
    ArtifactType.REVIEW,
    ArtifactType.INVESTIGATION,
    ArtifactType.IMPLEMENTATION_REPORT,
    ArtifactType.HANDOFF,
})


class ArtifactStatus:
    PROPOSED = "PROPOSED"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    IMPLEMENTING = "IMPLEMENTING"
    IMPLEMENTED = "IMPLEMENTED"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    DRAFT = "DRAFT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    SUPERSEDED = "SUPERSEDED"
    DEFERRED = "DEFERRED"


#: Reachable from any state of any type.
#:
#: SUPERSEDED means "a better answer exists"; DEFERRED means "we intentionally
#: chose not to answer yet". Collapsing them would make "what is still
#: deliberately deferred?" unanswerable, which is why DEFERRED is first-class
#: rather than a flavor of SUPERSEDED.
TERMINAL_ANY: frozenset[str] = frozenset({
    ArtifactStatus.SUPERSEDED,
    ArtifactStatus.DEFERRED,
})

#: Forward progressions per type. TERMINAL_ANY is additionally legal everywhere.
_FORWARD: dict[str, dict[str, frozenset[str]]] = {
    ArtifactType.PLAN: {
        ArtifactStatus.PROPOSED: frozenset({ArtifactStatus.REVIEWED}),
        ArtifactStatus.REVIEWED: frozenset({ArtifactStatus.APPROVED}),
        ArtifactStatus.APPROVED: frozenset({ArtifactStatus.IMPLEMENTING}),
        ArtifactStatus.IMPLEMENTING: frozenset({ArtifactStatus.IMPLEMENTED}),
        ArtifactStatus.IMPLEMENTED: frozenset(),
    },
    ArtifactType.REVIEW: {
        ArtifactStatus.OPEN: frozenset({ArtifactStatus.COMPLETE}),
        ArtifactStatus.COMPLETE: frozenset(),
    },
    ArtifactType.INVESTIGATION: {
        ArtifactStatus.OPEN: frozenset({ArtifactStatus.COMPLETE}),
        ArtifactStatus.COMPLETE: frozenset(),
    },
    # Mirrors the workspace's documented wrapup contract (WRAPUP-TEMPLATE.md:
    # READY FOR REVIEW | PARTIAL | BLOCKED | REVIEWED | REVIEWED WITH FINDINGS).
    # A two-state lifecycle could not answer "was this reviewed?", which is the
    # question the whole wrapup process exists to answer.
    #
    # DRAFT -> COMPLETE is retained: a report written outside the review process
    # is finished when its author says so, and forcing it through a review it
    # never had would be recording a review that did not happen.
    ArtifactType.IMPLEMENTATION_REPORT: {
        ArtifactStatus.DRAFT: frozenset({
            ArtifactStatus.READY_FOR_REVIEW,
            ArtifactStatus.COMPLETE,
        }),
        ArtifactStatus.READY_FOR_REVIEW: frozenset({
            ArtifactStatus.REVIEWED,
            ArtifactStatus.COMPLETE,
        }),
        ArtifactStatus.REVIEWED: frozenset({ArtifactStatus.COMPLETE}),
        ArtifactStatus.COMPLETE: frozenset(),
    },
    # A handoff is written to be picked up. OPEN means nobody has yet;
    # COMPLETE means someone did. DEFERRED (via TERMINAL_ANY) covers the
    # handoff that was deliberately never taken up, which is a real outcome
    # and not the same as one still waiting.
    ArtifactType.HANDOFF: {
        ArtifactStatus.OPEN: frozenset({ArtifactStatus.COMPLETE}),
        ArtifactStatus.COMPLETE: frozenset(),
    },
}

#: The state a newly created artifact of each type starts in.
INITIAL_STATUS: dict[str, str] = {
    ArtifactType.PLAN: ArtifactStatus.PROPOSED,
    ArtifactType.REVIEW: ArtifactStatus.OPEN,
    ArtifactType.INVESTIGATION: ArtifactStatus.OPEN,
    ArtifactType.IMPLEMENTATION_REPORT: ArtifactStatus.DRAFT,
    ArtifactType.HANDOFF: ArtifactStatus.OPEN,
}


class ArtifactMedium:
    MARKDOWN = "markdown"
    PDF = "pdf"
    HTML = "html"
    WIKI = "wiki"
    FILE = "file"


ARTIFACT_MEDIA: frozenset[str] = frozenset({
    ArtifactMedium.MARKDOWN,
    ArtifactMedium.PDF,
    ArtifactMedium.HTML,
    ArtifactMedium.WIKI,
    ArtifactMedium.FILE,
})


def valid_statuses(artifact_type: str) -> frozenset[str]:
    """Every status an artifact of this type may hold."""
    forward = _FORWARD.get(artifact_type, {})
    return frozenset(forward) | TERMINAL_ANY


def can_transition(artifact_type: str, from_status: str, to_status: str) -> bool:
    """Whether this status change is legal for this artifact type.

    A transition to itself is refused: a no-op status change would still stamp
    ``status_changed_at``, making an artifact look freshly moved when nothing
    happened. Terminal states are reachable from anywhere but lead nowhere --
    reopening a superseded plan means authoring a new one, which is what
    SUPERSEDES is for.
    """
    if artifact_type not in _FORWARD:
        return False
    if from_status == to_status:
        return False
    if from_status not in valid_statuses(artifact_type):
        return False
    if to_status in TERMINAL_ANY:
        return from_status not in TERMINAL_ANY
    return to_status in _FORWARD[artifact_type].get(from_status, frozenset())


def resolve_registration(artifact_type: str, status: str | None) -> str:
    """Decide the status a new artifact of this type may be registered with (CF-48).

    Moved here from the repository to match `can_transition`, which the same repository already
    delegates to. The asymmetry was the finding: ordinary transitions asked the domain, while the
    richer aggregate operations decided for themselves in infrastructure.

    Raises ValueError with the same messages the repository raised, because they are the
    registration contract's error surface and callers key on them.
    """
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact_type: {artifact_type!r}")
    resolved = status or INITIAL_STATUS[artifact_type]
    if resolved not in valid_statuses(artifact_type):
        raise ValueError(f"status {resolved!r} is not valid for {artifact_type!r}")
    return resolved


def require_known_medium(medium: str) -> str:
    """Reject an embodiment medium the domain does not define (CF-48)."""
    if medium not in ARTIFACT_MEDIA:
        raise ValueError(f"unknown medium: {medium!r}")
    return medium


#: Parameter names `supersession_cypher` binds. The repository must supply these.
SUPERSESSION_PARAMS: dict[str, Any] = {
    "terminal": sorted(TERMINAL_ANY),
}


def supersession_cypher(new: str = "new", old: str = "old") -> str:
    """Emit the legality predicate for one artifact superseding another (CF-48).

    Supersession is atomic by necessity -- the SUPERSEDES edge and the status move go together or
    neither does, or the graph can hold an edge pointing at an artifact still marked APPROVED. So
    the MUTATION belongs in one Cypher statement. That was never a reason for the RULE to live
    there too, which is the distinction this finding turns on.

    The rule, stated once: same type, not itself, the superseded artifact is not already in a
    terminal state, and the namespaces are compatible. `TERMINAL_ANY` is the domain's own set, so
    adding a terminal status now reaches this predicate instead of needing a second edit in
    infrastructure that nobody would remember to make.

    The fragment is a constant: `new` and `old` are Cypher variable names this codebase chooses,
    never caller input.
    """
    return "\n              AND ".join([
        f"{new}.artifact_type = {old}.artifact_type",
        f"{new}.artifact_uuid <> {old}.artifact_uuid",
        f"NOT {old}.status IN $terminal",
        f"{old}.namespace IN [{new}.namespace, $default_ns]",
    ])


def legal_next_statuses(artifact_type: str, from_status: str) -> frozenset[str]:
    """Every status reachable in one step from here.

    Exists so a refused transition can tell the caller what *is* legal. A bare
    "not allowed" makes someone guess, and guessing at a state machine means
    trying each state until one sticks.
    """
    if artifact_type not in _FORWARD:
        return frozenset()
    return frozenset(
        target
        for target in valid_statuses(artifact_type)
        if can_transition(artifact_type, from_status, target)
    )


#: Informal `Status:` headers found across the existing ~98-artifact corpus,
#: mapped to typed states for migration. Anything absent here stays unresolved
#: rather than being guessed -- the Phase A discipline.
STATUS_HEADER_ALIASES: dict[str, str] = {
    "proposal": ArtifactStatus.PROPOSED,
    "proposed": ArtifactStatus.PROPOSED,
    "reviewer-approved": ArtifactStatus.REVIEWED,
    "reviewed": ArtifactStatus.REVIEWED,
    "approved": ArtifactStatus.APPROVED,
    "in progress": ArtifactStatus.IMPLEMENTING,
    "implementing": ArtifactStatus.IMPLEMENTING,
    "implemented": ArtifactStatus.IMPLEMENTED,
    "complete": ArtifactStatus.COMPLETE,
    "completed": ArtifactStatus.COMPLETE,
    "open": ArtifactStatus.OPEN,
    "draft": ArtifactStatus.DRAFT,
    # The documented wrapup vocabulary. PARTIAL and BLOCKED both mean "not
    # finished", which is DRAFT; they are not merged into one status because the
    # raw header is retained and still distinguishes them.
    "ready for review": ArtifactStatus.READY_FOR_REVIEW,
    "partial": ArtifactStatus.DRAFT,
    "blocked": ArtifactStatus.DRAFT,
    "superseded": ArtifactStatus.SUPERSEDED,
    "deferred": ArtifactStatus.DEFERRED,
}


def status_from_header(raw: str | None, artifact_type: str) -> tuple[str | None, str | None]:
    """Map an informal ``Status:`` header to a typed state.

    Returns ``(status, unresolved_reason)``. A header that maps to a status the
    type cannot hold -- ``DRAFT`` on a plan, say -- is reported unresolved
    rather than coerced, because coercing would silently invent history.

    Real headers carry commentary: ``**SUPERSEDED** by menhir-todo-links``,
    ``APPROVED as the basis for future implementation``. The rule is that the
    *leading* token is the state and the remainder is prose, so matching walks
    from the whole string inward to the first word and stops at the first hit.
    It never scans further in -- a state named mid-sentence is being discussed,
    not declared, and picking it up would be inference.
    """
    if not raw or not raw.strip():
        return None, "no_status_header"
    key = re.sub(r"[*_`]", "", raw).strip().lower()
    for delimiter in ("(", "—", "–", " - ", ",", ";", ":"):
        key = key.split(delimiter)[0]
    key = " ".join(key.split())
    if not key:
        return None, "no_status_header"

    words = key.split(" ")
    mapped = None
    for width in (len(words), 2, 1):
        candidate = " ".join(words[:width])
        mapped = STATUS_HEADER_ALIASES.get(candidate)
        if mapped is not None:
            break
    if mapped is None:
        return None, "unrecognized_status"
    if mapped not in valid_statuses(artifact_type):
        return None, "status_invalid_for_type"
    return mapped, None


#: Artifact-to-artifact relations, each with the source and target types it
#: permits. Typed constraints live here rather than in a query so an illegal
#: pairing -- a plan "reviewing" another plan -- is refused by the model rather
#: than silently written.
#:
#: Cypher cannot parameterize a relationship type, so this whitelist is also the
#: injection guard: a relation outside it never reaches a query string.
#:
#: SUPERSEDES is deliberately absent. It is the evidence half of a lifecycle
#: change and is created only by ``supersede_artifact``, which also moves the
#: superseded artifact's status -- an edge alone must never imply a status
#: change it did not make.
ARTIFACT_RELATIONS: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    "reviews": (
        "REVIEWS",
        frozenset({ArtifactType.REVIEW}),
        frozenset({
            ArtifactType.PLAN,
            ArtifactType.INVESTIGATION,
            ArtifactType.IMPLEMENTATION_REPORT,
        }),
    ),
    "implements": (
        "IMPLEMENTS",
        frozenset({ArtifactType.IMPLEMENTATION_REPORT}),
        frozenset({ArtifactType.PLAN}),
    ),
    # A handoff informs the plan the next agent works from, the same shape as
    # an investigation feeding one. No other relation is opened up for it yet:
    # the corpus shows no handoff reviewing or implementing anything.
    "informs": (
        "INFORMS",
        frozenset({ArtifactType.INVESTIGATION, ArtifactType.HANDOFF}),
        frozenset({ArtifactType.PLAN}),
    ),
}

#: Supersession is same-type only: a review cannot supersede a plan. Different
#: types answer different questions, so one never replaces the other.
SUPERSEDES_EDGE = "SUPERSEDES"

#: Relations to objects outside the artifact class. ABOUT targets an existing
#: semantic :Entity rather than a bespoke :Subject label -- "OAuth" already
#: exists as one, and minting a second identity for the same concept is the
#: failure `model.operational_vs_semantic` exists to prevent.
ABOUT_EDGE = "ABOUT"
REFERENCES_TODO_EDGE = "REFERENCES_TODO"


def relation_is_legal(relation: str, source_type: str, target_type: str) -> tuple[bool, str | None]:
    """Whether ``source_type -[relation]-> target_type`` is permitted.

    Returns ``(ok, reason)``; the reason distinguishes an unknown relation from
    a known relation used between the wrong types, because they are different
    caller mistakes.
    """
    spec = ARTIFACT_RELATIONS.get(relation)
    if spec is None:
        return False, "unsupported_relation"
    _edge, sources, targets = spec
    if source_type not in sources:
        return False, "illegal_source_type"
    if target_type not in targets:
        return False, "illegal_target_type"
    return True, None


class QuestionStatus:
    OPEN = "open"
    ANSWERED = "answered"
    DEFERRED = "deferred"


QUESTION_STATUSES: frozenset[str] = frozenset({
    QuestionStatus.OPEN,
    QuestionStatus.ANSWERED,
    QuestionStatus.DEFERRED,
})

#: An :OpenQuestion is an Owned Record that is nonetheless *addressable*: a
#: review must be able to say which question it answered. Being referenceable is
#: not the same as having semantic identity -- it still never surfaces in
#: recall, carries no meaning alone, and dies with its artifact.
#:
#: Answering requires naming what answered it, mirroring RESOLVES_TODO: an
#: answered question with no answering artifact is a claim with no evidence.
#: Deferring needs no evidence, because deferring is a decision rather than an
#: answer.
ANSWERS_QUESTION_EDGE = "ANSWERS_QUESTION"


class DeclarationKind:
    """What class of thing a frontmatter key points at."""

    ARTIFACT_RELATION = "artifact_relation"
    SUPERSESSION = "supersession"
    SUBJECT = "subject"
    TODO = "todo"


#: Frontmatter keys menhir will transcribe, mapped to the kind of target they
#: name. Menhir never infers relationships from prose; it materializes ones a
#: human declared in structured metadata. That is not the CONCERNS mistake --
#: CONCERNS guessed intent from running text, this transcribes a declaration.
#:
#: `supersedes` is separated from the plain relations because supersession is a
#: lifecycle change, not just an edge: honoring it moves the old artifact's
#: status, and that must go through ``supersede_artifact``.
DECLARATION_KEYS: dict[str, str] = {
    "reviews": DeclarationKind.ARTIFACT_RELATION,
    "implements": DeclarationKind.ARTIFACT_RELATION,
    "informs": DeclarationKind.ARTIFACT_RELATION,
    "supersedes": DeclarationKind.SUPERSESSION,
    "about": DeclarationKind.SUBJECT,
    "todos": DeclarationKind.TODO,
}


class DeclarationStatus:
    """Outcome of resolving one declaration.

    Three states rather than two, because "no such target" and "target found but
    the relationship is illegal" are different author mistakes and want different
    fixes. Collapsing them would send someone hunting for a missing file that is
    sitting right there.
    """

    RESOLVED = "resolved"      # target identified and the edge exists
    UNRESOLVED = "unresolved"  # no target, or more than one
    REJECTED = "rejected"      # target identified, relationship refused


@dataclass(frozen=True)
class DeclaredReference:
    """One raw frontmatter entry, before any resolution is attempted.

    ``raw_target`` is stored verbatim and never overwritten by resolution. A
    declaration that cannot be resolved today is retained as unresolved rather
    than dropped: dropping it would let a rename silently delete a relationship,
    and the raw text is the only evidence of what the author meant.
    """

    key: str
    raw_target: str
    ordinal: int
    kind: str


def normalize_declarations(
    declarations: dict[str, object] | None,
) -> tuple[list[DeclaredReference], list[dict[str, str]]]:
    """Flatten a frontmatter mapping into ordered declarations.

    Returns ``(references, ignored)``. Unknown keys are reported rather than
    silently skipped -- frontmatter carries plenty of unrelated metadata, and a
    caller deserves to know which of its keys menhir did not act on.

    Ordinal is global across the mapping, not per key, so the author's ordering
    survives a round trip.
    """
    references: list[DeclaredReference] = []
    ignored: list[dict[str, str]] = []
    ordinal = 0

    for key, value in (declarations or {}).items():
        normalized_key = str(key).strip().lower()
        kind = DECLARATION_KEYS.get(normalized_key)
        if kind is None:
            ignored.append({"key": str(key), "reason": "unsupported_declaration_key"})
            continue
        targets = value if isinstance(value, (list, tuple)) else [value]
        for target in targets:
            text = str(target).strip() if target is not None else ""
            if not text:
                continue
            references.append(
                DeclaredReference(
                    key=normalized_key,
                    raw_target=text,
                    ordinal=ordinal,
                    kind=kind,
                )
            )
            ordinal += 1

    return references, ignored


@dataclass(frozen=True)
class ArtifactSourceSpec:
    """One embodiment of an artifact — the thing that carries the bytes.

    The file is the embodiment; ``locator`` merely says how to reach it now, so
    a rename or archive updates the locator and leaves identity and embodiment
    intact (`model.embodiment_invariant`).

    ``version`` is the embodiment's current revision handle -- ONE value, never
    a history. History belongs to the versioning system that owns it.
    ``integrity`` is optional and medium-dependent: for Git the version handle
    IS a content hash, so one value serves both legs.
    """

    medium: str
    locator: dict[str, str]
    version: str | None = None
    integrity: str | None = None

    def as_properties(self) -> dict[str, object]:
        props: dict[str, object] = {
            "medium": self.medium,
            "version": self.version,
            "integrity": self.integrity,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
        }
        # Locator legs are medium-specific; store them flattened so Cypher can
        # filter on repository/path without unpacking a map property.
        for key, value in (self.locator or {}).items():
            props[f"locator_{key}"] = value
        return props
