# Verified Current Findings - reconciled against `main`

**Reconciled:** 2026-07-10 against `main` @ `2f607e3` (direct code read).
**Supersedes for MVP purposes:** `verified-current-findings.md`, which was reconciled against
`menhir-frontier` @ `e42e6053` on 2026-07-06.

## Provenance note (resolves the "wrong branch" worry)

`e42e6053` is an **ancestor of current `main`** (`git merge-base --is-ancestor e42e6053 HEAD` = true),
not a divergent frontier branch. So the prior findings doc was a *stale point-in-time snapshot* of
main's own history, not a different codeline. `main` has since advanced to `2f607e3`; this pass
re-verifies each open item against that HEAD and drops/downgrades what no longer holds.

All Phase 11 remediation commits are confirmed present on `main`:
`4130e95`, `1858e9c`, `7bceb9a` (Phase 11 fixes) and `032b46d`, `23ac40f`, `b91bcc1`, `0597792`
(OAuth AS remediation) are all ancestors of HEAD.

---

## 1. Confirmed still-open on `main`

| ID | Finding | Evidence (main@2f607e3) | MVP status |
|---|---|---|---|
| A | Neo4j transport unencrypted beyond localhost | `config/settings.py:127` `neo4j_uri="bolt://localhost:7687"`, `:130` `neo4j_password=""`; `infrastructure/neo4j.py:53` `GraphDatabase.driver(uri, auth=...)` with no `encrypted=`/`trust=` | **MVP-required if public**; local-only = fine. Fold into M4 (document loopback-only OR force TLS on non-loopback URI). |
| B | Explorer app has no auth | `explorer/app.py:457` `create_app` has no auth middleware; `:627` default host `127.0.0.1` | **MVP-required (doc)**; already an M4 line ("keep explorer local-only or document as non-authenticated localhost tooling"). |
| D | Context-window retry uses free-text substring matching | `infrastructure/episode_lifecycle.py:30-36` `str(error).lower()` + `any(marker in error_text ...)`, markers `:24-25` | Post-MVP maintainability. No demonstrated misclassification. |
| MA-01 | `RecallService.recall` is very large | `services/recall_service.py:815` `recall(` in a 1505-line file | Post-MVP maintainability. Non-blocker. |

## 2. Reconciled - changed since the frontier snapshot

| ID | Prior claim | Reconciled verdict on `main` |
|---|---|---|
| **MA-03** | "`services/perception.py` + fold modules are dark code, no production call path" | **DROP.** False on main. `services/scheduler_tasks.py:393,421` calls `perceive_and_fold` from `consolidate_personal_memory` (the M2 Phase 3 consumer), guards `enable_cross_check/coref/verify=True`. Perception is the **wired, default-off** Phase 3 path, not dead code. |
| **FC-R01** | "perception Law-3 can treat every post-anchor mention as additive; final verification default-off" | **DOWNGRADE.** The production consumer pins `enable_verify=True` (`scheduler_tasks.py`), so the rollout path verifies. Library default remains off, but that is not the M2 path. Remaining knob is `enable_sum_grounding` (default off) = an M2 rollout decision, not a defect. |
| **C** | "telemetry: append-only growth + caller read limits unbounded" | **PARTIAL / DOWNGRADE.** Read limits are now clamped on main (`telemetry/store.py:514` `min(limit,100)`, `:547` `min(...,20)`, `:622` `min(limit,200)`); revision pruning exists (`:740` `prune_old_revisions(retention_days=14)`). Only append-only growth of non-revision tables remains = low operational debt. |

## 3. Remediated - confirmed closed on `main` (stay closed)

- SEC-01 / EC-01 (High) tier-gated destructive REST + backend dispatch - `4130e95`
- AR-01 (High) scheduler starts on `enrichment_ready` - `1858e9c`
- AR-02 (High) lease heartbeat during long jobs - `1858e9c`
- FC-02 / AR-04 (Med) forced-takeover fencing - `1858e9c`
- SEC-02 (Med) agent-tier ingest path allowlisting - `7bceb9a`
- TQ-01 offline suite green (1924 passed / 31 skipped, 2026-07-06)
- **OAuth AS-001..AS-007** all remediated 2026-07-09 (`032b46d`, `23ac40f`, `b91bcc1`, `0597792`);
  AS security audit + resource-server audit closed. **OAuth is DONE** (owner confirmed 2026-07-10).

## 4. Governance / launch gaps - MVP-required (newly tracked)

Not present in any roadmap milestone; verified absent at repo root 2026-07-10. Now todos:

| Item | Todo uuid | Priority |
|---|---|---|
| LICENSE file | `9b6eb192` | HIGH (public gate) |
| SBOM | `0dd4866f` | HIGH (public gate) |
| Coverage artifact (TQ-03) | `9fb8f6b4` | NORMAL |
| Model/version governance record (AI-G01) | `9eebaaf0` | NORMAL |

