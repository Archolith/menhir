# Memory Review Program — living tracker

**The single pane of glass for the 2026-07-03/04 memory-under-uncertainty review program.**
Update statuses in place, date-stamp changes, keep one line per item — detail lives in the linked
plan/review, never here. Status tags: `[HOTFIXED]` `[DONE]` `[IN PROGRESS]` `[PLANNED]` `[QUEUED]`
`[VERIFY]` `[DECIDE]` `[MONITOR]`.

Anchor docs (the four layers): `memory-ingest-under-uncertainty.md` ·
`memory-aggregation-under-uncertainty.md` · `memory-retrieval-under-uncertainty.md` ·
`memory-lifecycle-under-uncertainty.md`. Evidence:
`reviews/menhir-lifecycle-scale-probe-2026-07-03.md` (+ correction & topology addenda).

---

## 1. Hotfixed — live in menhir `main`, tests green
<!-- NOTE 2026-07-10: menhir-frontier was folded into main and its dir/branch removed. References to
     "both checkouts" / "frontier parity" below are historical; there is now a single checkout. The
     surviving "frontier" mentions (`_apply_frontier`, "frontier flags/files") mean flag-gated code
     WITHIN main, not a separate repo. -->



| # | what | re-enable condition |
|---|---|---|
| H1 | `[SUPERSEDED 07-04]` auto-merge disarm replaced by **judge-gated merging** (vetoes + k=3 unanimous judge; no-judge = the old disarm behavior, fail-safe). Reviewed (two agent defects caught+fixed: qwen gate, adapter attr), ported to production, committed both repos. | — closed |
| H2 | `[RE-ENABLED 07-10 via F5, uncommitted]` demote-with-TTL replaces the disarm: unpromoted nodes get a 14-day grace TTL, deleted only if uncorroborated at expiry. Re-enable condition met (F1 protective default + F2 lawful sharpness + F5 recoverable middle rung). | — closed by F5 |
| H3 | `[HOTFIXED 07-03]` decay GONE disarmed (`LifecycleService.should_delete` → False; policy thresholds still tested at policy layer) | lawful sharpness + null-protective gates |

⚠ Processes started before 07-03 hold pre-hotfix code until restarted — see V1.

## 2. Quick fixes not yet done (each < 30 min)

- `[DONE 07-04]` **Q1** `ingest_service` NameError fixed in BOTH checkouts
  (`getattr(session, "project", "")`) — direct-path document linking can run for the first time.
  Regression test still owed by the path-unification plan (which deletes this path anyway).
- `[QUEUED]` **Q2** bench/test telemetry isolation — harness should set `MENHIR_MCP_TELEMETRY_DB`
  so prod/bench/test stop sharing one sink (attribution muddied all forensics this program).
- `[DONE 07-04]` **Q3** `/no_think` gated on the chat MODEL NAME (`_wants_no_think`) in both
  checkouts. (Agent's first cut gated on ProviderKind, which has no 'qwen' member — never fired;
  caught in review and fixed.)
- `[DONE 07-04]` **Q4** REST tier enforcement landed in BOTH checkouts (auth Phase 0 item 2):
  `_require_tier` guard mirrors `mcp/contracts.py` semantics (empty tier = auth disabled = skip);
  route guards — DELETEs → operator, memory/flag writes → agent; internal dispatch gets a TOTAL
  per-op tier map (`_OP_TIER_OPERATOR`/`_OP_TIER_AGENT`, explicit read-only remainder pinned by a
  naming-convention test). New suite `tests/test_api_tier_enforcement.py` (both repos, 61 green
  each, incl. two pre-existing stale route-test assertions repaired in each repo).
- `[DONE 07-04]` **Q5** constant-time token compares (`hmac.compare_digest`) in BOTH checkouts
  (auth Phase 0 item 1), pinned by test.
  Production picks all three up via serve-watch hot-reload; bench farm still needs V1 restart
  (deliberately deferred — the 1b A/B is running).
