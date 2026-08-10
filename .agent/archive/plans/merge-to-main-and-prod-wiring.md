# Plan: merge the frontier chain to main + wire it for production

**Status: EXECUTED 2026-07-06 — merged via PR #17, merge commit `bd22abd`. Recall-neutral (frontier
defaults off, `e5b22e8`); ingest (`bd81718`) + build_context (`20be979`) accepted as improvements
(§7). Post-merge: `menhir` clone reset to origin/main; 3 merged remote branches deleted. Still owed:
capstone re-run (Phase 0.2) + Phase 3 prod wiring.**
Scope: everything committed on `claude/menhir-chain-handoff-doc-7iuat2` — the Event→Fold→View +
perception arc AND the review-program workstreams (identity, auth, retrieval contract). The
ingest-substrate work that was OUT OF SCOPE on 2026-07-04 (uncommitted, breaking 11 tests) has since
**landed committed and green** as `bd81718` and is now IN the branch's linear history — so a straight
branch merge carries it. It is accepted into this merge (see §7).

## 7. Default-behavior neutrality gate (added 2026-07-06)

Requirement (ctharvey): the merge must not change default production behavior — or the specific
changes must be explicitly accepted. A pre-merge audit found:

- **Frontier recall defaults were ON** (`frontier_oracle_ranking/_intent_lens/_evidence_anchor/
  _shadow=True` in `MemorySettings`), and `backend_impl.recall` feeds `settings.retrieval_tuning()`
  into every recall, so `_apply_frontier` would re-sort survivors by the oracle combiner on `main` —
  a ranking change the 2026-07-04 read-side bench verdict found neutral-to-negative (audit DOC-05).
  **RESOLVED**: all frontier portions defaulted OFF in `e5b22e8` → recall is byte-for-byte
  ScoringService with no `MENHIR_FRONTIER_*` set. The stack stays opt-in per deployment.
- **`2d65bb0`** (lifecycle sharpness gates): **neutral** — the gated compress/delete/rehydrate path
  is not in the default scheduler loop and is disarmed by design.
- **`20be979`** (counter-View dedup): dedup change is **neutral** for regular memories
  (`view_kind=None`), fact_edge_mode flip is **inert** (fact_edges off); BUT it makes two
  **unconditional** `build_context` output changes — an abstention message on empty recall and an
  "(unresolved conflict)" name marker when `conflict_bonus==1.0` (reachable in the default path).
  **ACCEPTED** as an honesty improvement.
- **`bd81718`** (ingest substrate): **unconditional** redesign — `ingest_episode` unified onto the
  queue (timeout→QUEUED, a new synchronous-caller outcome), zero-extraction→success (was terminal
  FAILED), failed episodes leave a recallable raw-capture entity, per-job LLM budget backpressure.
  **ACCEPTED** as an intended robustness improvement.

Net: the merge is behavior-neutral on the **recall/ranking** path (gate satisfied) and carries two
explicitly-accepted behavior improvements on the **ingest** and **build_context** paths. Any PR body
must list these two accepted deltas so downstream operators know ingest outcomes and context strings
changed.

## 1. Verified topology (2026-07-04)

One repo (`github.com/Archolith/menhir`), two local clones:

| clone | branch | state |
|---|---|---|
| `menhir-frontier` | `claude/menhir-chain-handoff-doc-7iuat2` | **235 ahead / 1 behind** `origin/main` (behind = `e8201da`, a docs note). **31 commits unpushed.** Dirty tree = in-flight agent work (excluded). |
| `menhir` | `main` | **3 unpushed local commits**: `4be56f1` (auth port), `9d6a8ed` (identity port), `b49f3c0` (unflag_memory). The ports duplicate branch content. |

**Merge dry-runs: 0 conflicts** — both vs `origin/main` and vs the port-carrying local `main`
(`git merge-tree` on merge-base; the ports applied identical content, so git resolves them cleanly).
Prefer **merge (PR), not rebase**: the branch is pushed/shared and 235 commits of forensic history
(every finding references its commit) should not be rewritten.

## 2. What is on the branch (inventory → merge classification)

