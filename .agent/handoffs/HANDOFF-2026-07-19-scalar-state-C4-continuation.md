# HANDOFF — ScalarStateView Piece C.4 continuation (no-context brief)

**Date:** 2026-07-19
**For:** a fresh session with ZERO prior context, continuing the ScalarStateView C.4 work.
**Read this fully before touching code.** It is self-contained; you should not need the prior
conversation. For exhaustive design detail, the source of record is the plan (see paths below).

---

## 0. TL;DR — where to start

1. **C.4.1 is FROZEN. C.4.2 is DONE and AWAITING REVIEW. C.4.3 is the next piece to build.**
2. The USER acts as a rigorous code reviewer between sub-pieces. **Do not start C.4.3 until the
   user has reviewed and signed off on C.4.2.** If the user says "start C.4.3", build it; if they
   post a review, apply the fixes and re-submit before moving on.
3. Every piece: implement → `pytest` affected suites → `ruff` → commit → push `main` → **watch CI**
   → update the plan doc → report CI status explicitly. Then stop for review.

---

## 1. What this feature is (one paragraph)

**ScalarStateView** graduates a validated bench finding into production Menhir: a typed-scalar,
entity-anchored "current value" memory (one register per `(entity, attribute, scope, value_kind,
unit)` slot) over 9 ValueKinds (boolean, status, count, duration, frequency, money, measurement,
clock_time, weekday). The spine invariant: **perception may be probabilistic; folds and Views must
stay deterministic.** An LLM turns prose into typed *proposals*; everything after (voting, the fold,
the durable event log) is deterministic and rebuildable from a persisted `:TypedAssertion` event log.

Pieces A, B, C.1, C.2, C.3 are complete and frozen. **C.4** is perception extraction — the LLM front
door that produces proposals and feeds them (once bound + persisted) into the proven C.1–C.3 machinery.

---

## 2. Repos, paths, branches

| Thing | Location |
|---|---|
| Menhir code | `projects/archolith/menhir/` — repo remote `Archolith/menhir`, branch **`main`** |
| Plan (source of record) | `projects/archolith/.agent/plans/menhir-scalar-state-view-implementation-plan.md` — repo remote `ctharvey/archolith-workspace`, branch **`master`** |
| Design plan | `projects/archolith/.agent/plans/menhir-scalar-state-view-design-plan.md` |
| This handoff | `projects/archolith/menhir/.agent/for-review/HANDOFF-2026-07-19-scalar-state-C4-continuation.md` |

**Two separate git repos.** Code changes commit to `menhir` (`main`); plan updates commit to the
`archolith` workspace (`master`). Keep them in sync — each code piece has a matching plan update.

**Current HEADs at handoff time:** menhir `ee5939f`; archolith-workspace plan `be67066`.
(`ee5939f` is an unrelated memory-server resilience fix, not part of C.4.)

---

## 3. Working discipline (non-negotiable)

- **One project per session** (menhir).
- **The merge saga (`MergeCoordinator`) is fingerprinted — DO NOT MODIFY IT.** Scalar reconciliation
  hangs off best-effort post-COMMIT hooks only, so it can never fail a committed merge/unmerge.
- **MCP config is generated** — never hand-edit `.mcp.json`/`opencode.json`/TOML. (Not relevant to
  C.4 code, but a standing rule.)
- **Commit convention:** conventional commits (`feat:`/`fix:`/`docs:`), stage files by explicit path,
  end commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **CI note:** pushes to `main` trigger `.github/workflows/tests.yml` (`unit-tests` job). These are
  PUSH runs on `main`, so a GitHub *PR-run* connector will show nothing — **always confirm CI via
  `gh run list` / `gh run watch` and report the result explicitly.**
- **No live Neo4j in this environment.** All C.4 tests are offline (LLM injected as a fake, and
  `FakeNeo4j`/in-memory fakes). State this honestly: uniqueness constraints, MERGE atomicity, and
  real concurrency are NOT exercised here — a live-Neo4j integration test is the C.4.4 final gate.
- **`perception.py` (the existing numeric counter boundary) must stay byte-identical when
  `enable_scalar_state` is off.** C.4 lives in a SEPARATE module so the counter path is untouched.