---

## 5. Reconciled MVP-blocker board (OAuth excluded - done) — UPDATED 2026-07-10

**Feature closeout — ALL DONE:**
- M3 `[DONE 5fd4f8b]` - file-event host hook installed (Claude Code) + both hook_center smokes green.
- M4 `[DONE b6502cc]` - local operator hardening doc (Findings A+B folded in); `/api/ready`, `/api/stats`, MCP-stdio backend-client, active hook verified against a local backend.
- M2 `[DONE 5559370/9abd44b]` - production server refreshed onto current main; one live `POST /api/phase3/run` recorded; scheduler kept on. Persona-fit note: personal-measure consolidation is a chatbot feature; coding-MVP value is mechanism+safety, not yield.

**Governance (public MVP) `[DONE ec8f59e/906fe64]`:** LICENSE (Apache-2.0) + NOTICE, SBOM (CycloneDX 1.6 w/ licenses), coverage artifact (78.2%), model-governance record (AI-G01). (Section 4.)

**6a `[DONE 41ff066]`** - ACCEPT + DOCUMENT (see §6a/§7). **6b `[DONE ddc9832]`** - RATIFY CURRENT (see §6b/§7).

**ONLY REMAINING — M1:** consecrate the fresh-Neo4j LongMemEval Mode-B benchmark on the finished
system, then M5 closeout.

**Not MVP blockers (post-MVP):** Finding D (retry text matching), MA-01 (recall size),
append-only telemetry growth (Finding C residual), perception library default flags (FC-R01 residual).
Post-MVP lifecycle work parked as durable todos: F2 `1a9eb1f2`, F5 `0b86a37f`, F4 `86e4b309`,
D2/D3 `e2716793`. Other follow-up todos: Phase 3 quoted-measure guard `3572cda8` + persona decision
`ce1d28a8`; CLI hook auth-door `dc982ad7`.

---

## 6. Reviews-folder sweep (2026-07-10) - findings not in the Phase 11 ledger

The prior ledger (`verified-current-findings.md`) only carried the Phase 11 audit. Sweeping
`menhir/.agent/reviews/`, root `.agent/reviews/`, and the archolith umbrella surfaced more:

### 6a. LIVE data-quality defect - highest-stakes item found (candidate MVP blocker)
`menhir-lifecycle-scale-probe-2026-07-03.md`: **CONFIRMED, "worse than predicted."** Production
auto-merge executed **~2,679 absorptions** on the live graph and the sharpness/uniqueness signal
**collapsed for ~59% of persistent nodes**, because `lifecycle_service.py:335` applies a
cosine-calibrated `CORRELATION_MERGE_THRESHOLD` to graphiti RRF rank scores (two different scales).
- **Acute bleed stopped:** hotfix disarmed both consumer gates (auto-merge + sharpness deletion).
- **Still open:** principled replacement queued in `.agent/plans/lifecycle-remediation.md`; and the
  **existing graph is already degraded** (2,679 bad merges / 59% sharpness collapse) - MVP needs a
  repair-or-accept decision on that damage before the fresh-Neo4j benchmark can represent reality.

