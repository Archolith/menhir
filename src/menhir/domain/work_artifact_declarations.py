"""Frontmatter declaration vocabulary and normalization, split from ``domain/work_artifact.py``.

Part of the ``work_artifact`` facade: the parent module re-exports every public
name defined here, so callers keep importing from ``menhir.domain.work_artifact``.
"""

from __future__ import annotations

from dataclasses import dataclass


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
