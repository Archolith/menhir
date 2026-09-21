"""Build the fixture WorkArtifact corpus E2E-4 reconciles.

E2E-4 needs a repository whose ``.agent/`` tree holds documents the corpus routes
recognize, with known types and statuses, and a Git history real enough that a moved
file produces rename evidence rather than a delete plus an add.

WHY GENERATED, AND WHY COMMITTED TO GIT
---------------------------------------
Generated for the same reason the code fixture is: a checked-in corpus drifts as people
tidy it, and the expectations in the lane quietly stop matching. Committed to Git
because the reconciler reads Git history for its rename evidence -- ``MatchBasis`` ranks
``GIT_RENAME`` above ``EXACT_LOCATOR`` precisely so a moved file is recognized as the
same artifact. A corpus sitting in an untracked directory would force the matcher onto
its weakest basis, ``UNIQUE_CONTENT_SHA256``, and the stable-UUID criterion would be
testing the fallback rather than the path that runs in practice.

THE ROUTES THIS EXERCISES
-------------------------
``CORPUS_ROUTES`` maps a directory to a type and a lane::

    .agent/plans        -> plan,                  active
    .agent/reviews      -> review,                active
    .agent/for-review   -> implementation_report, active
    .agent/archive/plans-> plan,                  archive

One document of each, so the lane can assert that routing assigns types from location
rather than guessing from prose. ``README.md`` is included deliberately and must NOT
become an artifact: ``INDEX_FILENAMES`` excludes it, and an index counted as a missing
artifact is a false finding that a corpus audit would report forever.

STATUSES ARE AUTHORED IN THE DOCUMENT
-------------------------------------
``_STATUS_RE`` reads a ``Status:`` line from the first 40 body lines, and
``status_from_header`` maps it. Each fixture document therefore declares one, in the
informal spelling real documents use, so the lane exercises the parser rather than a
frontmatter field the corpus rarely carries.

``UNPARSEABLE_STATUS_PATH`` declares a status no alias covers. That document must be
registered with its type's initial status AND flagged unresolved -- "nobody could read
the header" has to stay distinguishable from "this genuinely is PROPOSED", or every
audit conflates an unreadable document with a new one.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ArtifactCorpus",
    "MALFORMED_PATH",
    "MALFORMED_DOCUMENT",
    "EXPECTED_ARTIFACTS",
    "INDEX_PATH",
    "MOVE_DESTINATION",
    "MOVE_SOURCE",
    "OUTSIDE_CORPUS_PATH",
    "UNPARSEABLE_STATUS_PATH",
    "build_artifact_corpus",
    "move_document",
    "write_malformed_document",
]

#: Repo-relative path -> (artifact_type, expected typed status). The lane asserts these
#: against what reconciliation registered, so a routing or parsing change shows up as a
#: failing expectation rather than as a silently different corpus.
EXPECTED_ARTIFACTS: dict[str, tuple[str, str]] = {
    ".agent/plans/shop-refund-threshold.md": ("plan", "PROPOSED"),
    ".agent/plans/shop-storage-batching.md": ("plan", "APPROVED"),
    ".agent/reviews/shop-refund-threshold-review.md": ("review", "OPEN"),
    ".agent/for-review/WRAPUP-shop-refund-threshold.md": (
        "implementation_report",
        "READY_FOR_REVIEW",
    ),
    ".agent/archive/plans/shop-legacy-pricing.md": ("plan", "SUPERSEDED"),
}

#: An index, not work. Must not be registered as an artifact.
INDEX_PATH = ".agent/plans/README.md"

#: A document whose Status header no alias covers. Must register unresolved rather than
#: being coerced into a state nobody declared.
UNPARSEABLE_STATUS_PATH = ".agent/plans/shop-unclear-status.md"

#: Markdown outside every corpus route. Must be ignored entirely -- not registered, and
#: not reported as an unclassified document needing attention.
OUTSIDE_CORPUS_PATH = "docs/overview.md"

#: A document whose frontmatter declares metadata the parser must reject. Written only
#: by :func:`write_malformed_document`, so the ordinary corpus stays clean and the
#: adversarial case is opt-in per lane.
MALFORMED_PATH = ".agent/plans/shop-malformed-metadata.md"

MALFORMED_DOCUMENT = """---
artifact_uuid: definitely-not-a-uuid
artifact_type: strategy
corpus_lane: active
---
# A plan whose declared metadata is wrong in three ways