### 6b. Governance deployment gap (partially stale) — RESOLVED 2026-07-10
`menhir-governance-top-to-bottom-2026-07-04.md` §1: production was a strict subset of frontier -
wardens/oracles/perception were frontier-only + flags-off ("the system holding the real memories
runs none of the designed governance"). Perception has since merged to main (07-09), so §1 is
partly stale, but the review claimed the **read-side warden judiciary is still frontier-only** and
production `scoring_service.py` has **no source-aware floor / no RRF-scale awareness** (bare
`MIN_SIMILARITY_THRESHOLD=0.15`). Also flagged: the CLI **hook is a third, ungoverned auth door**
(builds services straight from `.env`, no bearer/tier) and **fails silent** (blanket except).

**RESOLUTION 2026-07-10 (@ctharvey) — RATIFY CURRENT read-side posture.** Verified against `main`:
- **The "no source-aware floor" claim is STALE — already remediated.** `scoring_service.py`
  `_passes_floor` enforces a **source-aware, RRF-scale-aware** floor: vector-similarity candidates
  below `MIN_SIMILARITY_THRESHOLD=0.15` (a rank-cut on graphiti's RRF scale, ceiling
  `GRAPHITI_RRF_DUAL_METHOD_MAX=2.0`) are dropped, but `FLOOR_EXEMPT_SOURCES` (BM25 / pending /
  file-linked — relevance is provenance, not cosine) survive. Pinned by `tests/test_scoring_service.py`.
- **The warden/oracle judiciary is default-OFF by design and evidence-backed.** All `frontier_*`
  read-side levers (`_warden_gate`, `_belief_gate`, `_oracle_ranking`, `_intent_lens`, `_bm25`, …)
  default `False` (`config/settings.py:232-240`), opt-in via `MENHIR_FRONTIER_*`. The LongMemEval
  campaign found plain node retrieval is the champion and read-time levers were neutral-to-negative,
  so default-off is the correct posture, not a governance gap.
- **MVP posture (deliberate):** ship with the **source-aware floor ON** (baseline governance) and the
  **aggressive warden/oracle levers OFF** (no bench evidence justifies them). The read-side is
  governed at the floor; the heavy judiciary stays opt-in.
- **CLI hook auth door:** tracked separately as todo `dc982ad7` (likely acceptable local-trust path
  analogous to the stdio operator-trust — `security-posture.md` §6; fail-silent is the intentional
  fail-open hook contract; to be verified + documented, not an MVP blocker).

### 6c. Dark-code health audit (2026-07-09) - corrects MA-03
Root `menhir-dark-code-health-audit-results.md` (OpenCode/glm-5.2): **11 findings, 5 High.**
- Confirms MA-03 is a *reframe not a drop*: perception.py is not dead code but **High-severity
  bloat** (1,429 lines); adds `backend_impl.py` (1,618-line god-object, ~600 lines boilerplate),
  `structure_queries.py` (1,430), `telemetry/store.py` (1,212), `enrichment_steps.py`,
  `graphiti_client.py`, and `routes.py` register-by-convention drift.
- **Test-suite masking** (bears on the TQ-03 coverage todo): `conftest.py` `StubMemoryGraphAdapter`
  (1,075 lines) reimplements production contracts (conflict validation, episode state machine), and
  `test_perception.py` / `test_recall_service.py` are happy-path only - so the green suite
  (1924 passed) overstates real coverage. Strengthens the case for the coverage-artifact todo.

### 6d. Older cth_mcp_memory-era audits (2026-06, pre-rename) - low priority
`INFRA-AUDIT`, `CROSS-CUTTING`, `MCP-TOOLS`, `API-EXPLORER` (all vs `src/cth_mcp_memory/`, before
the menhir rename): naming residue (`yawn_memory`/`yawn-memory`), thread-safety (I-01), unbounded
in-memory rate-limit dict, silent exception swallowing. Likely partially remediated; re-verify only
if doing a maintainability pass.

### Reviews confirmed CLOSED (no action)
OAuth: `menhir-oauth-security-open-items-plan.md` = COMPLETE (main @ `6207fa3`, 2685 passed);
`menhir-oauth-security-consolidated.md` = all findings closed. Frontier audits
(`menhir-frontier-*`) govern the frontier fork = post-MVP.

### Baseline integrity note
Local menhir HEAD `2f607e3` == `origin/main`, and descends from the OAuth-complete commits
`6207fa3`/`6bb7966`. This ledger's "against main" claim is verified current, not stale.

---

## 7. Revised MVP-blocker verdict (post-reviews-sweep)

The maintainability/dark-code items (6c, 6d, MA-01, Findings C/D) are **not** hard MVP blockers -
they are launch-confidence and post-MVP hygiene. The two items that genuinely affect whether the
MVP's core promise ("trustworthy memory") holds are:
1. **6a - live graph auto-merge damage** (hotfixed but graph already degraded; repair-or-accept
   decision needed before M1 benchmark is meaningful). ~~**Elevate to MVP-required.**~~
   **RESOLVED 2026-07-10 → ACCEPT + DOCUMENT.** The acute bleed is stopped (H1 judge-gated merging;
   H2/H3 disarmed) and protective fixes F1+F3 landed. The ~2,679 merges are unrecoverable (no
   snapshot); the disarmed gates fail safe toward retention (correct at personal scale); and M1's
   benchmark runs on a **fresh** Neo4j, so launch evidence is independent of the degraded dev graph -
   the "meaningful before M1" worry is moot. F2 (lawful sharpness) + F5 (demote-with-TTL), needed to
   re-enable H2/H3, are explicitly post-MVP. F6 compression decided → keep active (Option B; F3 makes
   rehydrate lossless). Degraded dev graph = accepted, documented local-only risk. See
   `.agent/memory-review-tracker.md` §4 + `.agent/plans/lifecycle-remediation.md`.
2. **6b - read-side governance still frontier-only** (production scoring has no source-aware floor).
   Decide explicitly whether MVP ships with governed or ungoverned read-side.
   **RESOLVED 2026-07-10 → RATIFY CURRENT.** The "no source-aware floor" claim was stale — the
   source-aware RRF-scale floor is live and test-pinned. MVP ships with the floor ON (baseline
   governance) and the frontier warden/oracle judiciary OFF (evidence-backed: plain retrieval is the
   LongMemEval champion; read-side levers were neutral-to-negative). CLI hook auth door tracked
   separately (todo `dc982ad7`). See §6b for the full resolution.

Neither was visible in the roadmap's M1-M5 or the first ledger pass.