| workstream | key commits | classification |
|---|---|---|
| Perception boundary + gate (steps 1–5, Lever B, stated-floor, audit receipt) | `ac10764 259f7df 8238e01 e9ce8fd` | **prod code, merges as-is** (pure services; nothing auto-invokes it) |
| Lever A σ WINDOW + A6 windowed recall | `ecc5052 52e5ab4` | prod code, merges as-is; A6 is a callable resolver (no ranked-injection — deferred product decision) |
| Law-3 RESET + bias coverage | `115e634 48cbe80` | prod code, merges as-is |
| Lever C1–C4 (category grouping, cross-episode dedup, coreference, verify gate) + prompt de-overfit + veto telemetry + NL counter surface | `2bad574 8353df9 f933115 889a1ef 18092fc 9c6d2b0 298116e` | prod code, merges as-is; **guards default OFF** (see §4.2) |
| Identity judge-gated merges + destruction disarms | `6ff3649` | **already ported to main** (`9d6a8ed`) — merge dedupes cleanly (verified) |
| Auth Phase 0 | `4130e95` | **already ported** (`4be56f1`) — dedupes cleanly |
| Retrieval scale contract (1a pin, 1b staged behind env, A/B = parity, default held) | `f7f4128 91843e5 da4b5a1 c9dd155` | prod code; behavior-neutral by default (flag off) |
| View repository (ViewKind SSOT, LWW guard, audit props) | in perception commits + `9ee3443` lineage | prod code; **additive schema** — new `:Entity` props only, no migration needed; `qs_*` back-compat mirror keeps pre-View readers working |
| Docs: plans, governance, tracker, handoffs, reviews | `fcd2bef 5529d98` + throughout | merges as-is (docs-only, verified) |
| Bench harnesses (capstone, tuning, acquisition window, answer A/B) | archolith-bench repo | separate repo — no action in this merge |

**Independent review already done (2026-07-04):** high-risk commits (auth, identity/destruction)
verified sound; 10 modified test files all principled hotfix-pins; unit suite 1022 passed with all
failures attributable to the excluded in-flight tree. See session review + memory records.

## 3. Preconditions (hard gates — do NOT merge before these)

- **P1 — the in-flight tree lands or is stashed by its owner.** The active agent's uncommitted work
  breaks 11 unit tests (`test_services_pipeline.py`, `adapter._correlation` contract change). The
  merge ships only committed history, but the freeze/verify steps need a green `-m unit` on the tip
  being merged. Coordinate; do not stash another session's tree.
- **P2 — attribute the 7 non-unit failures** seen in the aborted mixed run (likely env-dependent
  tests needing live services; the full suite is NOT casually runnable — verify with owners or run
  the specific files with required services up). They must be understood, not necessarily green.
- **P3 — reconcile push state.** Push the branch (now 6 unpushed: the 5 audit-remediation/settings
  commits + `a772c7f`). **RECONCILED 2026-07-06** — the `menhir` main clone's entire dirty+diverged
  state was audited and is fully redundant relative to the branch:
  - 3 unpushed port commits (`4be56f1`/`9d6a8ed`/`b49f3c0`) duplicate branch content → dedupe on merge.
  - 5 uncommitted `.agent`/config doc edits are a `cth.mcp.memory`→`menhir` naming sweep the branch
    already did (branch has 0 old-name occurrences + further evolution) → superseded on merge.
  - 2 untracked files were the ONLY unique content and were **rescued onto the branch** (`a772c7f`):
    `.agent/plans/fresh-neo4j-memory-benchmark-plan.md` + `.agent/reviews/Research Note- Evidence
    Admission for Agent Memory.pdf`.
  → After the merge lands, reset the clone with `git fetch && git reset --hard origin/main` (drops
  the redundant ports + doc edits, brings it current). Nothing unique is lost.
- **P4 (post-merge hygiene, not a gate)** — sweep the 11 stale bench servers on ports 8107–8121
  (tracker item V1) once the active agent is stopped; they hold pre-07-03 code.

## 4. Phased execution

### Phase 0 — freeze + record (½ session)
1. ~~Tag the tip~~ **DONE 2026-07-04** — three annotated tags pushed to origin:
   - `original-pre-frontier` (`093a44a`) — the last common state before ANY of the chain; the
     restore point / diff base ("what was it before all this": `git diff original-pre-frontier..main`).
   - `main-pre-merge-2026-07` (`e8201da`) — main immediately before the merge.
   - `frontier-chain-reviewed-2026-07-04` (`56b7e61`) — the reviewed chain tip the verdict covers.
   Re-tag the FINAL tip at merge time if the branch moves past `56b7e61` (in-flight work landing).
2. Re-run the **Arm C capstone** (`archolith-bench/scripts/longmemeval/analysis/capstone.sh run`)
   against the frozen tip — banks the with-everything retrieval numbers vs the 07-03 baseline
   (12/14 reached, rank 2, 133 tok). This is the merge's headline evidence and the post-merge
   regression reference.