**Status:** Proposed

`artifact_uuid` is not a UUID, `artifact_type` is not a known type, and `corpus_lane` is
a derived key an author must never write. Each is rejected separately, and none of them
may stop the rest of the corpus from reconciling.
"""

#: The stable-UUID test moves this file with `git mv` and re-reconciles. Both paths stay
#: inside `.agent/plans`, so the route and type do not change and the only thing under
#: test is whether identity survived the move.
MOVE_SOURCE = ".agent/plans/shop-storage-batching.md"
MOVE_DESTINATION = ".agent/plans/shop-storage-batching-v2.md"


_DOCUMENTS: dict[str, str] = {
    ".agent/plans/README.md": """\
# Plans index

This file routes documents. It is not itself work, and must never be registered as an
artifact -- `INDEX_FILENAMES` excludes it.

- `shop-refund-threshold.md`
- `shop-storage-batching.md`
""",
    ".agent/plans/shop-refund-threshold.md": """\
# Raise the shop refund approval threshold

**Status:** Proposed

## Problem

The refund approval threshold is hard-coded at 500 dollars in `src/shop/service.py`.
Support cannot approve routine refunds without escalating, and the escalation queue is
where refunds go to age.

## Proposal

Move the threshold into `config.py` and raise it to 750. Keep the escalation path for
anything above it.

## Acceptance

- The threshold is read from configuration, not a literal.
- Refunds at or below the threshold need no escalation.
- The existing storage tests still pass unchanged.
""",
    ".agent/plans/shop-storage-batching.md": """\
# Batch the shop storage writer

**Status:** Approved

## Problem

`OrderStore.put` writes synchronously on every call. Under load the api layer spends
most of its time waiting on the store.

## Proposal

Add an explicit `flush()` seam and batch writes between flushes. The guardrail in
`docs/overview.md` forbids a background writer, and this proposal does not introduce
one: flushing stays caller-driven.

## Acceptance

- `flush()` exists and is a no-op when nothing is buffered.
- No background thread or task is created.
- `tests/test_storage.py` covers the batch boundary.
""",
    ".agent/plans/shop-unclear-status.md": """\
# Investigate order id opacity

**Status:** Marinating pending further thought

## Problem

`docs/overview.md` says order identifiers are opaque and must never be parsed, but
nothing enforces it.

## Next step

Decide whether this is worth a lint rule or a runtime assertion.
""",
    ".agent/reviews/shop-refund-threshold-review.md": """\
# Review: raise the shop refund approval threshold

**Status:** Open

## Scope

Reviews `.agent/plans/shop-refund-threshold.md`.

## Findings

- The plan does not say what happens to refunds already sitting in the escalation queue
  when the threshold changes. They should not be silently approved.
- Reading the threshold from configuration is right, but the plan does not name the
  configuration key, so two implementations could pick different ones.
""",
    ".agent/for-review/WRAPUP-shop-refund-threshold.md": """\
# WRAPUP: raise the shop refund approval threshold

**Status:** READY FOR REVIEW

## Summary

Implements `.agent/plans/shop-refund-threshold.md`. The threshold moved to
`src/shop/config.py` and now reads 750.

## Files Changed

- `src/shop/config.py`
- `src/shop/service.py`

## Verification

- `pytest tests/test_service.py` PASS
- `pytest tests/test_storage.py` PASS

## Risks/Gaps

Refunds already in the escalation queue are untouched, per the review finding.
""",
    ".agent/archive/plans/shop-legacy-pricing.md": """\
# Legacy per-region pricing

**Status:** Superseded by the flat-rate pricing decision

## Problem

Pricing varied by region through a lookup table nobody maintained.

## Outcome

Abandoned in favour of flat-rate pricing. Kept for the rationale, not for execution.
""",
    "docs/overview.md": """\
# Overview

Prose that lives outside every corpus route. The reconciler must ignore it rather than
report it as an unclassified document.
""",
    "README.md": """\
# artifact-fixture