- `[APPLIED 07-05, uncommitted]` **Q6** shadow pipeline config aligned with the active arm.
  `auto_intent` was already fixed (both gate on `enable_intent_lens`); the remaining gap was
  `contradiction_interrupt` — `_apply_frontier` passed `tuning.enable_contradiction_interrupt`
  but `_run_assertion_shadow` omitted it (fell to the `AssertionPipeline` default `False`), so the
  shadow ran a different config than the arm it predicts whenever the flag was on. Shadow now
  passes all four gates (`auto_intent`/`contradiction_interrupt`/`belief_gate`/`evidence_anchor`)
  matching `_apply_frontier`. Behavior-neutral under the default (flag off); 16 shadow/intent/
  contradiction tests green. Frontier-only (production menhir has no shadow). Committed in `20be979`
  (folded into the WS3 retrieval commit, since it lives in `recall_service.py`).

## 3. Plans — index and status

**Perception (write side)**
- `[DONE 07-03]` `perception-dedup-signature-and-veto-receipts.md` — certain-only signature,
  same-day judge candidates, tri-state coref memo, `unresolved_coreference` veto, veto receipts.
- `[PLANNED]` `perception-law3-bias-coverage-and-crosscheck-independence.md` — next perception
  step; pre-registered correlated-error experiment decides verify-vs-crosscheck primacy.
- `[DESIGNED]` `perception-consolidation-prod-wiring.md` — gated on leaving benchmark mode; must
  absorb the law3 plan's Part-3 config amendments before build.

**Retrieval (read side)**
- `[IN PROGRESS]` `retrieval-scale-contract-and-gap-remediation.md` — 1a DONE 07-04 (scale pin +
  contract docs), **1b A/B RUNNING 07-04** (normalized-scale arm vs rrf baseline; pre-registered
  rule: flip default at ≥ parity, regression blocks, numbers recorded at the flag site either
  way). Run caveats: exclude/flag BM25-fallback-mode recalls (ceiling halves); if oracle-ranking
  arms are included, the SemanticOracle [0,1] clamp confounds them (normalized mode fixes a
  saturation artifact — a "gain" there may be the clamp, not the rescale); confirm the bench farm
  was restarted post-hotfix before trusting runs (pre-hotfix processes still auto-merge into the
  shared graph during replays). Parts 2–5 open.
- `[PLANNED]` `retrieval-reachability-receipts-and-bundle-honesty.md` — now-worthy slice;
  reachability receipts feed the A6 decision (D1).
- `[GATED]` `retrieval-recency-split-and-view-injection.md` — recency-basis A/B; A6 gated on
  receipts data AND on decision D1.

**Ingest**
- `[DONE 07-04]` `ingest-identity-merge-gating.md` — judge-gated merges (Part 2), co-mention +
  anchor-project vetoes (Part 1), identity receipts (Part 4), unmerge audit trail (Part 3),
  namespace-scoped search verification (Part 1), unnamed node correlation skip (Part 5). Unblocks H1
  re-enable. 43 new tests green; existing tests updated for new routing.
- `[PLANNED]` `ingest-substrate-durability-and-path-unification.md` — zero-extraction = success,
  raw-capture fallback node, direct path delegates to queue, per-job budget enforcement.

**Auth (MVP pillar) — `[DONE 2026-07-10]`**
- OAuth is fully shipped. The resource-server slice landed 2026-07-09 (JWT/JWKS validation,
  protected-resource metadata, preflight diagnostics, local smoke helper, OAuth-owned protected HTTP
  auth when enabled). The **embedded authorization server** was built and its security findings
  AS-001..AS-007 remediated (`032b46d`, `23ac40f`, `b91bcc1`, `0597792`), and **live SaaS-IdP
  verification against a real Auth0 tenant** landed (`4a16417`, `scripts/smoke/auth0_live_smoke.py`,
  4/4). The prior "remaining blockers" (AS choice, connector/token smoke) are resolved; the embedded
  AS is the AS choice and the Auth0 smoke is the live proof. Only genuinely-pending item: the
  interactive authorization-code + PKCE browser flow against a real IdP (Phase-0 decision-gated,
  post-MVP; RS token validation is proven). See `docs/security-posture.md` §4–§5, §11.

