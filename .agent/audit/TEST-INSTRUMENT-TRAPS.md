# Test-instrument traps and audit method rules

Source: Menhir issue #105 (filed 2026-09-13), copied here verbatim on 2026-11-02 and the issue
closed. It preserved method knowledge from the archived `menhir-open-questions-ledger.md`.
Add new traps to this file, not to the issue.

**Why this issue exists:** the workspace artifact corpus carries three classes of durable method knowledge that lived only inside `menhir-open-questions-ledger.md` (a living companion to the confirmed-findings register). The ledger's *actionable* items were ported to #100–#103; this issue preserves the classes that are **not** findings and will never close as bugs, so the local docs can be archived. Reference material — nothing here is immediately actionable.

## 1. Test-instrument traps (the T-series)

Each trap produced a test that **passed while asserting nothing**, found by mutation rather than review. The recurring meta-rule across all of them: *a test that cannot fail on its own defect proves nothing*.

- **T1** — Neo4j reports operator names as `NodeIndexSeek@neo4j`; comparing against the bare name makes `not in` pass vacuously.
- **T2** — `db.awaitIndexes()` waits for ONLINE, not planner statistics; a plan assertion right after bulk seed can measure the pre-index plan. Seeds need `db.prepareForReplanning()`. Binds every A3 plan-shape item.
- **T3** — `is` is not a copy-detector for interned immutables (short strings, small ints, `True`/`None`); "one concept, N declarations" needs AST/source checks, not identity probes.
- **T4** — a probe set is not a proof: test the *boundaries* of the representations involved (2\*\*53, inf/nan), not a sample of ordinary values.
- **T5** — a stub narrower than the real constructor turns an added kwarg into a `TypeError` elsewhere; grep for hand-rolled stubs of a seam before adding kwargs there.
- **T6** — dbHits is not a proxy for cost once payload size enters (25% fewer dbHits, 3× wall clock — shipped values, not rows).
- **T7** — a Cypher-fragment helper only interpolates inside an f-string; `EXPLAIN` every rendered query after consolidation (four of five sites were sending literal `{helper(...)}` text).
- **T8** — heuristics over RAW source misread pre-existing f-strings (doubled braces); confirm against the RENDERED string.
- **T9** — reaching a fallback by patching the names under test proves nothing about what the module BINDS; load a second copy under a private name with the dependency blocked at import.
- **T10** — asymmetry between adjacent call sites is a decision at least as often as a defect; execute the path or read the value before grading it HIGH.
- **T11** — version premises age: "Neo4j can't X" claims need the engine version stated and checked.
- **T12** — a concurrency test that never constructs the interleaving passes against the bug; to test a race you must own when the awaited thing resolves.
- **T13** — `asyncio.Lock` check-then-acquire is atomic on a single-threaded loop (2,000 trials, max concurrency 1); measure before filing TOCTOU shapes.
- **T14** — source-scanning invariant guards constrain NAMES, not dataflow (`resolved_x` passes, honest naming fails).
- **T15** — assert the NUMBER interpolated into operator text, not just the flag it feeds (10 reported where 5 existed).
- **T16** — a read inside a new write transaction self-deadlocks on lazy DDL; warm every store the transaction touches before locking.
- **T17** — a "SSOT is gone" test that only checks the old name doesn't bite; substitute the survivor and assert the call site delegates.
- **T18** — a name imported at module scope cannot be patched at its source module; check the binding site before concluding the code is wrong.
- **T19** — a baseline captured from the EDITED function pins the regression; baselines must come from git.
- **T20** — `class X(str, Enum)` formats as `X.MEMBER` under `str()`/f-strings while keeping `==`/`json.dumps`; check formatting, not just equality. `StrEnum` is the drop-in.
- **T21** — schema generators can delete `IF NOT EXISTS` DDL while every test passes (only fresh installs see it); pin the full statement list verbatim.
- **T22** — calling a module's bound reference to a guard doesn't prove it CALLS it; execute the module fresh with a recorder.
- **T23** — a cache fix trading one leak for another: whenever a fix starts RETAINING a resource, ask what closes it on every eviction path.
- **T24** — a quantified failure of ADDRESSABILITY is not a measure of EXPOSURE; characterize a sample before a row count becomes a severity (1.3M of 1.8M "unaddressable" rows were wake-sequence telemetry capped at 52 chars).
- **T25** — check whether the mechanism already exists before filing (a "missing" three-way purge outcome shipped under CF-165 twenty lines below the code read).
- **T26** — writing the hazard into a comment is not sweeping for it; when a fix is an instance of a general shape, search for the shape and add a ratchet anchored to real syntax (a guard that flags its own docs gets deleted).
- **T28** — a creation-site predicate describes new rows, not the rows already in the graph; "X cannot exist because the writer checks Y" is a claim about the future needing a count.
- **T29** — `count(r)` is not `count(DISTINCT b)`; fan-out makes an edge census overstate the node population 22× once — name which one a sentence reports.
- **T30** — a node's SHAPE is not its KIND: a finding that asserts lost content must show the content, not the exclusion (structure rows excluded from recall is intended behavior of the property read as the defect).
- **T31** — a read path built on a store helper inherits that helper's initialization WRITES; enforce "does not write" at the connection (`mode=ro`), not by auditing the statements you wrote.

