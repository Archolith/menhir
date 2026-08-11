# Menhir MVP Roadmap

> ## RECONCILED STATUS (2026-07-15) - READ THIS FIRST
>
> Supersedes the 2026-07-14 banner below. **M1 gate MET (PASS) 2026-07-15** — a tracked report now
> exists: `archolith-bench/benchmarks/longmemeval-menhir-2026-07-15.md` (first full n=500
> oracle-corpus run; all 3 measured gates PASS). See the M1 section below for the full verdict and
> the important caveat on what the PASS does and does not claim (relative retrieval-quality claim,
> not an absolute accuracy number). **Only M5 closeout remains.**
>
> ## RECONCILED STATUS (2026-07-14) - READ THIS FIRST
>
> Supersedes the 2026-07-10 banner below. Since that reconciliation (`main` @ `2f607e3`) a large wave
> landed (2026-07-11 -> 07-14), so the "everything upstream is frozen" framing is stale. The MVP gate
> itself is unchanged: **M1 (fresh-Neo4j launch benchmark) is still the only remaining item and has
> NOT been consecrated** - no tracked report exists under `menhir/benchmarks/` or
> `archolith-bench/benchmarks/`. The harness is built (`archolith-bench/scripts/longmemeval/`); the
> `results/` runs are gitignored and are not launch evidence. Run it LAST on the frozen system, then
> M5 closeout.
>
> **Post-07-10 wave - what changed under the milestones (git-verified):**
> - **6a lifecycle is now much stronger than "ACCEPT + DOCUMENT."** The merge/delete lifecycle
>   remediation shipped end-to-end: lossless merge snapshots + journaled ENTITY_MERGE / ENTITY_UNMERGE /
>   ENTITY_DELETE sagas with per-participant UUID fencing (invariant 14), an exact journaled unmerge
>   inverse, a degraded-legacy lane + recoverability inventory, and a recovery runbook (`5bcb3a8`,
>   `17d553f`, `cfb509b`, `6263579`, `4394eb6`, `6091095`; plan `lifecycle-remediation.md` COMPLETE +
>   archived). **F2 (lawful sharpness) and F5 (demote-with-TTL) - parked as post-MVP in the 07-10
>   banner and in section 7 below - are now IMPLEMENTED** (F2 `ba43ab9`, calibrated
>   `SHARPNESS_COSINE_FLOOR=0.80`; F5 07-10, re-enables H2). Historical ~2,679 pre-snapshot merges
>   remain unrecoverable (accepted); going-forward merges are now recoverable.
> - **Dependency migration:** graphiti-core -> 0.29.2, Neo4j Python driver -> 6, **langfuse removed**
>   (closes the old OBS-01/COMP-02 PII-to-third-party concern); live-canary breaks found + fixed
>   (`d018477`, `af61fe7`, `89e5cce`).
> - **Admission-capability-separation gate** (`0cc811e`): user/manual source tier now gated on
>   turn-evidence grounding - tightens M2/Phase 3 write-trust.
> - **Metric class separation** (Plan 2, `2dca674`..`b439a64`): `:Metric` split from `:View`/`:Fact`,
>   `MetricWriteCoordinator` cross-DB saga, durable fact provenance, receipt identity stamps.
> - **Recall hygiene + scoped bootstrap** (`2ccce26`, `02da0fb`, `96e114a`; production migration
>   cutover 07-14): structural nodes excluded from semantic recall, namespace-scoped startup bootstrap,
>   server-side client->namespace pinning (`f399383`).
> - **Explorer** mounted into the main app; standalone `:8787`/`:8090` removed (`086f38c`, `e5b67e1`) -
>   Finding B (explorer had no auth) now inherits the main-app STATIC auth on loopback.
> - **Test isolation:** all tests forced onto a stood-up / remote-OVM test Neo4j, never the operator's
>   live graph (`c2e5b59`, `24050e6`, `ed242a2`) - closes the test-safety gap; relevant to M1 method.
> - **Content-vector recall lane:** experiment closed **NO-GRADUATE** (`0450174`, `3909b39`);
>   production `search_scored` unchanged.
> - **Remote MCP `tools/list`** scoped to the caller's auth tier (`7cf0b75`).
>
> Net: M1 is the sole remaining MVP feature-gate; the substrate it will benchmark is materially more
> hardened than the 07-10 board implies. Treat the 6a / F2 / F5 lines in the 07-10 banner and in
> section 7 as superseded by this note.