## 4. Lifecycle remediation — protective sharpness & archive-first rehydration

Plan: `.agent/plans/lifecycle-remediation.md`. Status: F1+F3 DONE; F6 DECIDED (07-10); F2/F5 PLANNED (post-MVP).

**MVP 6a resolution (2026-07-10, @ctharvey):** the roadmap's "6a graph auto-merge repair-or-accept"
is closed as **ACCEPT + DOCUMENT**. Rationale: (1) the ~2,679 merges are unrecoverable — no snapshot
exists, no unmerge is possible; (2) the disarmed gates (H2/H3) fail safe toward *retention*, which is
the correct direction at personal scale; (3) M1's launch benchmark runs on a **fresh** Neo4j, so the
degraded dev graph does not affect launch evidence — it only affects day-to-day dev recall. F2 (lawful
sharpness) and F5 (demote-with-TTL) — needed to *re-enable* H2/H3 correctly — are explicitly **post-MVP**;
until they land the gates stay disarmed (retention-safe). The degraded dev graph is an accepted,
documented local-only risk.

**Deferred remediations are tracked as durable memory todos (so they survive plan archival):**
F2 = `1a9eb1f2` | F5 = `0b86a37f` | F4 (now ungated) = `86e4b309` | D2/D3 decay-wiring = `e2716793`.
This plan (`lifecycle-remediation.md`) stays **active, not archived** while F2/F5 remain PLANNED. The
former HIGH damage-tracking todo `9fbe2519` was closed (superseded by the ACCEPT decision + these
discrete forward todos).

- `[DONE 07-04]` **F1** protective sharpness default — removed `coalesce(toFloat(n.sharpness), 0.0)` 
  from stamping queries (`episode_stamping.py:56,102`); `should_compress`/`should_delete` now treat 
  missing sharpness as protection, never eligibility (`memory_types.py`); `fetch_decay_candidates` 
  excludes NULLs on delete phase (`consolidation_queries.py`). All 89 tests green.
- `[IMPLEMENTED + CALIBRATED 07-10]` **F2** lawful sharpness — true cosine via
  `graphiti_client.count_similar_by_cosine` (`NodeSearchConfig.sim_min_score` floor), replacing the
  RRF rank-artifact count. Landed `ba43ab9` (P1/P1b/P2/P4; 168 tests green). **P3 calibrated 07-10**
  against live LME `default` (n=150): `SHARPNESS_COSINE_FLOOR = 0.80` (0.75 was a 63%-compress cliff);
  probe `scripts/probe/probe_sharpness_cosine_floor.py`, result `.agent/reviews/f2-cosine-floor-probe.md`.
  P5 gate re-enable delivered via F5 (H2 re-enabled 07-10). Spec:
  `plans/lifecycle-f2-lawful-sharpness-implementation.md`.
- `[DONE 07-04]` **F3** rehydrate-from-archive — added `get_original_content(node_uuid)` to 
  `telemetry_store` (retrieves earliest `memory_revisions.old_value` for field='content'); 
  `lifecycle_service.rehydrate_node` now prefers archived content before falling back to node 
  summary/content. Archive-first recovery closes the photocopy-loss loop. Tests green.
- `[READY 07-10]` **F4** one-off merge audit over top `merged_from` absorbers — **now UNGATED**: H1
  judge-gated merging has landed (see H1 row), so the prerequisite is met. Audit survivor receipts vs
  episode MENTIONS to estimate legit-dedup vs false-merge fraction. Hard constraint: nothing may clean
  `merged_from` receipts (only surviving evidence; no unmerge possible). **CLOSED as won't-do
  (@ctharvey 07-10):** measure-only, no fix; recurrence is prevented by the merge-eligibility
  guardrail (`bf7756c`, ~51% of historical damage) + H1 judge-gating. Todo `86e4b309` closed.
