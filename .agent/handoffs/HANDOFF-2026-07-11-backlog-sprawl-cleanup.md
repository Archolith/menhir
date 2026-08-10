# HANDOFF — menhir `.agent/plans/backlog/` sprawl cleanup

> **Progress note (2026-08-08, curator audit, session 2 — full triage complete).** All buckets
> now walked one file at a time with ctharvey (verify claim against code -> propose ->
> approve/deny -> act), per this handoff's own rule. Results:
>
> **Bucket A** — `graph-verifiers.md` reviewed: status note still accurate (recall integration
> genuinely not wired), kept ACTIVE, not archived. The other 6 were already archived (see the
> 2026-08-07 entry below).
>
> **Bucket B** — resolved 2026-08-07 (frame docs kept as design rationale).
>
> **Bucket C** — all 5 reviewed: `r1-hybrid-candidate-generation.md` and
> `perception-law3-bias-coverage-and-crosscheck-independence.md` still accurate, kept as-is.
> `retrieval-scale-contract-and-gap-remediation.md` archived (all 5 parts verified DONE; status
> line was stale). `perception-window-and-triangulation.md` archived (all levers A/B/C1/C2/C3/
> Law-3/A6 verified DONE; status line claimed Lever C was still PLANNED). `menhir-temporal-
> chronostratum-plan.md` confirmed accurate, kept as-is.
>
> **Bucket D** — all 6 reviewed, none archived (correctly still backlog), but 2 stale status
> lines fixed: `perception-consolidation-prod-wiring.md` (scheduler wiring is actually BUILT and
> registered, default-off flag — not "gated on leaving benchmark mode"); `r2-facet-candidate-
> generation.md` (noted a dormant `FacetCandidateSource` seam already exists in `src/menhir`, not
> purely bench-side as stated). `r2-facet-production-integration.md`, `retrieval-recency-split-
> and-view-injection.md`, `retrieval-reachability-receipts-and-bundle-honesty.md`,
> `fold-algebra.md` also reviewed — the last one had a stale "NOT started" header despite its own
> later section (and 5+ downstream commits) proving it shipped; fixed.
>
> **Bucket E** — all 3 reviewed: `deferred-verification.md` and `menhir-frontier-undone-work-
> chunks.md` mostly accurate; the latter had one stale claim (Chunk E's L4 v0 slice plan was
> called "not yet authored" — it's actually written, mostly built, and already archived at
> `.agent/archive/plans/l4-artifact-loop-v0.md`), fixed. `ingest-primitive-family.md` had one
> stale marker (InstabilityCounter marked "NEXT BUILD" — already shipped as
> `instability_counter_bridge.py`), fixed.
>
> **Bucket F** (11 items) — all resolved: 5 already archived by other sessions
> (`menhir-belief-gate-activation.md`, `menhir-belief-gate-git-staleness.md`,
> `menhir-loopback-multiclient-provenance.md`, `menhir-phase3-consumer-quality-pack-v1.md`,
> `menhir-phase3-cross-check-quality-pack-v1.md`, `menhir-temporal-ingest-backdating-plan.md`);
> 6 reviewed and confirmed accurate, kept as-is (`anecdotal-recall-oracle-ladder.md`,
> `menhir-hyperedge-ready-storage.md`, `menhir-memory-supersession-and-dedup-plan.md` — owner
> decision on SUPERSEDED_BY still genuinely undecided, kept ACTIVE — `menhir-rung1-temporal-
> intent-reconciliation.md`, `menhir-temporal-bulk-ingest.md`).
>
> **Open decision 1** (temporal-plan merge) — already resolved before this session:
> `menhir-structure-temporal-oracle-plan.md` was merged into `menhir-temporal-chronostratum-plan.md`
> Rung 5 via commit `f38e8cc`.
>
> **Open decision 2** (does `artifact_archive` scope menhir's own-repo backlog, or is `git mv`
> needed) — still genuinely open; this session used `git mv` throughout, which works, but the
> question of whether the Menhir artifact-MCP path also works was not tested.
>
> **This handoff can now be treated as substantively closed** — every bucket has had a real,
> code-verified triage pass. The only remaining loose end is open decision 2 above (a tooling
> question, not a backlog-content question).