3. `-m unit` green on the tip (expects ~1033 passing once P1 clears).

### Phase 1 — merge to main (½ session)
1. Push branch; open PR `claude/menhir-chain-handoff-doc-7iuat2 → main` via `gh`.
2. PR body: the workstream inventory (§2) + the review verdict + capstone numbers. This plan is the
   review artifact; deep-dive review is already done.
3. Merge (no rebase). Post-merge on `main`: `-m unit` full pass in the `menhir` clone; confirm the
   port-duplicated files are content-identical (`git diff main~1..main -- src/menhir/api/auth.py` etc.
   should show only branch-side changes).
4. Update `menhir/.agent/` README/CHANGELOG: one entry for the merge (conventional `docs:` commit).

### Phase 2 — post-merge verification on main (½ session, benchmark graph)
1. `buildout_ab.sh` main-vs-frontier should now be a **parity check** (same code) — run a small-N
   sanity A/B; divergence means the merge missed something.
2. Re-run `capstone.sh delta` against main-served recall — must reproduce Phase 0 numbers.
3. Answer A/B (bench `8903e7c` harness) spot-check: committed Views still improve end-to-end answers.

### Phase 3 — production wiring (1 session; the real "wire it" work)
The design is already locked in `perception-consolidation-prod-wiring.md` (six decisions, updated by
`48cbe80` which closed the Law-3 blind spot #6). What remains is the build:
1. **Scheduler task** `sync_personal_memory_views` in the maintenance loop (mirrors
   `sync_experience_counters`): nightly; **dirty namespaces only** (Episodic newer than the
   namespace's Views' `created_at`); **batch re-fold** (Law-3 requires it); budget cap + 429
   hard-stop; disabled in benchmark mode with the rest of the scheduler.
2. **Pin ALL bias guards in the prod wrapper**: `enable_cross_check=enable_coref=enable_verify=True`
   (they default False in the library; under raw defaults the motivating failure — unanimous-but-wrong
   SUM — commits). k=5 initially; k is the cost dial once thresholds are trusted.
3. **Settings surface**: `MENHIR_PERCEPTION_*` env for k / threshold / budget / guard pins, wired
   through `MemorySettings` like `MENHIR_FRONTIER_SIMILARITY_SCALE` was (`91843e5` is the pattern).
4. **Embedder prerequisite**: View surfaces need `graphiti_embed_provider=openai` (+ model) or
   counters degrade to BM25-only (known R5 finding). Document in prod config; the scheduler task
   should log loudly when embedder resolves to None.
5. **MCP registry**: only if a new tool/entry point is exposed — then edit
   `ctharvey.workspace/mcp-registry.json` + `sync.py generate` (never hand-edit generated configs).
6. Unit tests for the task (fake store/adapter, mirroring `test_experience_counters_task.py`).

### Phase 4 — rollout + rollback story
- **Rollout order**: merge (Phase 1) is behavior-neutral — nothing invokes perception automatically,
  retrieval flag is off, guards land dormant. Risk concentrates in Phase 3's scheduler task; ship it
  dark (env-gated `MENHIR_PERCEPTION_CONSOLIDATION=off` default), enable on one namespace, watch the
  abstain-rate + `view_audit_*` receipts + `perception_abstained` counter for a week, then widen.
- **Rollback**: (a) scheduler task off = env flip; (b) written Views are supersedable, namespaced,
  source-tagged → surgical cleanup by `source`; (c) destruction stays DISARMED (do not re-arm
  sharpness deletion as part of this merge — it re-arms only with a lawful sharpness signal, its own
  plan); (d) the merge commit itself reverts cleanly if catastrophic (additive schema, no migration).

## 5. Acceptance criteria (the merge is DONE when)
1. `main` contains the chain; `-m unit` green on both clones; port-dedup verified.
2. Capstone numbers on main == Phase 0 frozen numbers (no retrieval regression).
3. Buildout A/B main-vs-frontier shows parity.
4. Prod scheduler task exists, dark-shipped, unit-tested, with all guards pinned and budget caps.
5. CHANGELOG + `.agent` docs updated; this plan marked EXECUTED with the PR number.

## 6. Explicitly out of scope
- The in-flight ingest-substrate work (its own PR after it lands green).
- Re-arming sharpness deletion (needs lawful sharpness — separate plan).
- A6 ranked-injection (product decision: should a computed count out-rank retrieved nodes?).
- Lever C recall re-pricing beyond the capstone (nice-to-have, not gating).
