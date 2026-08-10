# menhir Temporal Memory - "Chronostratum" Capability Plan

**Goal:** Make menhir genuinely better at temporal processing for agent memory --
especially coding/debugging agents (real use case: debugging a modded RimWorld,
"what broke after I added the CE willow patch, and what did I believe before the
load-order fix?"). **Not** to beat LongMemEval. The benchmark is a sanity check; the
scoreboard is real capability.

> Rev 4: Codex's four-timestamp / world-vs-belief split + Git grounding + the unification
> insight: code-structure nodes and git nodes JOIN on the same `:File`/`:Symbol` identity,
> making structure + git + memory one connected graph. Git is the keystone because it gives
> the moat a backbone that does NOT depend on extraction reliability.

## The clock model: THREE temporal sources
- **World time** -- when a fact was true: `valid_at` / `invalid_at`.
- **Belief / transaction time** -- when *menhir* knew it: `created_at` / `expired_at`.
- **Code time** -- when code actually changed: Git author/commit date. **Authoritatively
  grounds `valid_at` for code-change events** (no LLM date-parsing).

Two of three (belief, code) are hard-grounded. **Do not conflate `invalid_at` with
"superseded":** `invalid_at` = world-validity; `expired_at` = belief invalidation.
```
current-belief recall:     expired_at IS NULL
history / belief-drift:    include expired_at IS NOT NULL
as-of (world time):        valid_at <= as_of AND (invalid_at IS NULL OR invalid_at > as_of)
what-did-we-know-then:     created_at <= known_as_of AND (expired_at IS NULL OR expired_at > known_as_of)
```

## The unification: one graph, joined on :File / :Symbol
menhir already builds a code-structure graph (`:File, :Symbol, :Import, :Dependency, :Test`).
Git's change edges must land on **those exact nodes**, not duplicates. Then a single `:File`
node is the join point of three graphs:
- **structure** (imports / dependents / tests)
- **git** (which commits changed it, when)
- **memory** (what we believed about it, and when)

**Identity reconciliation is the one hard requirement:** git diff paths resolve to the
existing `:File` identity by repo-relative path within the namespace. Files = reliable join
(do in MVP). Symbols = v2 (needs tree-sitter symbol diffs to map a hunk to a `:Symbol`).

**Payoff -- time-aware blast radius (the debugging killer query):** menhir already has
`blast_radius`/dependency analysis; joining git makes it temporal:
> "Test C failed. What does it depend on (structure), and which of those changed between
> when it last passed and now (git)?"  = blast-radius x time.
No generic memory system can answer that; menhir is one identity-reconciliation step away.

## Substrate / under-use
`:Episodic` + `:Entity`; facts are `:RELATES_TO` edges with `valid_at, invalid_at,
created_at, expired_at, reference_time`. `recall_service.py` has NO temporal handling --
dates + supersession never reach the answer model.

## Novelty assessment (honest)
- **Table-stakes:** bitemporal stamps, timestamped facts, anchors/eras.
- **Novel-ish:** world-vs-belief split exposed *to the agent*.
- **Genuinely novel + menhir-specific (the moat):** (1) **Git-grounded, structure-joined**
  temporal memory; (2) ingestion-order independence; (3) partial-order/interval modeling.
- **Cut from v1:** causal edges (unreliable); "memory braid" (vocabulary).

## Gating risk: extraction reliability (Git mitigates)
Conversation-extracted temporal structure is noisy (fact recall 0.40-0.85; `expired_at`
sparse). Git is recorded ground truth -- use it for the code timeline instead of extraction.
Build emergently; prove each rung before the next.

## The ladder

### Rung 1: expose full bi-temporal edge state in recall (KEYSTONE)
**1A** surface `valid_at, invalid_at, created_at, expired_at` per fact in `RecallMemory`
(+ `is_current_belief`, `temporal_role`); direct Cypher if needed. *Verify fields populated.*
**1B** `include_invalidated` flag, default off => `expired_at IS NULL` (filter on
`expired_at`, NOT `invalid_at`). **1C** formatter renders happened-time vs learned-time.

### Rung 2: temporal-aware recall (query side)
`menhir_agentic_recall` (built) detects temporal intent: per-entity date-seeking sub-queries
+ flips `include_invalidated` for history/regression queries.

### Rung 3: ingestion-order independence
`valid_at` authoritative for ordering, independent of `created_at`. Late memories re-thread.
menhir recall/projection layer.

### Rung 4: timeline projection (intervals NOT v1)
v1 point events (valid_at) -> v1.5 optional happened_from/to when stated -> v2 Allen edges.

### Rung 4.5: Git-backed repo-state anchors (the grounding layer)
**MVP (first):** at session start/end capture repo state, snapshot it:
```
git rev-parse HEAD ; git branch --show-current ; git status --porcelain
git diff --name-status ; git diff --stat ; git diff --cached --stat
```
Schema (changes land on EXISTING `:File`/`:Symbol` nodes via path/name reconciliation):
```
(:GitCommit {sha, author_date, committed_at, message, parent_shas, branch_refs, ingested_at})
(:WorkingTreeSnapshot {dirty, staged_diff_hash, unstaged_diff_hash, captured_at})
(:GitChange {change_type, lines_added, lines_deleted, patch_summary})
(:Episode)-[:OBSERVED_REPO_AT]->(:GitCommit)
(:Episode)-[:HAD_WORKING_TREE]->(:WorkingTreeSnapshot)
(:GitCommit)-[:PARENT_OF]->(:GitCommit)
(:GitCommit)-[:CHANGED]->(:GitChange)-[:TOUCHES]->(:File|:Symbol)   # existing structure nodes
(:WorkingTreeSnapshot)-[:TOUCHED]->(:File)
(:FactState)-[:SUPPORTED_BY]->(:GitCommit|:GitChange|:Episode)
```
**Integration point:** the coding agent/harness posts the repo snapshot to a menhir ingest
endpoint at session boundaries. Store diff summaries + hashes by default; full patch optional.
**Caveat (load-bearing):** history is rewritable (rebase/squash/amend/force-push). Git is an
authoritative *anchor*, not a truth oracle -- keep `ingested_at` + changed-files/diff-hash
so a memory survives a vanished SHA. Distinguish author_date / committed_at / ingested_at;
use parent/child topology for order, not dates alone.
**Unlocks:** what changed after X? what files touched when Y started? what commit was active
during this failed session? what did we believe at commit A?
**Later:** full commit-graph topology, branch/merge awareness, tree-sitter symbol diffs,
test-result nodes, blame-aware lookup.

### Rung 5: structure-aware temporal memory (THE MOAT) -- Git-grounded, structure-joined
Commit thesis now; **do not implement before Rungs 1-3 prove the timeline is trustworthy.**
Two clocks (memory clock + git clock), joined on the structure graph:
```
(:EventFrame)-[:OBSERVED_AT]->(:GitCommit|:WorkingTreeSnapshot)
(:EventFrame)-[:TOUCHES]->(:File|:Symbol|:Test|:Dependency)
(:GitCommit)-[:CHANGED]->(:File|:Symbol|:Dependency)
(:FactState)-[:SUPPORTED_BY]->(:GitCommit|:Episode)
```
Separates: bug introduced commit A; symptoms session B; wrong theory session C; fix commit
D; belief corrected session E. Time-aware blast radius across the structure subgraph.

### Later / optional
Temporal anchors/eras as compression nodes. (Deferred) causal edges if extraction proves out.

## Eval: score retrieval AND answer, separately
Per item: (1) retrieved right CURRENT fact? (2) right SUPERSEDED fact when needed?
(3) exposed valid_at vs created_at (world vs learned)? (4) answer reasoned correctly?

**Seed fixture (ingestion-order scrambled, RimWorld):**
```
A (ingested 3rd): Monday - willow crash started after Combat Extended checked LOS.
B (ingested 1st): Wednesday - added CE willow texture-cache patch, thought it fixed it.
C (ingested 2nd): Thursday - realized the patch caused a load-order/compat issue.
D (ingested 4th): real fix was moving the CE willow patch after the plant texture defs.
Q: What broke after I added the CE willow patch, and what did I believe before the load-order fix?
```
Git-backed variant: each episode carries a repo snapshot (changed files
CE_Willow_TextureCache.xml, LoadFolders.xml) joined to the structure graph, so the answer
cites code history + belief history.

## Build order (locked)
1. Rung 1A - expose four timestamps (verify populated).
2. Rung 1B - current vs historical via `expired_at`.
3. Rung 1C - formatter happened-vs-learned.
4. Rung 2 - temporal-intent planner.
5. Rung 3 - timeline sorts by `valid_at`.
6. Rung 4.5 - Git session-snapshot anchors (MVP), joined to existing `:File` nodes.
7. Rung 5 - structure-aware temporal memory, Git-grounded + structure-joined.

## Naming
"Chronostratum" / Chronostratigraphic Memory Graph (menhir = standing stone; archolith = ancient stone).

## Durable identity (Rev 5) -- refines the identity-reconciliation requirement

**Identity != location.** Path / qualified-name are mutable *attributes*; the node needs a
**durable id** so git + belief + structure history follows code across moves/renames
(precisely when "what moved and what broke" matters most). Keying nodes on path orphans
history on every refactor.

- **Files (MVP-able):** assign a stable `file_id` on first sight. Git already detects
  renames -- `R` status in `git diff --name-status`, `git log --follow`, `-M/-C` similarity.
  On a rename, **update the path attribute, keep the `file_id`** -> edges stay attached.
- **Functions / symbols (v2/v3, hard):** no path. Needs content/structure identity --
  tree-sitter + a normalized-body hash + similarity matching to carry a `symbol_id` across
  a move. This is the "moved-code detection" problem; defer, don't block file-level work.
- **Principle:** durable id is the node; location is an attribute; git rename events
  re-point the attribute without breaking edges. The time-aware blast radius only stays
  correct across refactors if identity is durable.

Slots into Rung 4.5 (file durable ids + git rename-following) and Rung 5 (symbol durable
ids, v2).


---

## Reconciliation with the belief plan (Oracle / Warden / Mutator) -- 2026-06-28

Chronostratum is the **temporal signal layer**, not a standalone subsystem. It slots into
menhir's three-object model (see `menhir-research-execution-ladder.md`, frontier worktree):

```
Oracle   observe -> a score/signal over a candidate (read-only)
Warden   guard   -> an operational decision at the assertion boundary (ADMIT/FLAG/ATTENUATE/REFUSE)
Mutator  write   -> the persistence boundary
```

Chronostratum **produces signals**; **Wardens decide**. Mapping of the rungs to code
(built bench-first, pure domain, production graph/recall wiring still GATED):

| Chronostratum rung | module (menhir-frontier `src/menhir/domain/`) | role |
|---|---|---|
| Rung 1A/1B clock model | `temporal.py` (valid_at/invalid_at/created_at/expired_at, matches_query) | SIGNAL producer |
| Rung 1C happened-vs-learned | `temporal.py` `format_temporal_provenance` | projection |
| Rung 2 temporal-intent | `temporal_intent.py` (transparent floor under `menhir_agentic_recall`) | lens selector |
| Rung 3 ingestion-order independence | `temporal.py` `order_by_world_time` | timeline |
| Rung 4.5 git anchors + durable identity (Rev 5) | `repo_snapshot.py`, `git_staleness.py` (ancestry/branch/stash/rename) | SIGNAL producer |
| Rung 5 time-aware blast radius (capstone) | `structure_temporal.py` (StructureTemporalOracle) — steps 1-3 BUILT+benched, step 4 gated | ORACLE (observe->rank) |

**The supersession rule (one producer, named consumers):** validity/supersession is computed
ONCE in `temporal.py` (+ `git_staleness.py` for git-grounded staleness). Consumers: the
`CurrentnessWarden` (`domain/warden.py`) turns it into an assertion decision; the oracle
`TemporalOracle` (R6) turns it into a ranking score. Neither re-derives supersession. This is
where Chronostratum and the belief plan meet: **the clock model + git grounding feed the
Wardens.**

Status: Rungs 1A/1B/1C/2/3/4.5 built as pure-domain + tests (frontier branch
`claude/menhir-chain-handoff-doc-7iuat2`). Rung 5 (time-aware blast radius) has its steps 1-3
(pure-domain oracle + bench) BUILT and graduated; only step 4 (feed the Wardens / production
recall+graph wiring) is gated — see the Rung 5 section below for detail. Production wiring (graph
writes for snapshots, recall integration) stays gated until bench graduation on confirmed fixtures.


## Rung 5 — StructureTemporalOracle: time-aware blast radius (folded back in 2026-07-11)

> **Merge note (2026-07-11, ctharvey-approved).** Rung 5 was briefly split into a standalone
> `menhir-structure-temporal-oracle-plan.md` (2026-06-28) because it sits at Oracle altitude
> (observe -> rank) rather than SIGNAL-producer altitude (Rungs 1-4.5). That split created two
> temporal plans describing one body of work, so it is **folded back here** as the single temporal
> plan of record. The altitude distinction is preserved in the description below; the standalone
> plan is removed.

**Altitude:** an **Oracle** (observe -> score/rank candidates; read-only). It does NOT decide
(Wardens) or write (Mutator). One supersession-producer rule still holds: it *consumes*
`temporal.py`/`git_staleness.py`, never re-derives.

**The capability (the killer debugging query):**
> "Test C failed. What does it depend on (structure), and which of those changed between when it
> last passed and now (git)?"  = blast-radius x time.

Given a failing anchor + a time window, surface the structural dependencies that CHANGED in the
window, ranked. It COMPOSES built domain modules (adds orchestration, not new staleness/identity
logic):

| input | built module | role |
|---|---|---|
| blast radius (callers/callees/tests/deps of the anchor) | `domain/structural_expansion.py` | bounded neighbor set |
| which neighbors changed, ancestry/branch correct | `domain/git_staleness.py` | change grounding |
| the time window (last_passed -> now), valid/created | `domain/temporal.py` | window filter |
| identity survives renames across the window | `domain/repo_snapshot.py` | durable file_id |

**Output:** a ranked candidate set (changed-in-window dependencies of the failing anchor), shaped
as an OracleResult/packet that the **Wardens** then gate before assertion.

**Build plan (bench-first; production graph wiring gated):**
1. Time-windowed change filter — compose `expand_structural` with `derive_structural_staleness`
   restricted to a `[t0, t1]` window. Pure domain.
2. StructureTemporalOracle interface — `QueryContext{anchor, last_passed_at, now, change_log}` ->
   ranked `ChangedDependency` list (directness/recency/centrality). Mirror the oracle interface.
3. Bench — fixture = failing test + dependency graph + git changes in/out of window + an unchanged
   dependency. Metric: in-window-changed-dependency recall vs a structure-only baseline. Gate:
   surfaces the in-window changed dep, excludes out-of-window/unchanged, bounded.
4. **(gated)** Feed the Wardens / recall — wire candidates through `WardenChain`; production graph
   wiring stays gated until bench graduation.

**Discipline / non-goals:** symbol-level blast radius (tree-sitter) deferred — file-level first;
no probabilistic backend (transparent deterministic baseline first); durable identity is
load-bearing (`repo_snapshot.py`, Rev 5) so the answer stays correct across refactors.

**Status (2026-06-28, re-verified 2026-07-11): steps 1-3 BUILT, step 4 gated.**
- **Step 1** windowed change filter — `domain/structure_temporal.py::changed_in_window` (composes
  `structural_expansion` x `git_staleness`, rename-aware). Built, tested.
- **Step 2** oracle interface — `StructureTemporalQuery -> RankedDependency`
  (`domain/structure_temporal.py:120` `class StructureTemporalOracle`, proximity + window-recency +
  change-density, read-only, with rationale). Built, tested (9 domain tests).
- **Step 3** bench — `archolith_bench/r5/` + `fixtures/r5_seed_blast_radius.json` (real yawn.seed
  god-class split). GRADUATES: structure-only ranks a wrong sibling #1 (culprit_at_1=0); the
  time-aware oracle ranks the in-window culprit #1 (culprit_at_1=1, noise=0) — the difference is
  purely RANKING. 3 bench tests.
- **Step 4** feed the Wardens / production recall+graph wiring — GATED (not started), as planned.

Commits: menhir `7fcdf16` (domain), archolith-bench `5f69dbe` (bench). Branch
`claude/menhir-chain-handoff-doc-7iuat2`.
