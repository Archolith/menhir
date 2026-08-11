# menhir-frontier: Research Corpus Restructure Plan Review

**Status:** HISTORICAL / ACCEPTED. The four open decisions were resolved in `ecad548`; the reviewed
design and implementation shipped on 2026-06-29 across `8729389` through `dab8bb9`.
**Reviewed Plan:** `.agent/research/menhir-research-corpus-restructure.md`
**Project:** `menhir-frontier`

## Executive Summary
The plan is exceptionally well-structured, safe, and adheres perfectly to the constraints (no deletions, git mv only, history preservation). The file accounting is perfectly accurate—all 27 markdown files in `docs/research` are accounted for in the target layout. The plan to use a link-checking pass before committing is a critical and excellent safeguard.

The plan is **APPROVED** to proceed, subject to the resolutions of the Open Decisions below.

## Open Decisions (Feedback)

### OD-1: `schemas/` as its own subdir?
**Recommendation:** **Keep as its own subdir (Default)**.
*Rationale:* `layer4-knowledge-artifacts.md` and `cold-start-brief.md` define passive data structures and schemas. Folding them into `retrieval/` conflates passive structural definitions with the active pipeline (candidate generation, oracle evaluation). A dedicated `schemas/` directory correctly separates these concerns.

### OD-2: `llm-reviewer-seams.md` home
**Recommendation:** **Place in `direction/` (Alternative)**.
*Rationale:* A discussion on "where a bounded LLM reviewer should exist" is fundamentally a system architecture and boundaries discussion. While oracles (which are in `retrieval/`) act as reviewers, the *seams* and structural placement are high-level architectural decisions that align better with the synthesis found in `direction/`. If the document is purely implementation details of the oracle boundaries, `retrieval/` is fine, but the title implies structural architecture.

### OD-3: Status corrections (Section 6)
**Recommendation:** **Proceed as proposed**.
*Rationale:* The proposed status updates are strictly backed by the evidence provided (e.g., `intent-warden.md` moving to `supported-by-eval` due to `IntentOracle` graduation). The mapping from freeform prose to the controlled vocabulary (`canonical`, `active`, `supported-by-spike`) is accurate.

### OD-4: Manifest location
**Recommendation:** **Commit body only (Alternative)**.
*Rationale:* A `RESTRUCTURE-MANIFEST.md` file tracked in `docs/research/` becomes stale the moment subsequent file moves happen. It is point-in-time metadata about a migration. Placing the manifest in the commit body (and the wrapup document) ensures it is permanently immutable and tied directly to the `git mv` operations, without cluttering the documentation tree with transient migration artifacts.

## Additional Notes
- The strategy to triage `.agent/plans/` by moving consumed artifacts to `.agent/archive/plans/` using `git mv` is correct and follows workspace standards.
- Ensure that the link rewrite pass also checks for links in the codebase (e.g., source code comments pointing to `docs/research/...`) if applicable, though the plan appropriately focuses on intra-corpus links.