- `[DONE 07-10, `dd1a398`]` **F5** consolidation middle rung — demote-with-TTL replaces the
  H2 `else: pass`. Unpromoted SESSION nodes get a set-once 14-day `ttl_expires` (`DEMOTE_TTL_DAYS`);
  promotion clears it (rescue); expired nodes are deleted AFTER promotion (promotion wins) with a
  `record_lifecycle_action(trigger="demote_ttl_expiry")` audit before each DETACH DELETE. **P7**:
  daily `consolidate_lifecycle` job on the maintenance scheduler (consolidation was restart-only;
  resolves D3). **This re-enables H2** (F2+F5 both landed). H3 untouched. 171 tests green. No frontier
  port needed (single checkout). Spec: `plans/lifecycle-f5-demote-with-ttl-implementation.md`.
- `[DECIDED 07-10 → Option B]` **F6** compression severity review — the ≤200-char target plus F3.
  **Decision: keep compression active (Option B).** F3 (rehydrate-from-archive) has landed, so
  compress/rehydrate is lossless in practice — rehydration reads the pre-compression original from
  the revision sidecar before any summary merge. Archive is the recovery path; no compression pause
  needed. Decided by @ctharvey 2026-07-10.

## 5. Decisions needed

- `[DECIDE]` **D1** A6: aggregate-lens View *injection* (retrieval plan) vs FRE-1006 *substitution*
  (codex forensic review Part 6 — summary XOR sources, inspection right). Same problem, competing
  answers; resolve in one memo when reachability data exists. Also weigh the codex review's E1–E3
  falsification experiments alongside.
- `[RESOLVED 07-10 → WIRED]` **D2** decay wiring: `apply_decay` is now a daily `decay_lifecycle`
  maintenance-scheduler job (mirrors F5's `consolidate_lifecycle` P7; gated on `lifecycle_service`
  present + `lifecycle_decay_enabled`, default on, 86400s). The decay sweep (edge sync → lawful F2
  sharpness recompute → compress via `should_compress`, with F3 archive-first rehydrate) now runs.
  H3 (`should_delete`) stays `False`, so the decay sweep does NO deletion (compressed count only);
  GONE re-enable remains its own gated review. This activates F6's Option-B compression in practice.
- `[RESOLVED 07-10 via F5 P7]` **D3** consolidation cadence — `recover_orphans` is now a deliberate
  daily `consolidate_lifecycle` scheduler job (not just runtime init). Cadence chosen: 86400s.

## 6. Verify / monitor

- `[DONE 07-05]` **V1** stale bench farm stopped — 33 `menhir-frontier` serve processes on ports
  8107-8114 / 8119-8121 (~197 MB, the finished LME-analysis A/B leftovers holding pre-07-03 code)
  were killed via PowerShell; all 81xx listeners freed, 0 frontier serves remain. Production
  `menhir.cli serve` (port 8090) + the LME Neo4j (bolt 7689, 55.7k nodes) were left untouched;
  production serve-watch auto-respawned a fresh worker as expected. Inventory in the 2026-07-05
  process/DB audit.
- `[VERIFIED 07-04]` **V2** `DELETE /namespace` double-guarded: `backend_impl.delete_namespace:199-207`
  refuses empty/default names AND the default graphiti group; route maps ValueError → 400. Closed.
  (Residual: tier gap Q4 means any valid token reaches the guard.)
- `[VERIFY]` **V3** correlation candidate search is namespace-scoped (merge plan Part 1.3 —
  cross-namespace proposals would be a second live bug).
- `[MONITOR]` **V4** conflict queue growth post-H1 — the merge band now feeds it; hourly
  `confirm_conflicts` (limit 20) is the drain. Watch for backlog.
- `[CLOSED 07-10]` **V5** production↔frontier divergence — MOOT: menhir-frontier was folded into
  `main` and its dir/branch removed; there is a single `src/menhir` tree, so there is no divergence to
  measure and nothing to port (F2/F5 landed once, in main).

## 6b. Governance reframe (2026-07-04)