A repository whose `.agent/` tree is the E2E-4 fixture corpus.
""",
}


@dataclass(frozen=True)
class ArtifactCorpus:
    """A built fixture corpus and the identity evidence for the run manifest."""

    path: Path
    repository: str
    head_commit: str
    content_digest: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "corpus_path": str(self.path),
            "corpus_repository": self.repository,
            "corpus_head": self.head_commit,
            "corpus_content_sha256": self.content_digest,
        }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=_git_env(),
    ).stdout.strip()


def _git_env() -> dict[str, str]:
    """A committer identity that does not depend on the developer's global config.

    A machine with no ``user.email`` set fails ``git commit`` outright, and the failure
    surfaces as an unrelated fixture error long after the cause.
    """

    import os

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "menhir-e2e",
            "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
            "GIT_COMMITTER_NAME": "menhir-e2e",
            "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
        }
    )
    return env


def build_artifact_corpus(destination: Path, *, repository: str = "artifact-fixture") -> ArtifactCorpus:
    """Write the corpus, commit it, and return its identity.

    Built across three commits rather than one. The reconciler chooses a Git evidence
    interval from a persisted cursor, and a repository with a single root commit gives it
    nothing to reason about -- the rename pass in particular has no history to read.
    """

    destination.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").exists():
        _git(destination, "init", "-q", "-b", "main")

    # Commit 1: the repository exists and says what it is.
    _write(destination, "README.md", _DOCUMENTS["README.md"])
    _write(destination, OUTSIDE_CORPUS_PATH, _DOCUMENTS[OUTSIDE_CORPUS_PATH])
    _git(destination, "add", "README.md", OUTSIDE_CORPUS_PATH)
    _git(destination, "commit", "-q", "-m", "docs: fixture repository scaffolding")

    # Commit 2: the active corpus.
    active = [
        INDEX_PATH,
        ".agent/plans/shop-refund-threshold.md",
        ".agent/plans/shop-storage-batching.md",
        UNPARSEABLE_STATUS_PATH,
        ".agent/reviews/shop-refund-threshold-review.md",
        ".agent/for-review/WRAPUP-shop-refund-threshold.md",
    ]
    for relative in active:
        _write(destination, relative, _DOCUMENTS[relative])
    _git(destination, "add", *active)
    _git(destination, "commit", "-q", "-m", "docs: plans, review and wrapup")

    # Commit 3: the archived lane, added separately so the archive route is reachable
    # from a commit that did not also create the active documents.
    archived = ".agent/archive/plans/shop-legacy-pricing.md"
    _write(destination, archived, _DOCUMENTS[archived])
    _git(destination, "add", archived)
    _git(destination, "commit", "-q", "-m", "docs: archive the legacy pricing plan")

    return ArtifactCorpus(
        path=destination,
        repository=repository,
        head_commit=_git(destination, "rev-parse", "HEAD"),
        content_digest=_digest(),
    )


def write_malformed_document(corpus: ArtifactCorpus) -> str:
    """Add the malformed document to a built corpus and commit it.

    Separate from :func:`build_artifact_corpus` so the ordinary lane never has to reason
    about a conflicting entry, and the adversarial lane gets one deliberately.
    """

    _write(corpus.path, MALFORMED_PATH, MALFORMED_DOCUMENT)
    _git(corpus.path, "add", MALFORMED_PATH)
    _git(corpus.path, "commit", "-q", "-m", "docs: add a document with invalid declared metadata")
    return _git(corpus.path, "rev-parse", "HEAD")


def move_document(corpus: ArtifactCorpus, source: str, destination: str) -> str:
    """``git mv`` a corpus document and commit, returning the new HEAD.

    ``git mv`` rather than a filesystem rename: the reconciler's strongest non-declared
    basis is recorded rename history, and a plain move leaves it guessing from content.
    """

    _git(corpus.path, "mv", source, destination)
    _git(corpus.path, "commit", "-q", "-m", f"docs: move {source} to {destination}")
    return _git(corpus.path, "rev-parse", "HEAD")


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _digest() -> str:
    """A digest over the declared content, so a fixture edit is visible in the manifest."""

    hasher = hashlib.sha256()
    for relative in sorted(_DOCUMENTS):
        hasher.update(relative.encode("utf-8"))
        hasher.update(_DOCUMENTS[relative].encode("utf-8"))
    return hasher.hexdigest()
