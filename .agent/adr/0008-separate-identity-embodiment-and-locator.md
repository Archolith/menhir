# ADR 0008 — Separate Identity, Embodiment, and Locator

- **Status:** ACCEPTED retrospectively (2026-09-21). This records the general modeling rule proven
  by WorkArtifacts and declared Todo locations.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/data_models.md`, `.agent/workflows/artifact_authoring.md`,
  `.agent/plans/menhir-artifact-semantic-model.md`,
  `.agent/plans/menhir-work-artifact-reconciliation-2026-08-11.md`

## Context

Repository paths, page URLs, branches, and line references are convenient identifiers until the
first rename, move, dirty working tree, or alternate representation. Keying a semantic object on its
current locator silently orphans relationships when that locator changes. Normalizing an author's
declaration directly into a relationship creates a second failure: declarations that cannot be
resolved today disappear, making later repair impossible.

Menhir's WorkArtifact and TodoLocation models established a more durable pattern, but it was
documented as a set of modeling primitives rather than an architecture decision.

## Decision drivers

- Renames and moves must preserve semantic identity and relationships.
- One semantic object may have multiple representations or sources.
- Resolution rules change over time and need the original declaration for replay.
- Failure to resolve must remain visible rather than fabricate or discard meaning.
- Structural helper records must not leak into semantic recall merely because they have UUIDs.

## Decision

Menhir models **identity**, **embodiment**, and **locator** as separate concepts:

```text
Identity     what the thing is          stable across moves and representations
Embodiment   one manifestation of it    carries or identifies the bytes/revision
Locator      how to reach it now        mutable operational address
```

The accompanying declaration rule is:

```text
raw declaration -> normalization -> resolution -> durable identity
       kept                           may fail
```

Specifically:

1. **Semantic identity is stable and explicit.** Relationships target the durable object, not its
   current path, URL, title, or display name.
2. **An embodiment records one representation.** Markdown, PDF, wiki, or file sources can represent
   the same semantic object without becoming separate identities.
3. **Locators are mutable observations.** Repository, branch, path, page id, or URI may change while
   identity and source history remain intact.
4. **Integrity, version, and observation are separate facts.** A content digest answers whether
   bytes changed; a blob/revision identifies a version; an observed commit/time says when Menhir
   checked. One value does not stand in for all three.
5. **Author declarations are retained verbatim.** Normalized fields and resolved relationships are
   derived alongside the raw declaration, never in place of it.
6. **Resolution failure is durable state.** Unresolved records keep their reason and ordinal; Menhir
   does not invent a target or drop the declaration.
7. **Addressability does not imply semantic identity.** An owned subordinate such as
   `TodoLocation` may have a UUID for reference while remaining non-semantic, non-recallable, and
   lifecycle-bound to its owner.
8. **Move, copy, and replacement are distinct.** A move updates a locator and keeps identity; a copy
   or replacement receives a new identity, with supersession recorded explicitly when applicable.

## Considered alternatives

### Use repository path or URL as object identity

Rejected. Renames, branch changes, mirrors, and alternate media would either fork or orphan the
same semantic object.

### Keep only normalized declarations

Rejected. Changed normalization rules could not be replayed, and lossy normalization would erase
the author's original intent.

### Drop declarations that do not resolve

Rejected. Absence of a target today is not evidence that the declaration was meaningless; later
ingest or corrected rules may resolve it.

### Turn every addressable record into a semantic Entity

Rejected. Structural components would enter recall, accrue independent lifecycle, and compete with
the object whose meaning they only help locate.

### Infer moves by title or prose similarity

Rejected. Similarity can suggest review but cannot transfer identity. Moves require declared UUIDs
or trustworthy source/version evidence.

## Consequences

- Models need additional records and explicit reconciliation logic.
- File moves preserve artifact history and relationships instead of recreating objects.
- Dirty and committed sources can be represented honestly without inventing a version.
- Unresolved declarations remain queryable repair work rather than silent data loss.
- New models must decide whether a record is semantic, an embodiment, a locator, or an owned
  subordinate before assigning labels and lifecycle.

## Evidence in the repository

- `src/menhir/domain/work_artifact.py`
- `src/menhir/domain/todo_location.py`
- `src/menhir/domain/artifact_reconciliation.py`
- `src/menhir/infrastructure/work_artifact_repository.py`
- `src/menhir/services/artifact_reconciliation_service.py`
- `tests/test_artifact_reconciliation.py`
- `tests/test_todo_location.py`
- `tests/test_work_artifact.py`

## Non-goals

This ADR does not require every object to support multiple embodiments, make fuzzy reconciliation
authoritative, or promote ADR documents into the current WorkArtifact type registry.