### Verification commands (from `projects/archolith/menhir/`)
```bash
.venv/Scripts/python.exe -m pytest tests/test_typed_scalar_perception.py tests/test_typed_scalar_gate.py -q -p no:cacheprovider
ruff check src/menhir/services/typed_scalar_perception.py
# CI after push:
gh run list --limit 1 --json databaseId --jq '.[0].databaseId'   # then: gh run watch <id> --exit-status
```

---

## 4. Status ledger

| Piece | Status | Commits (menhir) |
|---|---|---|
| A (ViewKind) + B (keying) | DONE | `9c3996d`, `3035349` |
| C.1 typed-assertion repo | DONE/FROZEN | (earlier) |
| C.2 fold + rebuild | DONE/FROZEN | (earlier) |
| C.3 merge rebinding + activation gate | DONE/FROZEN | `478aa87`, `d2bb87c`, `ff61dee`, `f21ea32` |
| **C.4.1** proposal schema + parser | **FROZEN** | `e3d1727`, `641569a`, `a8f9b45`, `c029621` |
| **C.4.2** k-sample gate | **DONE — awaiting user review** | `fd162cd` |
| **C.4.3** binding + persistence + flag | **NOT STARTED (next)** | — |
| **C.4.4** transitive repair + live-Neo4j gate | NOT STARTED | — |

---

## 5. C.4 architecture you must know (so you don't re-derive)

All C.4 code is in **`src/menhir/services/typed_scalar_perception.py`** (a NEW module; `perception.py`
untouched). Tests: `tests/test_typed_scalar_perception.py` (C.4.1), `tests/test_typed_scalar_gate.py`
(C.4.2).

### 5.1 The proposal (C.4.1, FROZEN)
`TypedScalarProposal` = one UNBOUND, well-typed, grounded observation from a single extraction pass.
`extract_typed_scalars_once(episodes, llm_complete) -> list[TypedScalarProposal]` is pure given an
injected LLM. Fail-closed contract (a bad row is DROPPED, never raised):
- required fields (`subject`, `attribute`, `operation`, `stated_span`) must be REAL non-blank strings
  (no defaults, no coercion of non-strings); `operation` explicitly present; `episode` a real
  in-range int (bool/fractional excluded); `attribute` canonical snake_case;
- value passes the domain `validate_value` (finite numbers only — NaN/±inf and oversized ints that
  would `OverflowError` in `math.isfinite(float(x))` are rejected; clock_time is a real 00:00–23:59;
  `clock_time` is canonicalized to zero-padded `HH:MM`);
- any `when` must fully parse as ISO (`_parse_iso_strict`, NOT the tolerant fold reader), else
  dropped; absent/blank `when` → `None` (C.4.3 assigns the time basis);
- `stated_span` must occur **exactly once** in the episode text (unique grounding → located offsets;
  zero/multiple → dropped). So `claim_ordinal` is always 0 and identity is order-independent.
- `scope`/`unit` are OPTIONAL modifiers, deterministically canonicalized (`_canon_modifier`: lower,
  runs of whitespace/hyphens → single underscore).

**Shared identity helper:** `menhir.domain.typed_assertion.build_source_key(episode_uuid, span_start,
span_end, claim_ordinal)`. Both `TypedScalarProposal.source_key` and `TypedAssertion.source_key` call
it, so a proposal's predicted durable identity cannot drift from what the store persists.
`source_key` is BINDING-STABLE (no subject_uuid) — survives merge rebinding.

### 5.2 The gate (C.4.2, DONE — awaiting review)
`gate_typed_scalars(samples: list[list[TypedScalarProposal]], threshold=1.0) -> list[TypedScalarDecision]`.
**Source-claim-first voting:**
1. GROUP the k samples by `source_key` (so unrelated unbound proposals — e.g. "Alice owns 2 cats" vs
   "Bob owns 2 cats", which live in different episodes/spans — are never collapsed before binding).