**Created:** 2026-07-11 (Claude Code)
**Task:** de-sprawl the 37 backlog plans, one plan per session.
**Prereq context:** this session reconciled the MVP roadmap + `docs/research/` corpus against code
(14 commits `06bfdd3`..`0810dd3`). The backlog plans have the **same drift**: several read as forward
"build order" for code that already shipped. This handoff finishes that job for the plans folder.

---

## The rule (owner-set 2026-07-11 — do not violate)

> A plan/research doc may be **archived** only when its idea is **(a) fully implemented/shipped** or
> **(b) rebutted/superseded by evidence** — AND only with **ctharvey's explicit approval**.
> Stale / partial / in-progress / default-off-but-working = **NOT archivable**; it stays active with a
> dated status note. (Wrapups are archived freely; plans are not.)

Corollary: **never archive on your own judgment.** Triage → propose → get approval → archive.

## Per-plan process (one plan per session)

1. Read the plan's Status + Definition of Done.
2. **Verify the load-bearing claims against code** (`Grep`/`Read` in `src/menhir`; do NOT trust `Glob`
   under `projects/` — the umbrella repo gitignores nested contents, so Glob returns empty. Use
   `git ls-files` or `Grep` with an explicit path).
3. Classify: **DONE** (shipped+verified) / **SUPERSEDED** (rebutted or replaced) / **ACTIVE** (partial
   or planned).
4. If DONE or SUPERSEDED → propose archival to ctharvey; on approval:
   `artifact_archive(artifact_type="plans", filename="<name>.md", project="archolith")`
   (these live under `projects/archolith/.agent`? NO — they live in the menhir repo's own
   `.agent/plans/backlog/`; confirm the artifact MCP's menhir scoping, else `git mv` into
   `.agent/archive/plans/` with approval).
5. If ACTIVE → leave in place, add a **dated status note** reflecting real code state (same style as
   the research-doc sync this session).
6. Commit per plan: `docs(backlog): reconcile <plan> vs code (archive|status-note)`.

## Verified environment facts (save re-derivation)

- Read-side retrieval stack (oracle R4-R7, frontier levers) is **built but benched neutral-to-negative
  on LME and ships default-off** (`config/settings.py:232-244`, all `frontier_*=False`).
- Write-side consolidation is **built + active direction**: D0 `services/view_entropy.py`, D1
  `services/quantstate_consolidator.py`, fold `services/windowed_fold.py`, counters
  `services/failure_counter_bridge.py`/`instability_counter_bridge.py` (commit `f8dd8ab`).
- ANCHORED_TO bridge is live (8,857 edges); coverage 24.5% is the remaining lever (todo already closed).

---

## Triaged worklist (my best-effort read; VERIFY each before acting)

### A. Likely DONE — archive candidates (verify, then propose)
| Plan | Status line | Verify |
|---|---|---|
| `turn-capture-claude-hook.md` | DONE 2026-07-07 | hook + `:TurnEvidence` producer present |
| `ingest-substrate-durability-and-path-unification.md` | IMPLEMENTED + TESTED 2026-07-05 | parts 1-4 in code |
| `perception-dedup-signature-and-veto-receipts.md` | DONE 2026-07-03 | `GateDecision.veto` |
| `ingest-identity-merge-gating.md` | IMPLEMENTED 2026-07-04 (pending review) | confirm reviewed/landed |
| `menhir-intent-oracle-plan.md` | **top says "not started" but body says production integration DONE** (IntentOracle in `default_oracles()`) — stale top line; net DONE. Fix or archive. |
| `menhir-r8-control-rails-plan.md` | Guards 1/2/3/5/6 DONE; Guard 4 (`domain/diversity.py`) + Guard 7 (`ContradictionWarden`) exist — confirm both landed → likely DONE |
| `graph-verifiers.md` | prototype + scheduler wiring DONE 2026-07-06 `1ccc230`; has a "Next steps (not done)" tail — confirm whether tail matters |

