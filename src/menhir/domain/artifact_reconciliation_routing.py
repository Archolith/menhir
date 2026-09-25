"""Registration routes.

How a repo-relative path maps to a directory's artifact type and corpus
lane, plus the small path classifiers the scanner uses.
"""

from dataclasses import dataclass

from menhir.domain.artifact_reconciliation_model import CorpusLane
from menhir.domain.work_artifact import ArtifactMedium, ArtifactType


# ---------------------------------------------------------------------------
# Registration routes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusRoute:
    """One directory's routing rule.

    ``directory`` matches files sitting *directly* in it, not recursively --
    recursion happens by declaring the child directory as its own route. That is
    the difference from the old one-level ``DIR_TYPES`` scan: subdirectories are
    covered because they are named, not because a glob swallowed them, so a new
    unnamed subdirectory is reported rather than silently typed by its parent.
    """

    directory: str
    artifact_type: str | None
    lane: str
    #: Archive/reference routes never retype an artifact that already exists.
    preserve_existing_type: bool = False
    #: Reference has no single type; a new record there must declare one.
    requires_declared_type: bool = False


#: Ordered longest-directory-first so ``plans/backlog`` is tested before ``plans``.
CORPUS_ROUTES: tuple[CorpusRoute, ...] = (
    CorpusRoute(".agent/plans/backlog", ArtifactType.PLAN, CorpusLane.BACKLOG),
    CorpusRoute(".agent/plans", ArtifactType.PLAN, CorpusLane.ACTIVE),
    CorpusRoute(".agent/reviews", ArtifactType.REVIEW, CorpusLane.ACTIVE),
    CorpusRoute(".agent/handoffs", ArtifactType.HANDOFF, CorpusLane.ACTIVE),
    CorpusRoute(
        ".agent/for-review", ArtifactType.IMPLEMENTATION_REPORT, CorpusLane.ACTIVE
    ),
    CorpusRoute(
        ".agent/archive/plans",
        ArtifactType.PLAN,
        CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    CorpusRoute(
        ".agent/archive/reviews",
        ArtifactType.REVIEW,
        CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    CorpusRoute(
        ".agent/archive/handoffs",
        ArtifactType.HANDOFF,
        CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    #: Archived wrapups. `.agent/for-review` (the active lane) routes to
    #: IMPLEMENTATION_REPORT, and archiving does not change what the document is.
    #: Without this route the gateway's own archive destination falls out of the
    #: corpus entirely -- `route_for_path` returns no match and `build_entry` None.
    CorpusRoute(
        ".agent/archive/wrapups",
        ArtifactType.IMPLEMENTATION_REPORT,
        CorpusLane.ARCHIVE,
        preserve_existing_type=True,
    ),
    CorpusRoute(
        ".agent/reference",
        None,
        CorpusLane.REFERENCE,
        preserve_existing_type=True,
        requires_declared_type=True,
    ),
)

#: Filenames that route documents rather than being work. Excluded from the
#: corpus, not reported as unclassified: an index is not a missing artifact.
INDEX_FILENAMES: frozenset[str] = frozenset({"README.md", "index.md"})

#: Extensions the scanner will consider, mapped to their medium.
MEDIA_BY_SUFFIX: dict[str, str] = {
    ".md": ArtifactMedium.MARKDOWN,
    ".pdf": ArtifactMedium.PDF,
    ".html": ArtifactMedium.HTML,
}


def route_for_path(rel_path: str) -> CorpusRoute | None:
    """The route owning this repo-relative path, or None if it is outside the corpus."""
    normalized = (rel_path or "").replace("\\", "/").strip("/")
    if not normalized:
        return None
    parent, _, filename = normalized.rpartition("/")
    if not filename:
        return None
    for route in CORPUS_ROUTES:
        if parent == route.directory:
            return route
    return None


def is_index_document(rel_path: str) -> bool:
    normalized = (rel_path or "").replace("\\", "/")
    return normalized.rsplit("/", 1)[-1] in INDEX_FILENAMES


def medium_for_path(rel_path: str) -> str | None:
    normalized = (rel_path or "").replace("\\", "/").lower()
    _, dot, suffix = normalized.rpartition(".")
    if not dot:
        return None
    return MEDIA_BY_SUFFIX.get(f".{suffix}")