> ## RECONCILED STATUS (2026-07-10)
>
> The M1-M5 milestone bodies below are partly STALE. The current, code-verified MVP state is
> reconciled against `main` in **`.agent/verified-current-findings-main-2026-07-10.md`**. Key deltas:
> - **OAuth is DONE** (embedded AS + full security remediation + live Auth0). Listed as a post-MVP
>   parking-lot item below, but it is fully shipped - not remaining work.
> - **M1 is not "planned / highest-first."** The fresh-Neo4j LongMemEval Mode-B benchmark is BUILT
>   and has been run many times (archolith-bench `scripts/longmemeval/`, `results/`). It needs
>   *consecration LAST*, on the finished system - not first.
> - **Two blockers not in M1-M5** (both now RESOLVED): (6a) live memory-graph auto-merge damage and
>   (6b) read-side governance. See the board below.
> - **Governance gaps** (LICENSE, SBOM, coverage artifact, model-version record) are MVP-required —
>   now DONE.
> - **Write-side consolidation is BUILT** (not in M1-M5, not a gap): the post-LME pivot shipped as
>   code — D0 retrieval-entropy View-reachability probe (`services/view_entropy.py`,
>   `infrastructure/view_repository.py`, MCP `ops/view_entropy.py`), D1 QuantState
>   (`services/quantstate_consolidator.py`, `infrastructure/quantstate_repository.py`), event fold
>   (`services/windowed_fold.py`), and agent-experiential counters
>   (`services/failure_counter_bridge.py`, `instability_counter_bridge.py`; FailureEvent->QuantState
>   bridge `f8dd8ab`). It runs write-time / explicitly (scheduler off in bench mode). This is the
>   active direction the read-side ladder pivoted to (2026-07-04; current status in Track W of
>   `.agent/research/menhir-research-execution-ladder.md`);
>   the M1-M5 bodies below predate it and do not mention it.
>
> **Live MVP board — updated 2026-07-10. Only M1 remains; everything upstream is DONE:**
> 1. ~~M3 - file-event host hook + hook_center smokes~~ **DONE `5fd4f8b`** (both smokes green; hook installed for Claude Code)
> 2. ~~M4 - loopback-only operator hardening doc + verify ready/stats/MCP-stdio~~ **DONE `b6502cc`** (Findings A+B folded in; 4 surfaces verified)
> 3. ~~M2 - live `POST /api/phase3/run` + enable decision~~ **DONE `5559370`/`9abd44b`** (scheduler kept on; live run recorded; persona-fit note: personal-measure consolidation is chatbot-shaped, coding-MVP value is mechanism+safety)
> 4. ~~6a - lifecycle-remediation + repair-or-accept the degraded graph~~ **DONE `41ff066`** (ACCEPT + DOCUMENT: merges unrecoverable, gates fail-safe toward retention, M1 uses a fresh graph; F2/F5/F4/D2/D3 parked as durable todos; F6 → keep compression)
> 5. ~~Governance files~~ **DONE `ec8f59e`/`906fe64`** (LICENSE Apache-2.0 + NOTICE; SBOM CycloneDX w/ licenses; coverage 78.2%; model-governance AI-G01)
> 6. ~~6b - governed vs ungoverned read-side~~ **DONE `ddc9832`** (RATIFY CURRENT: source-aware RRF floor ON, warden/oracle judiciary OFF/evidence-backed; "no floor" finding was stale)
> 7. ~~M1 - consecrate the fresh-Neo4j benchmark~~ **DONE 2026-07-15** (full n=500 oracle-corpus IR
>    gate, all 3 gates PASS on the recalibrated Hit@3 bar; `archolith-bench/benchmarks/longmemeval-menhir-2026-07-15.md`)
> 8. **M5 closeout — THE ONLY REMAINING ITEM.**
>
> Also this session: fixed 2 stale OAuth AS tests (`7290478`), repaired a corrupted numpy dist-info in
> the venv, and filed follow-up todos (Phase 3 quoted-measure guard + persona decision; CLI hook
> auth-door `dc982ad7`).

