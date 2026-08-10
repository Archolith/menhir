# Plan: Memory supersession chains + redundancy dedup

<!-- Filename convention: <project>-<feature>-plan.md -->

> **Status note 2026-07-11 (code-reconciled): ACTIVE — kept, owner decision pending.**
> Verified against `src/menhir`:
> - **Phase 1 NOT built.** No `classify_relation`; `confirm_contradiction` is still un-generalized
>   (`infrastructure/llm.py:178`, used at `lifecycle_service.py:998`). No `SUPERSEDED_BY` edge and no
>   `set_superseded` anywhere in menhir.
> - **Phase 2 goal met by a DIFFERENT mechanism.** Recall returns current heads with an opt-in
>   `include_superseded` (`api/routes.py:159`, `domain/recall.py:108`, `backend_impl.py:221`), but via
>   the **temporal-belief** path (`expired_at is None` -> `current_belief`/`superseded_belief`,
>   `recall_service.py:190-191`), NOT the node-lineage `SUPERSEDED_BY` edge this plan specifies.
> - **Phase 0** (painscan `consolidate_memory_clusters.py`) lives in `cth.painscan` (separate repo),
>   not verifiable from a menhir session.
>
> **Owner decision needed:** is the `SUPERSEDED_BY` UPDATE-lineage chain (with the 4-way
> `classify_relation`) still wanted, or is it effectively superseded by the shipped temporal
> `expired_at`/`include_superseded` mechanism + judge-gated merge (`ingest-identity-merge-gating`)?
> Kept ACTIVE pending that call — not archived on my own judgment.

## Project Scope

This plan spans **two repos** and must be executed as **two sequenced sessions**
(one project per session — never write across both in one run):

| Field | Value |
|-------|-------|
| **Phase 0 project** | `cth.painscan` (`projects/ctharvey/cth.painscan`) |
| **Phase 1+ project** | `menhir` (`projects/archolith/menhir`) |
| **Base** | `main` (both repos) |

Rationale for ordering: prove the UPDATE-vs-DUPLICATE-vs-CONTRADICTION classifier
and the dedup behavior offline on the painscan candidate **ledger** (cheap,
reversible JSON, no live graph) before porting the validated logic into menhir's
live Neo4j/Graphiti graph as a new `SUPERSEDED_BY` edge.

### In scope

```
# Phase 0 — cth.painscan
projects/ctharvey/cth.painscan/scripts/consolidate_memory_clusters.py   (new)
projects/ctharvey/cth.painscan/painscan/dream.py                        (wire post-merge pass)
projects/ctharvey/cth.painscan/.agent/CHANGELOG.md

# Phase 1+ — menhir
projects/archolith/menhir/src/menhir/infrastructure/consolidation_queries.py
projects/archolith/menhir/src/menhir/infrastructure/correlation_queries.py
projects/archolith/menhir/src/menhir/infrastructure/llm.py
projects/archolith/menhir/src/menhir/domain/recall.py
projects/archolith/menhir/src/menhir/infrastructure/cypher.py (recall head-filter)
projects/archolith/menhir/tests/...
projects/archolith/menhir/.agent/CHANGELOG.md
```

### Out of scope

```
# The other repo in any given session (one project per session).
# Existing tests — do not modify to accommodate changes; stop and report.
# Graphiti vendored internals — extend via menhir adapters, not upstream edits.
# The decay/compression ladder semantics (ACTIVE→COMPRESSED→GONE) — additive only.
```

---

## Compact Summary

Add explicit memory lineage to menhir: a `SUPERSEDED_BY` edge that chains "same
subject, newer info" memories so recall returns only the current head, plus a
redundancy-dedup for identical copies — both driven by one CONTRADICTION /
DUPLICATE / UPDATE / UNRELATED classifier, proven first on the painscan ledger.

---

## Background