## 2. Working-tree hazards (lanes + orchestrator share one checkout)

- A lane reported a `NameError` on a constant that existed — it read the file mid-edit. Verified as a race, not a defect (3 clean runs after).
- Staging by path (`git add <file>`) can sweep a lane's in-progress edits into the orchestrator's commit when both touch one file (CF-189's source landed inside CF-190's commit that way). Prefer finishing and killing a lane before committing anything in a file it owns.

## 3. Standing method notes

- **Before writing `NOT RUN`, check what it would actually cost to run.** Two items sat as NOT RUN on "no Neo4j available" while the throwaway test container (`docker-compose.test.yml`, bolt :7688) was one `docker compose up -d` away — and one turned out to be a *worse* finding than its parent entry. The test instance answers Neo4j semantics/query plans; only the production graph answers corpus questions. Different blockers, different costs.
- **Do not commit a test change without re-running the mutation against it.** A test edit is a change to the thing that decides whether code is right; it gets the same inverse-test as code. A lane's post-fix "improvement" that avoided `importlib.reload` was vacuous — the mutation took one command.
- **A reason to defer is a claim and decays like a status stamp** — re-read it against the sites before trusting it (CF-48: one sentence written for a group was inherited by every member, including the cheapest site).
- **Assert on what executed, not what was recorded** — three separate controls have been found incrementing correctly while structurally unable to fire.
- **A recorded blocker is a claim with a timestamp** — re-measure before planning against an entry (CF-73's "no graph to verify against" was false the whole time; CF-115 overstated 8×).

## 4. Ledger sections now fully superseded or ported (so this issue is the only carrier)

- §A (settleable with test instance): every item SETTLED 2026-08-20/21/23 — A1 `size([])=0` non-NULL; A2 `neo4j.time.DateTime` TypeError, fixed `db8743f0`; A3 six plan-claims measured (CF-201/203/204 outcomes recorded at register; CF-160 refuted, regraded M→L); A4 surrogate-encoding reachable+contained, `83a38a7e`, cost = one silently lost audit row, second-order flaw filed CF-255; A5/A6 settled.
- §B (production-gated): B1 re-counted → CF-248 (characterized, not blocking); B2 settled 2026-08-24 (population exactly 60, zero overlap-branch admissions, side-finding CF-256); B3 → #100.
- §C: C1 control SHIPPED (`d58403d1`, CF-236+CF-194 fix — delimiter neutralization); C2 → #101; C3 not a regression (needs_llm lane).
- §D: D1 → #102; D2 settled by CF-127 owner ruling (structure entities = shared silo; residuals: 107-predicate ratchet, CF-257, documented coalesce).
- §E/F: accepted limitations of shipped fixes (CF-182/195/89/108/14/84/143/139/193 …) recorded at the register entries; F5 → #103.
- §G (wrong-guidance annotations): lives in the register entries themselves.