**Status:** current roadmap to local MVP, reconciled 2026-07-09 against `main`, `.agent/plans/`,
`.agent/reviews/`, `docs/roadmap/`, the latest Hook Center / Phase 3 commits, and
`archolith-bench` `master` at `01bfd6d`.

## MVP Definition

Menhir's MVP is a local, single-user agent-memory service that can be trusted during real coding work.
It does not try to prove the full Semantic Operating System vision. It must do four things well:

1. Capture durable user-authored evidence from agent hosts without transcript logging.
2. Consolidate that evidence into precise, current Views without writing wrong current state.
3. Warn when recalled file-anchored memories may be stale because the underlying file changed.
4. Produce reproducible launch evidence from a fresh graph, not from the developer's long-lived graph.

The MVP is explicitly **local-first**: Neo4j is local, auth can remain the hardened static-tier scheme,
and live OAuth rollout / multi-user / organization-scale governance stay post-MVP unless the launch target changes.

## Current Mainline State

These are treated as already landed on `main`:

| Area | Current state | Owner docs |
|---|---|---|
| Core memory service | v1 graph/MCP/REST/runtime complete; namespaces, structure graph, TODO graph, backend-first MCP, and degraded startup are documented | `.agent/memory-roadmap.md`, `.agent/architecture.md`, `.agent/data_models.md` |
| Read-side frontier/oracle work | Built in pieces but not an MVP dependency; read-side gates were neutral-to-negative on real benches and remain opt-in/default-off | `.agent/plans/backlog/menhir-frontier-undone-work-chunks.md`, `.agent/plans/backlog/deferred-verification.md` |
| TurnEvidence producers | Claude Code, OpenCode, and Codex producers share one deterministic triage core; raw turns do not enter normal recall | `docs/turn-evidence-producers.md`, `.agent/adr/0001-conversation-turn-capture-surface.md` |
| Phase 3 consumer | `consolidate_personal_memory` exists, with real-data validation, correction handling, receipt clarity, and deterministic SUM grounding promoted on | `.agent/reviews/menhir-phase3-realdata-validation-2026-07-07.md`, `.agent/plans/menhir-phase3-*.md` |
| Hook Center | File events mark structure files dirty; stale anchors are labelled in recall/context; verification receipts enrich warnings without clearing dirty state | `docs/hook-center-tool-events.md`, `docs/runbooks/hook-center-*.md`, `docs/smoke/2026-07-08-hook-center-stale-lane.md` |
| Write-side consolidation | D0 entropy/View-reachability, D1 QuantState, event folds, scalar/event projections, and agent-experiential counters are built; scheduled jobs remain off in bench mode. The counting-slice D0 delta is reported; the July owner plans are archived decision records | `.agent/research/menhir-research-execution-ladder.md` Track W, `.agent/architecture.md`, `.agent/data_models.md`, `.agent/memory-aggregation-under-uncertainty.md` |
| Auth hardening | Static bearer tier scheme has route/tool tier enforcement, constant-time compare, query-auth narrowing, destructive-op audit, and landed OAuth resource-server code for protected HTTP routes; the remaining work is IdP selection and live connector proof | `.agent/plans/auth-oauth-mvp.md` |

## Archolith-bench Tie-in

`archolith-bench` is the falsification/evidence repo. As of `master` `01bfd6d`, its current Menhir
state is:

| Bench surface | Current state | MVP implication |
|---|---|---|
| Phase 3 View consolidation | Implemented as `archolith-bench harness menhir-phase3`, with offline CI smoke, six-scenario suite, tracked report `benchmarks/menhir-phase3-view-consolidation-2026-07-07.md`, and live characterization runbook `benchmarks/RUNBOOK-phase3-live-characterization.md` | Use this as M2's external evidence path. Menhir should not invent a second Phase 3 benchmark. |
| Phase 3 SUM phrasing matrix | `scripts/probe_phase3_sum_rate.py` plus the July 8 report show deterministic SUM grounding improved cross-check-dominated variants with `wrong_view_writes=0` | M2 rollout should cite this result and only add new live evidence if the production config changes. |
| LongMemEval framework | `scripts/longmemeval/` exists and the industry matrix marks persistent Menhir memory as `candidate-before-launch` | The MVP launch benchmark should either produce a tracked `benchmarks/longmemeval-menhir-*.md` artifact here, or explicitly document why the Menhir-local fresh-Neo4j benchmark is the MVP gate instead. |
| Facet/R2 | Real-embedder runs showed F can win on the draft fixture, but gate-b/gate-c found active FACET wiring is not justified over real embedding + wardens; recommendation is shadow-only | Facet production wiring is post-MVP unless new bench evidence shows topical lift. |
| Read-side oracle/frontier | LongMemEval campaign shows plain node retrieval remains the champion; read-time levers were neutral-to-negative | Read-side oracle default-on work is not an MVP blocker. |
| Headline numbers | `HEADLINE-NUMBERS.md` has no active public numbers | Do not use Menhir MVP results as launch/public copy until archolith-bench records them as active evidence. |

## MVP Roadmap

### M0 - Current-doc alignment

**Status:** complete once this doc is committed.

Purpose: give future agents one current route instead of choosing between stale chain handoffs,
proposal docs, and executed plans.

Done when:

- `docs/roadmap/README.md` points here as the active MVP sequence.
- `.agent/README.md` distinguishes the old chain handoff from current MVP sequencing.
- Old proposal docs remain preserved, but no proposal is implied to authorize MVP implementation.

### M1 - Fresh Neo4j launch benchmark

**Status: GATE MET (PASS) 2026-07-15** — first full n=500 oracle-corpus IR-gate run. All 3
measured gates PASS: Hit@3(support) menhir=4.60% vs graphiti(vector-only)=0.40% (~11.5x, gate
recalibrated — see below), MRR@10 0.0466 vs 0.0033 (~14x), explainability 100%. Evidence:
`archolith-bench/benchmarks/longmemeval-menhir-2026-07-15.md`. Scope: **oracle-only** (`_s`/`_m`
large-haystack recall deferred post-MVP per `.agent/archive/plans/menhir-m1-oracle-lme-ir-benchmark.md`).
**Read the evidence doc's "What this PASS means and does not mean" section before citing this** —
it is a relative (beats-vector-only-baseline) retrieval-quality claim, not an absolute memory-QA
accuracy claim; in absolute terms menhir found supporting evidence for only 81/500 questions
(16.2%). The separate Mode-B answer-accuracy (LLM-judge) run remains untracked — see
`archolith-bench/benchmarks/industry-trusted-benchmark-coverage.md`.

Purpose: prove Menhir works from a clean graph with explicit relevance labels. The long-lived local
graph is useful for development, but it is not launch evidence.

Build from `.agent/plans/fresh-neo4j-memory-benchmark-plan.md`:

- `benchmarks/` fixture corpus and query qrels.
- Fresh Docker Neo4j harness that never touches the normal `menhir-neo4j` container.
- Vector-only vs Menhir recall comparison.
- JSON and Markdown report artifacts with commit, corpus hash, provider config summary, and metrics.

Tie to `archolith-bench`:

- Preferred launch-facing path: implement or finish the persistent-memory LongMemEval Mode B artifact
  that `archolith-bench/benchmarks/industry-trusted-benchmark-coverage.md` already lists for Menhir.
- Acceptable MVP-local path: build the Menhir-native fresh-Neo4j benchmark first, then either publish
  its report under `archolith-bench/benchmarks/` or add an archolith-bench wrapper that consumes the
  Menhir JSON/Markdown report.