Menhir today forks similar memories two ways: genuine **contradictions** go
through `resolve_conflict_group(replace)` (loser set `GONE`, content absorbed,
edges bridged — destructive, history flattened); everything else gets a
**correlation edge** and both nodes are kept and co-surface. There is no model
for an **update** — "same fact, newer value" (e.g. delegate port 8084 → 8090,
migration state V83 → V86). Updates are either misfiled as contradictions or
left as co-surfacing duplicates that crowd recall. This matters because the
default KNOWLEDGE recall preset weights recency at only β=0.1 (similarity-
dominated), so the newest copy does **not** reliably win — all versions surface
together (observed: three identical "Smokie33" nodes returned in one recall).

Separately, painscan's candidate harvest matches new signals to existing
clusters by **label-only** cosine ≥ 0.72 (`analyze.py:embed_map`), so two
clusters describing the same fact under different labels never merge (observed:
clusters 668 vs 811 both encoded the same VPS password). The friction-ledger
retro-merge (`scripts/consolidate_clusters.py`) is not run on the memory ledger.

---

## Goals

1. **Supersession lineage in menhir** — a `SUPERSEDED_BY` edge chaining
   evolving facts; recall returns chain heads only by default; superseded nodes
   are retained (non-destructive) and reversible (drop the edge to un-supersede).
2. **Redundancy dedup** — fold byte-identical / near-identical copies into one
   canonical node (distinct from supersession, which is for *changed* info).
3. **One shared classifier** — generalize the existing `confirm_contradiction`
   LLM call into CONTRADICTION / DUPLICATE / UPDATE / UNRELATED, scope- and
   subject-gated, conservative by default.
4. **Phase 0 on painscan first** — validate dedup + UPDATE classification on the
   candidate ledger (offline, reversible) before touching the live graph.

## Non-Goals

- Do not change the decay ladder (ACTIVE→COMPRESSED→GONE); supersession adds a
  parallel `superseded` flag, it does not reuse GONE.
- Do not auto-supersede or auto-dedup **user-flagged** nodes (same exemption the
  decay sweep honors).
- Do not modify existing tests to accommodate changes (stop and report instead).
- Do not raise the KNOWLEDGE recency weight as part of this work — head-filtering
  makes "newest wins" deterministic without a scoring change. (A β tweak is a
  separate, eval-gated experiment.)

---

## Implementation Plan

### Phase 0 — painscan ledger consolidation (validate the idea offline)

**Files touched:**
- `scripts/consolidate_memory_clusters.py` (new) — mirror of
  `consolidate_clusters.py` but pointed at the **memory** ledger
  (`load_memory_ledger`/`save_memory_ledger`), matching on **label + definition**
  embeddings (not label-only), union-find transitive merge, keep highest-evidence
  cluster canonical, dedup evidence by `session_id+note`, recompute strength,
  refresh memory trends. Backup-first, `--dry-run`, idempotent, `--threshold`.
- `painscan/dream.py` — call the consolidation pass once at the end of a dream
  cycle (after `merge_memory`), behind a config flag.

**Anchors:**
- Reuse `painscan.embed.embed` / `cosine` and the `_UF` union-find from
  `scripts/consolidate_clusters.py`.
- Embed the concatenation `label + "\n" + definition` per cluster; default
  threshold 0.82 (looser than the friction tool's 0.85 because memory facts
  carry identity in the note). Expose `--threshold`.
- Add a 4-way classification hook (CONTRADICTION / DUPLICATE / UPDATE / UNRELATED)
  using the existing LMStudio client; for Phase 0, DUPLICATE → merge clusters,
  UPDATE → keep both but tag the older cluster `superseded_by: <cid>` and the
  newer `supersedes: <cid>` in ledger JSON (the offline analogue of the edge),
  CONTRADICTION/UNRELATED → leave as-is. This exercises the exact classifier
  menhir will reuse, on reversible JSON.

**Validation:** run on the current `MEMORY-CANDIDATES-LEDGER.json` (clusters
668/811 should fold; an UPDATE pair like port/migration-state should chain).
Confirm idempotency (second run = 0 changes) and JSON validity.

### Phase 1 — menhir: the classifier + `SUPERSEDED_BY` edge

**Files touched:**
- `infrastructure/llm.py` — generalize `confirm_contradiction` into
  `classify_relation(a, b) -> {CONTRADICTION|DUPLICATE|UPDATE|UNRELATED}`.
  Gate inputs on same scope + overlapping subject/predicate; conservative
  default (UNRELATED on low confidence).
