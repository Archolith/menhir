# Top-to-bottom review — through the governance lens (2026-07-04)

**Reframe:** menhir's goal is a **governance/memory system**, not a memory system with features.
Each remaining unreviewed section is assessed against five governance criteria:
**authority** (who may act) · **provenance** (where things came from) · **accountability**
(decisions reconstructible) · **policy** (rules explicit, enforced at choke points) ·
**reversibility** (wrong decisions undoable). Completes the coverage program begun 2026-07-03.

---

## §1 Production ↔ frontier diff — the deployment gap IS the governance gap

`diff -rq` of the two `src/menhir` trees: **production is a strict subset** — 0 production-only
files, 46 frontier-only files, 33 shared files diverging. The 46 frontier-only files are the
entire governance apparatus: perception gates, fold algebra, Views, oracles, combiner, **wardens**,
belief/temporal/scope producers, artifact tier, QuantState. Divergence in shared files is
concentrated in `recall_service.py` (764 changed lines — production predates source attribution,
fact edges, tracing, frontier portions entirely) and production's `scoring_service.py` has **no
source-aware floor and no RRF-scale awareness** (bare `MIN_SIMILARITY_THRESHOLD = 0.15`, zero
`CandidateSource` references). Lifecycle/correlation/enrichment diverge only trivially (the
destructive paths were near-identical — which is why the hotfixes applied cleanly to both).

**Governance verdict: the system holding the real memories runs none of the designed governance.**
Every safety mechanism this program reviewed is dormant (frontier, flags off) or absent
(production). The single most consequential strategic fact found by the whole program.

## §2 `core/backend_impl.py` + bootstrap + runtime — the wiring layer

Spot-reviewed (not exhaustively read): delegation facade + string-dispatch REST ops; flag plumbing
settings→`RetrievalTuningConfig`→recall verified end-to-end; `delete_namespace` double-guarded;
`recover_orphans` fired at every runtime init (the de-facto consolidation engine — tracker D3).
**Governance verdict:** wiring is where policy silently becomes behavior — the init-time
consolidation is an *implicit policy nobody decided* (no cadence, no owner, no record), exactly
the pattern a governance system forbids. Bulk read still outstanding.

## §3 `cli/hook.py` — the third door (read fully)

The Claude Code integration: UserPromptSubmit recall injection (frequency-gated), Stop-event save
*nudges* (the model chooses to write — good: admission stays deliberate), PostCompact forced
recall. Well-behaved install/uninstall with marker-based upsert.
**Governance verdicts:**
- **Authority: it bypasses auth entirely** — builds services straight from `.env`, no bearer, no
  tier. Menhir now has three doors: MCP (governed), REST (half-governed, Q4), hook (ungoverned).
  A governance system has one door with one policy.
- **Accountability: it fails silent by design** (blanket except → empty hook output). Recall could
  be dead for weeks with no signal — an availability failure no ledger records. Needs a
  health-breadcrumb (telemetry event on hook failure, surfaced by `check_health`).
- Provenance: injected context carries no source markers (ties to the bundle-honesty plan).

## §4 `domain/warden.py` + producers — the judiciary exists (read fully)

The strongest governance artifact in the codebase. Every `WardenVerdict` carries its warden's
name and reason (decisions with provenance); `WardenChain` composes most-restrictive-wins with
all contributing verdicts kept "for explainability"; every warden is **permissive on missing
signals** (absence is unknown, not non-self) — the read-side mirror of the write-side's "missing
signals never veto"; axes are disjoint by contract (scope / evidence-anchor / oracle-admission /
currentness / contradiction / exhaustion). The `EvidenceAnchorWarden` ("retrieval & LLM summaries
are attention, not truth") is a conservation-law enforcement in miniature.
**Governance verdict: the judge the codex forensic review said was missing is BUILT — but it sits
on the frontier fork behind default-off flags, and it governs only the read side** (what may be
asserted), not the write side (what may enter the record). Producers (`belief.py`, `temporal.py`,
`scope.py`, `self_reinforcement.py`) reviewed at consumer level only.

## §5 Episode repositories — the docket is sound (claim path read)

`claim_pending_episode`: conditional single-statement claim guarded on
`state IN [PENDING, FAILED]`, `retry_after <= now()`, attempt caps — atomic by construction, with
a diagnostic precheck. Matches the service-level maturity verdict from the ingest review.
**Governance verdict:** custody of in-flight work (leases, ownership fencing, heartbeats) is the
best-governed part of the system — ironically, the *queue* has better chain-of-custody than the
*memories*.

## §6 MCP tool surface — the governed door (tier map surveyed)

~40 tools; `required_tier` is real and granular: 13 declare `operator`, 14 declare `readonly`,
the remainder default to `agent`; enforced centrally in `contracts.py` with query-auth
allowlisting and an `add_memory` rate budget.
**Governance verdict:** the MCP surface is the model of what the other two doors should be. The
REST tier gap (Q4) and the hook's no-auth path (§3) are deviations from an existing good pattern,
not missing design.

## §7–8 Oracle periphery, explorer, structural, infra — light pass

Oracle-stack core already cleared 07-04 (combiner implements corroboration independence;
constants are declared placeholders). Periphery detail (executor/diversity/brief_builder/
view_entropy/windowed_*) deferred until default-on candidacy. Explorer: loopback UI with a
CANDIDATE-reject delete and no auth — acceptable only while loopback-bound (fold into auth
Phase 3's bind assertion). Structural subsystem + infra plumbing: conventional-review genre, no
governance surface beyond what stamping already covers.

---

## Synthesis — what "governance/memory system" requires, and what already exists

The 07-03 program established **memory correctness** layer by layer. Governance adds five
obligations across all layers. Inventory against them:

| obligation | exists | gap |
|---|---|---|
| **Admission** (what may enter the record, on what foundation) | perception's veto gate (aggregates only); stamping choke point; CANDIDATE tier | raw entity/edge writes are admitted on extraction alone — the codex review's foundation-typed admission (basis: STATEMENT/RECORD/DERIVED/OPINION + ADMITTED predicate) is the missing write-side constitution |
| **Assertion** (what may be claimed as current truth) | the warden judiciary — built, provenance-bearing, composable | dormant: frontier-only, flags off; production asserts ungoverned |
| **Authority** (who may act) | MCP tier enforcement; namespace guards; locked stamps | REST parity (Q4), hook door (§3), OAuth identity (`auth-oauth-mvp.md`) |
| **Accountability** (reconstructible decisions) | receipts culture: gate receipts, merged_from, revision sidecar, telemetry; abstention/identity/reachability receipts planned | no unified decision LEDGER — receipts are per-layer, sinks are shared/unattributed (Q2); "who refused/deleted/merged what, when, why" is not one query |
| **Reversibility** (wrong decisions undoable) | bridged deletes; revision archive; keep-both conflicts | unmerge trails (planned), raw-capture (planned), archive-reading rehydration (F3), no-delete-on-scalar (hotfixed, needs principled replacement) |

**The strategic recommendation:** write the fifth anchor doc — `memory-governance.md` — with
those five obligations as its spine, and promote the codex forensic-transfer review from
"interesting analysis" to the write-side half of the roadmap (its foundation gate + conservation
law are Admission; the wardens are Assertion; the OAuth plan is Authority; a unified decision
ledger is Accountability; the already-planned repair machinery is Reversibility). Almost every
pillar exists as code or plan; what does not exist is the document that makes them one system —
and a deployment path that puts any of it in front of the memories it is supposed to govern (§1).