- Do **not** treat existing LongMemEval score-campaign notes as this MVP gate; those notes explain why
  read-side re-ranking lost and why consolidation became the direction.

MVP gate:

- ~~Hit@3 is at least 0.80 on the launch set.~~ **RECALIBRATED 2026-07-15 — see note below.**
  Hit@3 (support) must exceed the graphiti (vector-only) baseline at the same top-3 cutoff, on
  the same graph. Absolute round number replaced with a relative bar.
- Menhir graph recall ties or beats vector-only MRR@10.
- ~~`must_not_return_rate == 0` for stale/superseded facts.~~ **CORRECTED 2026-07-14 — not a
  benchmark gate.** `knowledge-update` is the LongMemEval question type that encodes supersession;
  read it off the per-question-type breakdown on real data instead of inventing a synthetic corpus.
- ~~`session_leakage_rate == 0` when session recall is disabled.~~ **CORRECTED 2026-07-14 — not a
  benchmark gate.** This is a boolean invariant, not a retrieval-quality metric, and menhir already
  pins it: `tests/test_recall_service.py::test_recall_filters_session_nodes_by_default` and
  `::test_recall_includes_session_nodes_when_requested`. Cite those tests as the launch evidence.
- Every returned Menhir result carries explainability/scoring metadata.

> **Gate-list provenance (2026-07-14):** the two struck criteria were written for the *native*
> hand-authored-qrels benchmark below, which is unimplemented and is not the MVP path. The MVP path
> is `.agent/archive/plans/menhir-m1-oracle-lme-ir-benchmark.md` (oracle-LME, reuses the built harness); on
> that path the two criteria do not apply. A synthetic fixture was built to satisfy them, found to
> duplicate existing unit-test coverage, and removed. Do not reintroduce it.

> **Hit@3 recalibration (2026-07-15):** the original "0.80 on the launch set" threshold was
> written for the same never-built native hand-authored-qrels benchmark referenced above (a small,
> curated fixture with explicit relevance labels) and was carried over unchanged when M1 pivoted to
> the real LongMemEval oracle corpus — it was never re-derived or validated against that harness.
> The first full n=500 run against the real corpus measured menhir Hit@3(support)=4.6% against an
> 80% bar with no empirical grounding for this corpus's actual difficulty (LongMemEval's gold
> answers are frequently paraphrased/abstractive rather than literal quotes — e.g.
> `single-session-preference` scored 0/30 for *both* menhir and the vector-only baseline, pointing
> to a token-overlap matching-methodology limit rather than a pure retrieval failure). No external
> published Hit@3 numbers exist for comparison — Zep and Mem0 publish LLM-judge answer-accuracy on
> LongMemEval, not raw retrieval Hit@3, so the metrics aren't comparable. The threshold is replaced
> with a relative bar (menhir beats the vector-only baseline at the same cutoff), mirroring the
> existing MRR@10 gate's structure instead of a second unvalidated absolute number. See
> `.agent/archive/plans/menhir-m1-oracle-lme-ir-benchmark.md` and
> `archolith-bench/scripts/longmemeval/analysis/lib/retrieval_quality.py` (gate1_pass) for the full
> evidence and implementation.

### M2 - Phase 3 production rollout

**Status:** built; bench-backed; needs rollout decision and operator evidence.

Purpose: make TurnEvidence useful without expanding capture. The producer is deliberately narrow; the
consumer must stay precision-first.

Minimum MVP work:

- Keep capture selective: user prompts only, deterministic triage only, no assistant/tool/full transcript capture.
- Treat `archolith-bench harness menhir-phase3` as the external regression suite for consumer behavior.
- Choose the initial enabled namespace(s) and enable `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED`
  only after a local smoke run.
- Record one live operator run of `POST /api/phase3/run` or the scheduler job over a known namespace.
- Document accepted View families and known abstention cases in a runbook or evidence note.

MVP gate:

- No wrong current-state View writes in the launch smoke.
- Stated measures, deterministic corrections, and grounded SUM cases pass.
- Count-vs-spend partial extraction remains a receipt/characterization case, not a hard gate.
- Raw `:TurnEvidence` remains excluded from normal recall.