- `infrastructure/consolidation_queries.py` — add `set_superseded(old_uuid,
  new_uuid)`: create `(old)-[:SUPERSEDED_BY {created_at, similarity}]->(new)`,
  set `old.superseded = true`, `old.invalid_at = datetime()`. Re-point on
  insert: if `old` already had `SUPERSEDED_BY`, move the edge to the new head so
  recall is always one hop from current (denormalized `is_current` boolean).
- `infrastructure/correlation_queries.py` — route the classifier output:
  DUPLICATE → existing absorb/supersede (`replace`); UPDATE → `set_superseded`;
  non-contradiction-non-update → existing correlation edge (unchanged).

**Anchors:**
- New `superseded: bool` / `is_current: bool` node props, distinct from
  `freshness`. Superseded nodes are NOT GONE and must be excluded from the decay
  GONE sweep's deletion (they are history) but excluded from prominence/edge
  inflation.
- Tiebreak for dual-newest (branching): `valid_at` → `created_at` → `flagged`.

### Phase 2 — menhir: recall head-filter

**Files touched:**
- `domain/recall.py` / `infrastructure/cypher.py` — default presets add
  `WHERE coalesce(n.is_current, true) = true` (equivalently
  `NOT EXISTS { (n)-[:SUPERSEDED_BY]->() }`). Add an opt-in `include_superseded`
  / a `history` preset that walks the chain for audit.

**Anchors:**
- One predicate in the candidate-fetch query; verify it composes with existing
  scope/freshness filters and the CONFLICT preset.

---

## Definition of Done (paste into session prompt)

```
[ ] Phase 0: scripts/consolidate_memory_clusters.py folds clusters 668/811 in a
    dry-run, is idempotent on second run, and leaves the ledger valid JSON
[ ] Phase 0: classifier emits CONTRADICTION/DUPLICATE/UPDATE/UNRELATED and is
    conservative (UNRELATED on low confidence)
[ ] Phase 1: SUPERSEDED_BY edge created on UPDATE; old node superseded=true,
    invalid_at set, NOT GONE; flagged nodes never auto-superseded
[ ] Phase 1: re-point-on-insert keeps recall one hop from the current head
[ ] Phase 2: default recall returns only current heads; include_superseded walks
    the chain; CONFLICT preset still surfaces conflict members
[ ] No existing tests modified; new tests cover classify_relation + head-filter
[ ] CHANGELOG updated in the active repo for the session
```

---

## Commit Shape

```
# Phase 0 (cth.painscan session)
feat(painscan): consolidate near-duplicate memory clusters before emit
docs(painscan): changelog

# Phase 1+ (menhir session)
feat(menhir): classify_relation (contradiction/duplicate/update/unrelated)
feat(menhir): SUPERSEDED_BY lineage edge + supersede routing
feat(menhir): recall returns current heads; include_superseded for history
test(menhir): classifier + head-filter coverage
docs(menhir): changelog
```

---

## Risks / Deferred

- **Mis-supersession hides a valid fact.** Chaining "prod port" under "staging
  port" removes the still-valid node from default recall. Mitigations: scope/
  subject gating, conservative classifier, and reversibility (drop the edge).
  Wrong supersession must be cheaper to undo than redundancy is to tolerate.
- **Branching / dual-newest** needs the `valid_at→created_at→flagged` tiebreak;
  without it the chain diamonds and recall head is ambiguous.
- **Supersede ≠ dedup.** Identical copies (the Smokie33 case) want merge, not a
  chain; evolving facts want a chain. Keep both actions; share one classifier.
- **Temporal alignment.** Use Graphiti's `valid_at`/`invalid_at` rather than a
  parallel clock; set `invalid_at` on supersession.
- **Deferred:** KNOWLEDGE recency-weight (β) tuning and any supersession penalty
  in scoring — head-filtering should make this unnecessary; revisit only with an
  eval-set measurement.
- **Deferred:** auto-supersession in the live enrichment scheduler (vs. a manual/
  batch maintenance pass). Start with a batch pass; promote to inline only once
  the classifier's precision is trusted on real candidates.