2. Within a source claim, each sample casts AT MOST ONE vote for its `interpretation_label` =
   normalized `subject_text` + attribute + scope + value_kind + unit + operation + normalized_value +
   `when`. A sample that read the claim TWO different ways is internally conflicted → casts NO vote
   (`_VOTE_CONFLICTED`); a sample that didn't perceive it → `_VOTE_ABSENT`. Both sentinels stay in
   the denominator (always `k`), so omissions/conflicts count AGAINST agreement.
3. `agreement = modal real interpretation / k`; commit iff `>= threshold` (default 1.0 = unanimous).
   Sentinels dilute but never win.
4. Defense-in-depth: never commit a winner without a located span (C.4.1 guarantees it).
5. Deterministic: first-seen `source_key` order; representative = first-seen proposal for the winning
   interpretation (so 37 int vs 37.0 float, which normalize equal, commit the first typed value).

`TypedScalarDecision` carries: `source_key, committed, reason, veto, agreement, k, distribution,
proposal (representative | None), evidence_tier`. **`evidence_tier = "agent"`** — perception is ALWAYS
the lowest tier; higher tiers come from other paths, never extraction. (Read-time effective tier is
still the weakest required contributor computed by the C.2 fold; never trust the stamped tier.)

---

## 6. C.4.3 — the NEXT piece (full spec)

**Title:** `feat(scalar-state): bind, persist, and rebuild committed typed scalars + enable_scalar_state`.
Still one reviewable sub-commit. Goal: take committed `TypedScalarDecision`s, bind them to resolved
entities, persist them as `:TypedAssertion`s, rebuild the ScalarStateViews, all behind a flag.

### 6.1 Post-finalization entity binding (fail-closed)
- There is NO Menhir `resolve(subject)->uuid`. Graphiti resolves entities internally in
  `add_episode()`; but the FINAL identity is post-correlation/merge. **Bind against the surviving
  entities via `fetch_linked_entity_uuids_for_episode(resolved_episode_uuid)`** (survivor, not the
  absorbed node). Confirm the exact method name/signature in the codebase before use
  (`grep -rn "fetch_linked_entity_uuids_for_episode" src`).
- Binding rule: match `proposal.subject_text` against the episode's final linked entities.
  **Abstain from authority on ZERO or MULTIPLE candidates.** A uniquely bound `subject_uuid` →
  persist a fully-bound assertion that can materialize a View. Zero/multiple → persist as an
  ADVISORY event-log entry with `binding_pending=true` (NO display-text-keyed View — that would
  recreate the rejected lexical sidecar).

### 6.2 Persist + rebuild
- Build a `TypedAssertion` (domain object, `src/menhir/domain/typed_assertion.py`) from the decision's
  representative proposal + bound `subject_uuid` + `evidence_tier="agent"` + `valid_at` (from `when`,
  or the time basis when `when is None` — `episode_reference` / `learned_fallback`, see `time_basis`
  field + `TIME_BASES`).
- Persist via `TypedAssertionRepository.record_assertion(...)`
  (`src/menhir/infrastructure/typed_assertion_repository.py`) — this is the C.1 durable store; it is
  atomic (head MERGEd on `source_key`) and idempotent. Then call
  `ScalarStateService.rebuild_scalar_state(subject_uuid)` (`src/menhir/services/scalar_state_service.py`)
  to fold the log into the View. Re-running perception over the same episode is a no-op (same
  `assertion_key`).

### 6.3 The `enable_scalar_state` flag (off ⇒ byte-identical)
- Mirror the existing `enable_*` levers. When OFF, the typed-scalar path does not run at all;
  `perception.py` behavior is unchanged. Wire it wherever perception is invoked in the ingest/
  enrichment pipeline (`grep -rn "perceive_and_fold\|enable_" src/menhir/services` to find the seam).

### 6.4 ACTIVATION ORDERING (hard requirement — from the C.3 review)
When the flag is on, **`MemoryGraphAdapter.activate_scalar_state()` MUST run and pass BEFORE**:
perception workers record any assertion, the merge/unmerge hooks are installed, and repair jobs start.
- `activate_scalar_state()` is the gated path (`src/menhir/infrastructure/memory_graph_adapter.py` →
  `TypedAssertionRepository.activate_scalar_state`). It is EXACT-MATCH fresh-only: it refuses
  (`ScalarStateActivationError`) if ANY `:TypedAssertion`/`:TypedAssertionHead` exists whose
  `identity_version != IDENTITY_VERSION` (unstamped/older/newer all fail closed). It also creates the
  source_key identity DDL (which is deliberately OUT of the unconditional bootstrap).