The goal is a **governance/memory system**. Top-to-bottom coverage review completed through that
lens: `reviews/menhir-governance-top-to-bottom-2026-07-04.md`. Headlines: production runs NONE of
the governance apparatus (all 46 frontier-only files); the warden judiciary is built,
provenance-bearing, and dormant; menhir has three doors (MCP governed / REST half / hook
ungoverned); the queue has better chain-of-custody than the memories.
- `[DONE 07-04]` **D4** governance frame adopted; fifth anchor doc written —
  `memory-governance.md` (five obligations as spine; conservation law adopted; codex foundation
  gate promoted to the write-side admission roadmap; per-layer obligations table §5).
- `[QUEUED]` **Q7** hook door: health breadcrumb on silent failure + fold under the one-door auth
  policy (`cli/hook.py` bypasses auth entirely today).
- `[DECIDE]` **D5** deployment path: which governance mechanisms get promoted from frontier flags
  into production, in what order — the diff shows the governed system and the deployed system are
  currently different systems.

## 6c. Frontier read-side ladder — bench verdict reconciliation (2026-07-04)

The read-side (retrieval/oracle) bench verdicts that motivate this program's write-side
(consolidation) direction. Detail in the linked benchmark-notes, not here.
- `[DONE 07-05]` **R1 hybrid** — gate recalibrated (exempt saturated metrics) + miner symbol/scope
  vehicle fixed, then LIVE re-run on the 23.8k-node prod clone (155 queries): **does NOT graduate,
  for real** (not the old artifact). E_hybrid_a0 symbol 0.700<0.710 + wrong_scope regressed; floor
  neutral-to-negative on the sole headroom family (paraphrase 0.517 vs 0.533). `hybrid_alpha` stays
  UNSET — R1's floor joins the oracle stack as a neutral-to-negative read-time lever.
  `archolith-bench/.agent/benchmark-notes/r1-dummy-gold-run.md`.
- `[DONE 07-04]` **Oracle stack (R6/R7)** — LOSES on LongMemEval: node-only 0.400 > sem+temporal
  0.367 > full stack 0.333; every read-time lever neutral-to-negative. Confirms the pivot — you
  cannot re-rank your way to information candidate generation never assembled; aggregation is a
  consolidation problem. `archolith-bench/.agent/benchmark-notes/lme-score-campaign.md`.
- `[DONE 07-04]` **archolith-bench CI** — first CI on the repo (py3.11/3.12, offline suite,
  siblings from public GitHub); top infra gap closed.
- `[PROMISING 07-05]` **R2 facet (Chunk F)** — real embedder swapped in (OpenAI
  text-embedding-3-small): F (facet + meet-point) GRADUATES gold+hybrid on the DRAFT fixture even
  vs the lifted baselines (wrong_scope 0.07 vs 0.38-0.40, <=0.05 recall loss) — the positive
  counterpoint to R1. Owed before wiring CandidateSource.FACET: ctharvey's hardened fixture + real
  derived structural facets (hybrid uses a gold stand-in). `archolith-bench .agent/benchmark-notes/facet-r2-real-embedder-run.md`.

## 7. Coverage ledger (what the program has actually read)

**Read:** perception / fold_algebra / event_fold · recall / scoring / context_builder /
retrieval_tuning / hybrid_retrieval / graphiti_client search surface · ingest_service /
enrichment_steps / episode_stamping · lifecycle_service / memory_types / llm.py prompts ·
scheduler roster + runtime-init topology · oracle stack core (assertion_pipeline, oracle_combiner,
retrieval_oracles) · api/auth.py + routes destructive surface + mcp/contracts tier enforcement ·
settings→tuning flag plumbing (wired end-to-end, verified) · targeted repo fragments.
**Unread:** `domain/warden.py` decision internals · oracle_executor / diversity / brief_builder /
view_entropy detail · bulk of `backend_impl.py` beyond spot-checks · the ~40 MCP tool files ·
explorer app · structural code-graph subsystem · production checkout beyond verified sites.
**Oracle-stack verdict (07-04):** flag-gated stack is well-built — combiner implements
corroboration independence (1/√n per family, per-family caps, contradiction as negative
log-evidence, missing→uncertainty); oracles stateless/pure; nothing persists a rankable
confidence (§7-clean). Constants are self-declared calibration placeholders — fine while gated,
must be measured before any default-on.
