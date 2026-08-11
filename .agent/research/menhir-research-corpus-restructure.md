# menhir research-corpus restructure — design spec

**Status:** IMPLEMENTED (2026-06-29) — historical design record. Open decisions were approved in
commit `ecad548`; implementation shipped across `8729389` through `dab8bb9` (cluster moves, link
rewrite, master/cluster indexes, status normalization, roadmap index, plan triage, and changelog).
**Author:** Claude Code session 2026-06-29
**Scope:** `docs/research/`, `docs/roadmap/`, `.agent/plans/` (menhir-frontier worktree, branch `claude/menhir-chain-handoff-doc-7iuat2`)
**Hard constraint:** retain ALL information. No deletions, no body-content rewrites. Every move via `git mv` (history preserved). The only edits to doc bodies are status-header normalization (section D), each evidence-backed.

---

## 1. Problem

The research corpus has a good governance framework (`docs/research/README.md`: controlled status vocabulary, anti-sprawl rules, a superseded-docs section) but the corpus has **drifted away from its own index**:

1. **Two large docs are unregistered in the index entirely:**
   - `intent-warden.md` (22 KB) — the IntentOracle design. Absent from Canonical / Speculative / reading-order / parked tables. Its own header still says *"design only. No implementation"* — but `IntentOracle` graduated and shipped into `default_oracles()` (chain-handoff `c979ca4`, `dcf795e`). Doubly stale.
   - `llm-reviewer-seams.md` (29 KB, 2026-06-29) — "Where should a bounded LLM reviewer exist in Menhir?" Referenced nowhere in the index.
2. **Status headers don't follow the index's own controlled vocabulary.** `archolith-bench-operational-model.md` and `oracle-architecture.md` have no parseable `## Status`; `research-process.md`'s status section is the taxonomy list itself; `intent-warden.md`, `llm-reviewer-seams.md`, `semantic-operating-system.md`, `research-vs-shipped-inventory.md` use freeform prose where a label belongs.
3. **Stale statuses lag the code reality.** Frontier wired the oracle pipeline into recall (`4450395`, `3bac9b5`, `30c58d0`), yet `oracle-amplified-retrieval.md` / `oracle-runtime-interfaces.md` are still `speculative`. The reading-order cluster list omits both new docs.
4. **`docs/roadmap/` has no index** and mixes altitudes (MVP plans, strategic notes, sketches); `doc-drift-watch-mvp.md` and `org-scale-menhir.md` aren't in the research corpus map at all.
5. **`.agent/plans/` mixes living docs with consumed session artifacts** (a dated live-verification handoff, a concluded query-profile evaluation).

## 2. Goals / Non-goals

**Goals**
- Re-cluster `docs/research/` into themed subdirectories matching the index's existing reading-order clusters (0–5), so structure formalizes what the README already implies.
- Register the two orphaned docs; correct stale/freeform status headers to the controlled vocabulary.
- Give `docs/roadmap/` an index grouped by altitude (no file moves).
- Triage `.agent/plans/`: archive consumed session artifacts, keep living plans.
- Guarantee zero broken links and zero lost content via `git mv`, full link rewrite, a link-check pass, and a before→after manifest.

**Non-goals**
- No content rewrites of doc bodies (status-header line only, per D).
- No deletions of any kind.
- No new research claims, no promotion of any concept, no code changes.
- No re-litigation of settled decisions (chain-handoff §8).

## 3. Target layout — `docs/research/`

Mapped to the index reading-order clusters. Every current file is accounted for.

```
docs/research/
  README.md                      # master index, rewritten to point into clusters
  direction/                     # cluster 0 — architectural synthesis, read first
    README.md
    semantic-operating-system.md
    oracle-architecture.md
    llm-reviewer-seams.md        # NEWLY registered (OD-2: structural-architecture synthesis, not pure retrieval)
  process/                       # cluster 1 — how research/eval works
    README.md
    research-process.md
    archolith-bench-operational-model.md
    research-vs-shipped-inventory.md
  positioning/                   # cluster 2
    README.md
    positioning.md
  retrieval/                     # cluster 3 — candidate -> oracle -> combine -> rails
    README.md
    retrieval-tuning-stack.md
    facet-retrieval.md
    facet-extraction-plan.md
    oracle-amplified-retrieval.md
    oracle-runtime-interfaces.md
    oracle-execution-and-performance.md
    retrieval-control-rails.md
    intent-warden.md             # NEWLY registered
  schemas/                       # cluster 3 — L3/L4 spec-only schemas (DEFAULT: own subdir; see OD-1)
    README.md
    layer4-knowledge-artifacts.md
    cold-start-brief.md
  belief-temporal/               # cluster 4
    README.md
    belief-layer.md
    connected-data-substrates.md
    tracehead-braidtrace.md
  vision/                        # cluster 5
    README.md
    cognitive-replay-and-phasing.md
  archive/                       # superseded — kept as pointers, NEVER deleted
    README.md
    probabilistic-belief-layer.md
    probabilistic-circuit-breakers.md
    agent-experience-substrate.md
    cognitive-artifacts-and-software-cognition.md
    cognitive-infrastructure-platform.md
```

Each subdir `README.md` is 5–10 lines: cluster purpose, doc list with one-line summary + status. The master `README.md` keeps the governance sections (status vocabulary, anti-sprawl rules, promotion ladder, durable save list) and replaces the flat Canonical/Speculative tables with a cluster-indexed view that links into each subdir.

## 4. Target layout — `docs/roadmap/` (index only, no moves)