### B. Shipped write-side FRAME docs — archive vs keep-as-rationale (ctharvey judgment)
These describe shipped work but double as design rationale; archiving loses the "why". Decide per-doc.
| Plan | Note |
|---|---|
| `aggregation-as-consolidation.md` | "ACTIVE DIRECTION" but D0/D1 shipped; it's the thesis doc |
| `quantstate-agent-counter.md` | "ACTIVE" but QuantState shipped |
| `event-fold-view-architecture.md` | "ARCHITECTURAL FRAME"; acknowledges "everything built this session" |
| `productionize-view-primitives.md` | "COMPLETE 2026-07-02" for build, but a 2nd block says primitives are **inert / not wired** (READY TO EXECUTE) — PARTIAL, keep |

### C. ACTIVE / partial — keep, add status note
| Plan | Status |
|---|---|
| `r1-hybrid-candidate-generation.md` | IN PROGRESS; increment landed, benched-negative, `hybrid_alpha` unset |
| `retrieval-scale-contract-and-gap-remediation.md` | IN PROGRESS; Parts 1a/1b/2 DONE |
| `perception-window-and-triangulation.md` | Levers A/B/Law-3/A6 DONE; Lever C PLANNED |
| `perception-law3-bias-coverage-and-crosscheck-independence.md` | PART 2 DONE; Parts 1/3 remain |
| `menhir-structure-temporal-oracle-plan.md` | planned; steps 1-3 BUILT, step 4 gated (= Chronostratum Rung 5) |
| `menhir-temporal-chronostratum-plan.md` | Rungs 1A-4.5 built; Rung 5 remaining (see row above — dedup these two) |

### D. PLANNED / not started — keep
`fold-algebra.md` (DESIGNED, not started) · `perception-consolidation-prod-wiring.md` (DESIGNED, gated
on leaving bench mode) · `r2-facet-candidate-generation.md` (PLANNED bench-first) ·
`r2-facet-production-integration.md` (status unread) · `retrieval-recency-split-and-view-injection.md`
(PLANNED, decision-gated) · `retrieval-reachability-receipts-and-bundle-honesty.md` (PLANNED).

### E. LIVING / reference — keep, do NOT archive
`deferred-verification.md` (verification tracker) · `ingest-primitive-family.md` (inventory) ·
`menhir-frontier-undone-work-chunks.md` (chunk tracker; some chunks DONE).

### F. Status UNREAD — read first, then classify
`anecdotal-recall-oracle-ladder.md` · `menhir-belief-gate-activation.md` (likely DONE — gate activated
2026-06-29) · `menhir-belief-gate-git-staleness.md` · `menhir-hyperedge-ready-storage.md` ·
`menhir-loopback-multiclient-provenance.md` · `menhir-memory-supersession-and-dedup-plan.md` ·
`menhir-phase3-consumer-quality-pack-v1.md` · `menhir-phase3-cross-check-quality-pack-v1.md` ·
`menhir-rung1-temporal-intent-reconciliation.md` · `menhir-temporal-bulk-ingest.md` ·
`menhir-temporal-ingest-backdating-plan.md`.

---

## Suggested session order
1. **Bucket A** (clearest wins — verify + archive with approval). Fix the `menhir-intent-oracle-plan.md`
   stale top-line first (it contradicts its own body).
2. **Bucket F** (read + classify; some are probably DONE and move to A).
3. **Bucket C** (add dated status notes).
4. **Bucket B** (your call on archive-vs-keep for the frame docs).
5. **Buckets D/E** stay as-is unless status changed.

## Open decisions for ctharvey
- Frame docs (Bucket B): archive as "shipped" or keep as design rationale? (They're the "why" behind
  the write-side arc.)
- Do the two temporal plans (`menhir-structure-temporal-oracle-plan` and the Chronostratum Rung 5)
  overlap enough to merge?
- Confirm whether `artifact_archive` scopes menhir's own-repo `.agent/plans/backlog/` correctly, or
  whether these need `git mv` into `.agent/archive/plans/`.