- **Call the gate even when the required indexes already appear online** (do NOT skip on a
  `scalar_state_schema_ready()` short-circuit), then verify `scalar_state_schema_ready()` AFTER
  activation — so a rollback or hand-altered DB can't bypass the identity-version check.
- Operator escape hatch for a dev store: `purge_scalar_state_nodes()` (deletes the whole footprint —
  event log + all `kind='scalar_state'` Views).

### 6.5 Tests (offline)
Flag-off parity (counter path byte-identical); unique-binding → bound View; zero/multiple → advisory
`binding_pending`, no View; abstention NO-OP; idempotent re-perception; activation-ordering (gate
runs before record; refuses over legacy; verifies schema_ready after). Live Neo4j is NOT available —
disclose that binding/persist tests use fakes.

---

## 7. C.4.4 — after C.4.3 (spec summary)

1. **Transitive orphan repair across merge chains** (carried C.3 obligation): if A→B's post-commit
   scalar hook FAILED but B→C's SUCCEEDED, repairing A→B must REPLAY the downstream B→C reconciliation
   **even though B→C already has a `:ScalarReconcile` receipt**. The current
   `repair_incomplete_reconciliations` only re-runs ops lacking a receipt of their own kind; extend it
   to walk the merge lineage forward from a repaired op and re-run downstream reconciliations.
2. **Scheduled orphan-rebind repair pass** over `orphaned_subject_uuids` resolving each to its
   survivor via merge lineage.
3. **Live-Neo4j integration test** (the final gate before shipping): merge→rebind→unmerge→restore with
   uniqueness constraints ACTIVE, covering record concurrency. This is the one thing the offline
   suite cannot prove.

Then a small docs commit recording Piece C as shadow-build-only. **Piece D** (recall composer
authority) is a separate, later effort — gated, staged shadow→counterfactual→canary. Do NOT start D
as part of C.4.

---

## 8. Gotchas / traps

- **Do not touch `MergeCoordinator`** (fingerprinted). Reconciliation is post-COMMIT best-effort only.
- **`validate_value` is the DURABLE validator** (guards `:TypedAssertion` too) — any change there has
  blast radius across all scalar-state code; run the full scalar-state suite after touching it.
- **`_parse` in `fold_algebra.py` is TOLERANT by design** (truncates to 10 chars, stringifies
  non-strings) — do NOT use it for the precision-first perception `when`; C.4.1 uses its own strict
  `_parse_iso_strict`.
- **CI connector blind spot:** PR-run endpoints don't see push-on-`main` runs. Always report CI
  explicitly from `gh`.
- **Memory MCP server** (`127.0.0.1:8090`, `menhir.cli serve`) is a live local service; on 2026-07-19
  it jammed on a locked SQLite telemetry DB. If memory tools hang, restart via
  `scripts/start-server.ps1 restart` (see `HANDOFF`/todos). Unrelated to C.4 code, but you'll be using
  those tools.

---

## 9. First actions for the fresh session

1. Recall memory: search "ScalarStateView C.4" (the memory system holds detailed per-piece notes).
2. Read the plan §3, §6, §10 (Deliverable order, C.4 sub-split) in the archolith-workspace plan doc.
3. Confirm C.4.2 review status with the user. If a review is pending, wait for it; if fixes are
   requested, apply → re-verify → re-submit before proceeding.
4. When cleared, build **C.4.3** per §6 above: explore the exact binding seam first
   (`fetch_linked_entity_uuids_for_episode`, the perception invocation site, the `enable_*` pattern),
   THEN implement. Delegate single-file mechanical parts if useful; keep multi-file wiring yourself.
5. Verify (offline suites + ruff), commit to menhir `main`, push, watch CI, update the plan doc on
   archolith `master`, report CI explicitly, then stop for review.