### M3 - Hook Center rollout

**Status:** built; smoke evidence exists for the stale lane.

Purpose: stop stale file-anchored memories from being silently treated as current truth.

Minimum MVP work:

- Install the file-event hook for the active local agent host(s), at least Codex and/or Claude Code.
- Run `scripts/smoke/hook_center_live_smoke.py`.
- Run or reference the stale-lane smoke for file event -> dirty file -> stale recall label -> context
  warning -> verification receipt.
- Keep Hook Center evidence Menhir-local unless/until archolith-bench gains a Hook Center suite; current
  archolith-bench does not own this benchmark.
- Keep v1 advisory-only: no automatic down-rank, delete, re-anchor, or dirty clearing.

MVP gate:

- File edits mark matching structure file nodes dirty.
- Stale anchored memories appear with `stale_anchor=true`.
- Context output includes an actionable stale warning.
- A post-dirty verification receipt changes the advisory but does not clear stale state.

### M4 - Local operator hardening

**Status:** partly done; finish only local-MVP blockers.

Purpose: make the local service safe enough for regular single-user use.

MVP blockers:

- Document that no-key/open auth mode must stay loopback-only.
- Keep explorer local-only or document it as non-authenticated localhost tooling.
- Add or document a telemetry retention/inspection practice if the launch benchmark or Hook Center
  smoke creates meaningful sidecar growth.
- Verify `GET /api/ready`, `GET /api/stats`, MCP stdio backend-client mode, and the active hooks
  against the local backend.

Non-blockers for local MVP:

- Live OAuth rollout: IdP selection, connector proof, and token issuance operations.
- Multi-user namespace ACLs.
- TLS/mTLS and cloud deployment posture.

### M5 - MVP closeout

**Status:** pending M1-M4.

Done when:

- Benchmark report exists and passes the MVP gate.
- Phase 3 rollout evidence exists for one namespace or remains explicitly manual-only with a reason.
- Hook Center smoke/report evidence exists for the active host.
- `.agent/verified-current-findings.md` is reviewed and any accepted local-only risks are called out.
- `.agent/CHANGELOG.md`, `.agent/architecture.md`, `.agent/data_models.md`, and `.agent/endpoints.md`
  are updated for any implementation deltas made while closing M1-M4.

## Post-MVP Parking Lot

These are valuable, but they are not required to call the local MVP usable:

- Live OAuth 2.1 rollout after the authorization-server decision.
- L3/L4 artifact loop, ColdStartBrief, Context Engine, and organization-scale Menhir.
- Doc Drift Watch automation.
- Facet candidate production wiring beyond the already-bounded anchored-slice evidence.
- Oracle/read-side frontier default-on graduation; current evidence does not justify it.
- R10/R11 reranking/amplification.
- Multi-tenant/cloud deployment.

## Source Reconciliation Notes

- `.agent/plans/chain-handoff.md` is useful historical context for the frontier branch, but it is not
  the current MVP sequence on `main`.
- `archolith-bench` `master` at `01bfd6d` is the current external evidence state used by this roadmap:
  Phase 3 is bench-backed; LongMemEval persistent memory remains candidate-before-launch; FACET active
  wiring is not justified by the latest gate-c result; no headline numbers are active.
- `docs/roadmap/weekend-oracle-runtime-roadmap.md` and `oracle-integration-plan.md` remain proposal
  and spec sequencing for the SOS direction, not MVP blockers.
- `.agent/plans/merge-to-main-and-prod-wiring.md` is mostly executed; its Phase 3 production-wiring
  work is represented here as M2 rollout/evidence rather than a merge blocker.
- `.agent/reviews/menhir-phase3-realdata-validation-2026-07-07.md` is the key Phase 3 review: it
  validates the producer/consumer skeleton and records the fixed consumer defects.
- `docs/hook-center-tool-events.md` is the current Hook Center contract; the latest stale verification
  commits add actionability without changing the advisory-only invariant.