New `docs/roadmap/README.md` grouping the 6 existing files by altitude:
- **Active build sequencing:** `weekend-oracle-runtime-roadmap.md`, `oracle-integration-plan.md`
- **L3/L4 GAP decision-support (proposals, not rungs):** `l3l4-overlay-sequencing-options.md`, `l3l4-hybrid-sketch.md`
- **Strategic notes (not rungs):** `org-scale-menhir.md`, `doc-drift-watch-mvp.md`

## 5. `.agent/plans/` triage

Move to existing `.agent/archive/plans/` via `git mv` (history preserved):
- `session-handoff-2026-06-28-live-verification.md` — dated, consumed session handoff.
- `menhir-query-profile-evaluation.md` — evaluation concluded ("composition-only verdict", recent commits `39d07de`/`976b0eb`).

Keep in place (living): `chain-handoff.md`, `deferred-verification.md`, `menhir-research-execution-ladder.md`, `r1-hybrid-candidate-generation.md`, `r2-facet-candidate-generation.md`, `menhir-intent-oracle-plan.md`, and this spec.

## 6. Status-header normalization (section D)

Bring every doc to the index's controlled vocabulary. Corrections are evidence-backed against chain-handoff + code reality; bodies are otherwise untouched.

| Doc | Current header | New status | Evidence |
|---|---|---|---|
| `intent-warden.md` | "design only. No implementation" | `supported-by-eval` | Bench graduated embedder-invariant (`d3811a2`); `IntentOracle` in `default_oracles()` (`c979ca4`), domain port (`dcf795e`) |
| `oracle-amplified-retrieval.md` | `speculative` | `supported-by-spike` | Oracle bench built (38 tests); combiner wired into recall (`30c58d0`, `3bac9b5`) |
| `oracle-runtime-interfaces.md` | `speculative` | `supported-by-spike` | Interfaces drafted; AssertionPipeline wired observe-only (`4450395`) |
| `archolith-bench-operational-model.md` | (none) | `canonical` | Index already lists it canonical |
| `oracle-architecture.md` | (none) | `active` | Index already lists it active (direction) |
| `research-process.md` | (taxonomy list) | `canonical` | Index already lists it canonical |
| `semantic-operating-system.md` | freeform prose | `active` | Index lists it active (direction); keep descriptive note below the label |
| `research-vs-shipped-inventory.md` | freeform prose | `canonical (snapshot)` | Index label; keep the audited-date note |

## 7. Safety protocol — nothing gets lost

1. Every move is **`git mv`** — full history preserved, reviewable as renames.
2. **Zero deletions; zero body edits** except the status-header lines in section 6.
3. Rewrite **all** intra-corpus and external relative links to new paths. Known referrers (counts from grep): `research/README.md` (33), `.agent/plans/chain-handoff.md` (50), `menhir-research-execution-ladder.md` (10), plus inter-doc links in roadmap/research and `.agent/` operational docs.
4. **Link-check pass** after rewrite: enumerate every `.md` link target in the corpus and confirm each resolves to an existing file. Report fixed-count and confirm 0 dangling.
5. **Before→after manifest in the commit body** (OD-4 — no tracked file, to avoid transient metadata going stale in-tree): every file in scope, old path → new path, so the move is fully auditable and reversible from `git log`. Also reproduced in the session wrapup.

## 8. Verification

- `git status` / `git diff --name-status` shows only renames + the status-header + index edits — no content churn in moved bodies (confirm with `git diff -M` rename detection).
- Link-check pass: 0 dangling `.md` targets across `docs/` and `.agent/`.
- Manifest row count == file count moved.
- `query_structure` re-ingest is NOT required (docs not code), but note the structure watcher may re-scan.

## 9. Rollout (commit strategy)

Staged commits on the current branch (`claude/menhir-chain-handoff-doc-7iuat2`), each conventional-commit prefixed `docs:`:
1. `docs: re-cluster research corpus into themed subdirs (git mv only)` — the moves + subdir READMEs; the before→after manifest goes in this commit's body.
2. `docs: rewrite cross-links for research-corpus restructure` — all link rewrites.
3. `docs: normalize research-doc status headers + register intent-warden, llm-reviewer-seams` — section 6 + master index rewrite.
4. `docs: add roadmap index + triage .agent/plans archive` — sections 4 and 5.

CHANGELOG entry added per maintenance rules. Commit only files touched by this work; explicit paths; no `git add -A`.

## 10. Open decisions — RESOLVED (user, 2026-06-29)

- **OD-1 — `schemas/` as its own subdir?** RESOLVED: **yes, own subdir.** `layer4-knowledge-artifacts.md` + `cold-start-brief.md` are passive data structures and belong in a separate structural space, not the active `retrieval/` pipeline.
- **OD-2 — `llm-reviewer-seams.md` home.** RESOLVED: **`direction/`.** Bounded-LLM-reviewer seams lean toward structural-architecture synthesis rather than pure retrieval mechanics.
- **OD-3 — status corrections (section 6).** RESOLVED: **all approved** as written (evidence-backed against index + commits).
- **OD-4 — manifest location.** RESOLVED: **commit body + wrapup only**, no tracked `RESTRUCTURE-MANIFEST.md` (avoids transient metadata going stale in-tree).

**Design approved for implementation (user: "reorg is approved for go", 2026-06-29).**

## 11. Risks

- **Link rewrite is the main risk.** Mitigated by the link-check pass (step 7.4) — a broken link is caught before commit, and content is never at risk because no file is deleted.
- **chain-handoff.md is static by its own rule** ("This doc is static"). Its 50 references are paths, not claims; rewriting paths is allowed and required. The "Last updated" line is bumped only if a path-bearing section changes.
- **Cross-worktree:** changes land on the frontier branch only; `menhir` main is untouched (it is 31 behind and carries no research corpus changes of its own).
