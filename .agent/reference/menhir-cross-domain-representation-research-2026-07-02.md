# Cross-domain representation research — ingestion, consolidation, and View techniques for Menhir

**Date:** 2026-07-02 · **Type:** frontier architecture review (mechanisms, not metaphors)
**Bias (given):** remove complexity; prefer abstractions that replace multiple special cases over new features.
**Accepted premises:** read-time reranking exhausted · write-time representation is the lever · events immutable ·
deterministic folds → generic Views · Views additive, never replacements · probability only at the perception boundary.
**Grounding:** `.agent/reference/fold-algebra.md` (σ/ρ/δ, four monoidal reducers), `.agent/architecture.md`,
D0 results (`archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`), `view_repository.py`, `recall_service.py`.

Each idea: **(1) name · (2) source domain · (3) mechanism · (4) why it helps · (5) where it fits ·
(6) complexity · (7) risk · (8) novelty-for-Menhir · (9) verdict · (10) smallest falsifying experiment ·
(+) what it removes/simplifies** — the last field is the admission criterion.

---

## A. The log is the system

### 1. Kappa-style replayable perception (perceiver versioning + `refold`)
2. **Source:** streaming systems — Kappa architecture (Kreps), "the log is the database".
3. **Mechanism:** keep only the immutable log + deterministic reprocessors; every derived store is a disposable
   cache rebuilt by replaying the log through the (versioned) processor. Two stamps make this real: typed events
   carry `perceiver_version` (model + prompt + schema hash), and a generic `refold(view_key | namespace)` op
   re-derives any View from its event set.
4. **Why it helps:** perception WILL improve (Arm B is v0). Today a better detector strands old episodes at old
   quality. With versioned perception + deterministic folds, upgrades are a *replay*, not a migration: re-perceive
   raw episodes where `perceiver_version < current`, re-fold, done. Also the universal repair tool — any View bug
   ends in "refold", not hand-Cypher.
5. **Fits:** ingest (stamp) + one repository op. No new node types.
6. **Complexity:** S (stamp) + M (refold over MENTIONS/episode sets).
7. **Risk:** low; replay cost is bounded by namespace.
8. **Novelty:** low as an idea, high as an operational guarantee Menhir doesn't yet state.
9. **Verdict: Prototype immediately** (the stamp now; refold with the first perception upgrade).
10. **Falsifier:** take the 14-Q counting slice, delete all Views, `refold` from typed events, diff against the
    originals — any mismatch means folds aren't actually deterministic/total and the whole premise needs repair.
+ **Removes:** the entire future "View migration" problem class; per-kind repair scripts.

### 2. Watermarks + event-time discipline at the fold layer
2. **Source:** stream processing — MillWheel/Flink watermarks; event-time vs processing-time.
3. **Mechanism:** aggregations are correct under out-of-order arrival only if (a) merges compare *event time*, and
   (b) each materialized result carries a watermark — "complete up to T; later data may still revise".
4. **Why it helps:** the fold-algebra design already found the concrete bug: `_write_version` supersession is
   arrival-ordered (no `valid_at` guard) — the to-watch 25/20 hazard. The transfer adds the missing half: a
   `view_complete_until` stamp so a consumer (and the belief gate) can distinguish "current as of T" from
   "final". Late events either merge (order-insensitive reducers) or supersede-only-if-newer (EXTREME/LWW).
5. **Fits:** `_write_version` (one guard) + one View property. Kind-agnostic.
6. **Complexity:** S.
7. **Risk:** low; it deletes a correctness hazard.
8. **Novelty:** low — which is the point; this is settled engineering Menhir can import wholesale.
9. **Verdict: Prototype immediately.**
10. **Falsifier:** replay the counting-slice assertion events in shuffled order; measure wrong-current rate with
    and without the `valid_at` guard. If shuffled order never occurs in practice (single ordered ingest), deprioritize.
+ **Removes:** the unstated "callers must feed events in temporal order" contract (a landmine, not a contract).

### 3. CRDT semilattice reconcile (the reconcile IS a lattice join)
2. **Source:** distributed systems — state-based CRDTs (Shapiro et al.): LWW-register, G-Counter, G-Set, OR-Set.
3. **Mechanism:** if merge is associative, commutative, and idempotent (a join-semilattice), then concurrent,
   duplicated, and out-of-order writes all converge without coordination. The CRDT catalog is exactly Menhir's
   kind catalog: counter/stated-total = LWW-register, event-count = G-Counter, distinct = G-Set,
   timeline = grow-only sequence keyed by (when, id).
4. **Why it helps:** Menhir is quietly becoming multi-writer (scheduler bridges + consolidation detectors + future
   agents writing Views concurrently). Declaring each ViewKind's merge as a lattice join makes multi-writer safety
   a *property*, not a hope — and it subsumes the fold algebra's Law-1/Law-2 as the two ways a merge fails
   idempotence/commutativity.
5. **Fits:** a `merge(state, state)` method on `ViewKind`; `_write_version` calls it instead of blind supersede.
6. **Complexity:** M.
7. **Risk:** low-medium — G-Counter needs per-writer partitions or provenance dedup (Law-2); don't fake it.
8. **Novelty:** medium.
9. **Verdict: Research further** (adopt the catalog naming now; implement merge when a second concurrent writer exists).
10. **Falsifier:** run two writers recording the same counter interleaved; if the single-writer assumption is
    enforceable forever (one scheduler, one lease), the machinery is unnecessary — archive.
+ **Removes:** per-kind ad-hoc reconcile logic; replaces it with one law ("merge is a join").

### 4. Tombstones / AGM contraction — the `Ceased` event primitive
2. **Source:** belief revision (AGM postulates: revision vs *contraction*) + LSM-tree tombstones.
3. **Mechanism:** revision replaces a belief with a new one; contraction *retracts without replacement*. Storage
   engines implement retraction as a first-class record (tombstone) that flows through compaction like any write.
4. **Why it helps:** Menhir can currently say "the value changed" but not "the thing ended." "I sold my bike" is not
   `bikes=3` and not `bikes=0` stated — it is a contraction on an ownership key. One event primitive
   (`Ceased(key, when)`) + one fold rule (a tombstone supersedes the current version with an *ended* state, kept,
   provenance-linked) enables many Views: preference cessation, project closure, ownership transfer, deprecation
   of a code pattern, "we stopped using X". Satisfies the multiple-Views bar easily.
5. **Fits:** event schema (one type) + `_write_version` (ended-state supersession) + recall filter already excludes
   non-current Views, so ended state falls out of the existing supersession machinery.
6. **Complexity:** S-M.
7. **Risk:** medium — perception must distinguish "stopped being true" from "stopped being mentioned"; require an
   explicit cessation statement (abstain otherwise).
8. **Novelty:** high for Menhir; the current model literally cannot represent this.
9. **Verdict: Prototype immediately** — it is the smallest missing verb in the belief lifecycle.
10. **Falsifier:** count LME knowledge-update questions (and real agent transcripts) whose gold answer requires a
    retraction rather than a new value. If ~zero, archive until demand.
+ **Removes:** the future temptation to encode endings as magic values (0, "none", "n/a") in scalar slots.

---

## B. Identity and keying (the load-bearing implicit layer)

### 5. Identity View — entity resolution as a deterministic fold (union-find)
2. **Source:** union-find / e-graphs (equality saturation, `egg`) / database record linkage.
3. **Mechanism:** equivalence is maintained incrementally by a union-find structure: assertions `SameAs(a, b)` are
   unions; `find(x)` yields a canonical representative; congruence closure extends merges through structure.
   Union is a join-semilattice (fits #3); the whole structure is rebuildable from the assertion log (fits #1).
4. **Why it helps:** every keyed fold is only as correct as its key. "my trek" / "the new bike" / "bike #2" must
   map to one identity before SET/COUNT dedup or per-subject LWW can be right — the 6 dedup-count misses in the
   D0 slice are *identity* work as much as counting work. Today keying is smuggled inside perception (unversioned,
   unreplayable, invisible). Transfer: perception emits only *identity assertions* (probabilistic, at the boundary,
   with provenance); a deterministic IdentityView folds them; **every other fold keys through `find()`**.
5. **Fits:** a new deterministic layer between Typed Events and Fold — "Key Resolution" — plus one View kind
   (the union-find state, payload-backed). Identity changes are events, so re-keying = refold (#1).
6. **Complexity:** M (union-find is trivial; re-keying existing Views on merge is the real work — but that is
   exactly `refold` on the affected keys).
7. **Risk:** medium — wrong merges poison folds; mitigate with #10 (span-grounded assertions) and by keeping
   merges reversible via replay (drop the bad assertion, refold).
8. **Novelty:** high.
9. **Verdict: Prototype immediately** — highest-leverage idea in this review.
10. **Falsifier:** hand-label identity clusters for the 6 dedup-count LME questions; measure fold accuracy with
    perception-internal keying vs SameAs+union-find keying. No delta → identity wasn't the bottleneck; archive.
+ **Removes:** per-fold keying heuristics; makes "keying granularity is a perception decision" (a flagged wart)
  into a versioned, auditable, replayable decision.

### 6. Reaching definitions — code archaeology as an EXTREME fold over change events
2. **Source:** compiler dataflow analysis (reaching definitions, def-use chains).
3. **Mechanism:** for each symbol/file, the "reaching definition" at time T is the latest definition event ≤ T;
   def-use chains link definitions to their uses.
4. **Why it helps:** code archaeology ("who last changed X, why, and what used it") is on the optimize-for list and
   is currently answered by ad-hoc git spelunking at read time. Git commits are already an immutable event log —
   ingest them as typed `Defined(symbol, commit, when)` events; the CurrentDefinition View is just
   EXTREME(valid_at) keyed by symbol; def-use = MENTIONS-style provenance. Zero new algebra.
5. **Fits:** a new event *source* (git adapter already exists for staleness), reusing the counter/current machinery.
6. **Complexity:** M (ingest volume management; key by file+symbol via the structure graph).
7. **Risk:** low-medium — event volume; mitigate by folding at ingest and not stamping every def into recall
   (only the current ones).
8. **Novelty:** medium.
9. **Verdict: Research further** (build when a code-archaeology query class is actually measured, via #14's catalog).
10. **Falsifier:** D0-style probe — take 10 real "who/when/why changed X" questions from session history; measure
    answer cost with and without the materialized CurrentDefinition View.
+ **Removes:** read-time git walks (the `git_staleness` machinery becomes a consumer of a View instead of a
  bespoke recall-time pass — an existing special case absorbed by the generic architecture).

---

## C. Perception precision without polluting the folds

### 7. Span-grounded extraction verification (a deterministic verifier between perception and fold)
2. **Source:** information extraction faithfulness / metamorphic testing / proof-carrying code (in spirit:
   claims must carry checkable evidence).
3. **Mechanism:** an extractor's claim is admitted only if it carries a *span citation* that a deterministic
   checker can verify against the raw event: the stated value must literally occur (post-normalization: "25",
   "$400k"→400000) in the cited span; the subject tokens must occur nearby. Claims failing the check are rejected
   or demoted to the CANDIDATE tier — never folded.
4. **Why it helps:** Arm B's residual risk is exactly this: over-extraction (iPhone=1 "counts", transient $20
   costs) and the aggregation plan's "wrong current-state fact out-ranks truth". A verifier converts perception
   precision from a model property into a *pipeline* property. The LLM stays probabilistic; the gate is
   deterministic; the fold layer stays clean — precisely the boundary the architecture prescribes.
5. **Fits:** between Perception and Typed Events. Typed events gain `span` provenance (already implied by
   episode_uuids; this sharpens it to character offsets).
6. **Complexity:** S-M.
7. **Risk:** low — worst case it rejects true claims (recall loss), which for write-time state facts is the safe
   failure direction (D0 precision guard logic).
8. **Novelty:** medium.
9. **Verdict: Prototype immediately** (it is the productionizing step Arm B already asked for, with `value>1` /
   no-transient filters as its first two rules).
10. **Falsifier:** re-run the Arm B detector + verifier on the 12 held-out non-counting namespaces; if
    over-extraction (23 emissions) doesn't drop materially at unchanged true-positive rate, the gate is dead weight.
+ **Removes:** ad-hoc post-hoc filters accumulating inside each detector; one gate, all extractors.

### 8. N-version perception consensus (vote before you fold)
2. **Source:** fault tolerance — N-version programming / TMR; ensembles as redundancy, not accuracy.
3. **Mechanism:** run K cheap, *diverse* extractors (different prompts/models/temp-0 seeds) over the same episode;
   admit a typed event only on quorum agreement of (key, value); disagreement → CANDIDATE tier or abstain.
   Deterministic given the votes.
4. **Why it helps:** stated-total perception is already 5/5, so this is not for move-1; it is for *new, riskier
   extractors* (identity assertions #5, cessations #4, causal links #9) where a single-model error poisons a
   deterministic structure. Consensus is the cheap insurance for exactly the extractors this review proposes.
5. **Fits:** perception boundary only.
6. **Complexity:** S (orchestration) but K× inference cost.
7. **Risk:** medium — correlated errors across "diverse" LLMs are common; diversity must be real (different
   families, not different seeds).
8. **Novelty:** low-medium.
9. **Verdict: Research further** — apply selectively to structure-poisoning extractors, never wholesale.
10. **Falsifier:** on the Arm B slice, measure whether 3-way quorum beats 1-shot + span verification (#7) on
    precision per dollar. If #7 alone matches it, archive.
+ **Removes:** nothing (adds cost) — hence selective use only. Weakest of the perception ideas; kept for the
  poison-sensitive extractors.

### 9. Causal parent pointers — happened-before as an event field
2. **Source:** distributed tracing (span parent ids) / Lamport happened-before.
3. **Mechanism:** every span carries its parent; causality is recorded at *emission time* (when it is cheap and
   known) rather than reconstructed at query time (when it is expensive and lossy).
4. **Why it helps:** one field on typed events — `caused_by: [event_ids]`, filled by perception when the text
   states causation ("because", "after the deploy failed", retry-of) — enables a family of Views: retry chains
   (`SUM` over chains), root-cause closure (#13 lineage), recurring failure sequences (#11 DFG gets true edges,
   not just temporal adjacency), reversion tracking. Multiple Views from one primitive: passes the schema bar.
5. **Fits:** event schema + perception; folds consume it later.
6. **Complexity:** S (record now, exploit later — the classic tracing move).
7. **Risk:** low — an unused field costs almost nothing; a hallucinated causal link matters only once a View
   consumes it (gate with #7: causal claims need a span).
8. **Novelty:** medium.
9. **Verdict: Prototype immediately** (the field), research (the Views).
10. **Falsifier:** sample 50 agent-session events; if <10% have statable causal parents, the field will be too
    sparse to ever support a View — archive.
+ **Removes:** future read-time causality reconstruction (an LLM guessing "what led to this" over retrieved prose).

---

## D. The fold algebra — generalizations that earn their keep

### 10. MAP(key → ρ) — the keyed-monoid reducer (one addition, many closures)
2. **Source:** MapReduce / group-aggregate in query engines / indexed monoids.
3. **Mechanism:** lift any reducer ρ to a keyed family: `MAP(k, ρ) : [Event] → {k → ρ-state}`. Still a monoid
   (pointwise combine). SET = MAP(id → ⊤); histogram = MAP(bucket → SUM); DFG (#11) = MAP((from,to) → SUM);
   top-k/most-frequent = δ(argmax) over MAP(id → SUM).
4. **Why it helps:** the fold-algebra design already flagged MAP as "the fifth reducer waiting in the wings".
   This review's cross-domain sweep confirms it from three directions independently (process mining, histograms,
   per-writer G-Counters in #3). That convergence is the "appears repeatedly across domains" test passing.
5. **Fits:** fold algebra; value slot is a JSON map payload (the Timeline precedent: payload + mirrored scalar).
6. **Complexity:** S-M.
7. **Risk:** low — bounded-key-cardinality guard needed (a map over unbounded ids is a memory leak; cap + spill
   to "other").
8. **Novelty:** low (that's good).
9. **Verdict: Prototype when the first MAP-shaped question arrives** (most-frequent / per-bucket) — not before.
10. **Falsifier:** none needed; it is a generalization, falsified only by never being demanded.
+ **Removes:** SET as a special case; pre-empts three would-be one-off reducers (histogram, top-k, matrix).

### 11. Directly-follows View — process mining over agent event streams
2. **Source:** process mining (van der Aalst) — directly-follows graphs, the alpha algorithm's substrate.
3. **Mechanism:** from an event log, count transitions: `DFG[(a_type, b_type)] += 1` for consecutive events per
   case (per session / per subject). Deterministic, incremental, additive. The DFG is the empirical state machine
   of a process, mined without a model.
4. **Why it helps:** "agent experience" is on the optimize-for list and currently exists only as failure counters
   (frequencies, no structure). A DFG View answers *sequence* questions deterministically: "what usually follows
   an enrichment timeout", "which action precedes reverts", "does editing X reliably lead to test failures".
   It is `MAP((from,to) → SUM)` — pure reuse of #10 — over events Menhir already ingests (failure_events,
   revisions, session actions).
5. **Fits:** one ViewKind on top of #10; consumes existing telemetry event sources.
6. **Complexity:** M.
7. **Risk:** medium — temporal adjacency ≠ causation; surface it as "observed sequence frequency", never "cause"
   (true causal edges come from #9 when available).
8. **Novelty:** high for the memory-system context.
9. **Verdict: Research further → prototype on telemetry** (failure/revision streams are already flowing; zero new
   perception needed for v0).
10. **Falsifier:** mine the DFG over existing scheduler/failure telemetry; if no transition pair has both
    frequency ≥ N and precision ≥ 70% for predicting its successor, the structure carries no signal — archive.
+ **Removes:** the future "recurring failure pattern detector" as a bespoke feature; it's a fold.

### 12. Provenance semirings — provenance folded alongside the value, per component
2. **Source:** database theory — Green, Karvounarakis & Tannen, "Provenance Semirings" (PODS 2007).
3. **Mechanism:** annotate inputs with elements of a semiring and propagate annotations through operations;
   the same polynomial machinery specializes to why-provenance, counting, trust/confidence, and access control
   by choosing the semiring. Concretely for folds: every reducer also reduces an annotation — SUM carries the
   bag-union of supporting event ids; EXTREME carries the winner's id (and the losers into the superseded chain);
   SET/MAP carry per-member support; min-confidence (Viterbi semiring) carries the weakest link.
4. **Why it helps:** today provenance is node-level (`MENTIONS` on the whole View). For a scalar counter that's
   fine; for SET/MAP/timeline values the question "why does the View contain THIS member" is unanswerable without
   re-derivation. Per-component provenance makes "why do we believe X = 4" a deterministic O(1) read — the
   debugging and belief-evolution story the optimize-for list asks for — and gives `source_confidence` an actual
   semantics (min/× semiring) instead of a decorative float.
5. **Fits:** fold algebra (annotation slot per reducer) + View payloads. Incremental: episode_uuids are the
   degenerate version already.
6. **Complexity:** M.
7. **Risk:** low-medium — payload growth; cap polynomial size (keep top-k support per member).
8. **Novelty:** high.
9. **Verdict: Research further** — adopt the *discipline* now (every reducer states its annotation rule in the
   algebra doc); implement per-component storage with the first SET/MAP kind.
10. **Falsifier:** take 10 "why does memory say X" debugging sessions; if node-level MENTIONS answered all of
    them without re-derivation, per-component provenance is over-engineering — archive.
+ **Removes:** per-kind provenance conventions; one rule ("every reducer folds the annotation semiring too").

---

## E. Consolidation scheduling and the missing layers

### 13. Lineage-closure Views — semi-naive transitive closure over chains
2. **Source:** deductive databases — Datalog semi-naive evaluation; incremental transitive closure.
3. **Mechanism:** recursive relations (reachability) materialized incrementally: new edge Δ joins only against
   existing closure, not recomputed from scratch.
4. **Why it helps:** Menhir's chains — SUPERSEDES (belief lineage), caused_by (#9, root cause), SameAs (#5,
   identity ancestry) — are all walked at query time today (`history()` walks; provenance walks). Materializing
   closures makes "full belief lineage of X", "root cause of failure F", "everything that was ever merged into
   entity E" O(1) reads. Same machinery, three chains: passes the reuse bar.
5. **Fits:** a generic ClosureView(edge_type) kind; incremental on each new edge.
6. **Complexity:** M.
7. **Risk:** low-medium — closure size; chains in Menhir are shallow (supersession depth ~ revision count), so
   quadratic blowup is unlikely; guard with depth caps.
8. **Novelty:** medium.
9. **Verdict: Research further** — build only when a chain-walk shows up in a latency/answer-cost measurement.
10. **Falsifier:** measure real chain depths; if p99 < 5 hops, query-time walks are fine forever — archive.
+ **Removes:** nothing yet (chains are short today) — hence the wait. Candidate absorber for `history()`.

### 14. Knowledge-compilation catalog — query classes as compilation targets
2. **Source:** knowledge compilation (Darwiche & Marquis) — compile offline into a target form chosen by which
   queries become tractable; the "compilation map" relates forms to supported queries.
3. **Mechanism (transferred):** maintain an explicit registry: *query class → sufficient View kind → compilation
   (fold) cost → answer cost achieved*. New View kinds are admitted by pointing at a query class in the registry
   whose answer cost D0 measures as high; the registry is checked by the nightly view-entropy probe.
4. **Why it helps:** it converts "should we build this View?" from taste into arithmetic — the D0 instrument is
   already the measurement half; this is the missing bookkeeping half. It also *bounds growth*: a fold with no
   registry entry is by definition speculative (the anti-accumulation bias, mechanized).
5. **Fits:** `.agent` doc + the view_entropy probe's summary keyed by query class. Not code-heavy.
6. **Complexity:** S.
7. **Risk:** none material.
8. **Novelty:** medium (as governance-by-measurement).
9. **Verdict: Prototype immediately** (a table in the fold-algebra doc + probe wiring).
10. **Falsifier:** if after a quarter the registry never vetoed or prioritized a fold, it's ceremony — drop it.
+ **Removes:** feature-accumulation pressure; every fold must name the query class it compiles.

### 15. Compaction-debt scheduler — MDL-prioritized consolidation
2. **Source:** LSM-tree compaction policy (storage engines) + minimum description length (information theory) +
   — same mechanism found independently in — complementary learning systems (prioritized replay by information
   gain, cognitive science).
3. **Mechanism:** compaction is triggered and prioritized by *debt*: the gap between the cost of the raw form and
   the cost of the compacted form. Menhir version: per (namespace, subject), debt = estimated tokens of unfolded
   gold-relevant events minus tokens of the would-be View (~the D0 FLOOR, which is already computed). The
   consolidation scheduler folds highest-debt subjects first; debt below threshold → don't fold at all.
4. **Why it helps:** answers "what should consolidation work on next" deterministically — currently implicit /
   everything-hourly. Also gives the *don't-build* signal: a subject whose events are already compact
   (debt ≈ 0) should never get a View (anti-accumulation again).
5. **Fits:** maintenance scheduler; consumes the entropy instrument's FLOOR machinery.
6. **Complexity:** M.
7. **Risk:** low — worst case is a bad priority order, never a wrong value.
8. **Novelty:** medium-high (the D0→scheduler closure is the novel part).
9. **Verdict: Research further** (needs the D0 floor computation ported from the bench into menhir; natural
   follow-on to the view_entropy probe just built).
10. **Falsifier:** rank subjects by debt; if the ranking is uncorrelated with where Arm-A-style consolidation
    actually reduced answer cost on the slice, the debt metric is wrong.
+ **Removes:** "consolidate everything on a timer" — replaces schedule-by-clock with schedule-by-debt.

### 16. Derived Views — a dependency-tracked second-order fold layer (build systems)
2. **Source:** build systems ("Build Systems à la Carte", Mokhov/Mitchell/Peyton Jones) — minimal rebuilds via
   dependency tracking, early cutoff via content hashes; salsa/Adapton for demand-driven variants.
3. **Mechanism:** targets declare inputs; a target rebuilds iff an input's *content hash* changed (early cutoff);
   the scheduler walks the dependency graph, not the clock.
4. **Why it helps:** several wanted capabilities are folds over **Views**, not events: ProjectHealth
   (failure counters + revision counters + test counters), Invariant checks ("test count never decreases" —
   a predicate over a counter's history emitting ViolationEvents), DELTA reports, the DFG-over-identity
   composition (#11 keyed through #5). The current architecture has no legal place for these — that is a
   structural gap, not a missing feature (see final section). The transfer is disciplined: derived Views declare
   input view_keys; `view_sig` of inputs gives early cutoff for free (it already exists!); rebuild is
   deterministic δ/ρ over input payloads; provenance = the input Views (which chain to events — additivity is
   preserved transitively).
5. **Fits:** a `DerivedViewKind` whose "events" are input-View versions; scheduler hook on supersession.
6. **Complexity:** M-L (the one redesign-flavored item; keep the graph two levels deep, forbid cycles).
7. **Risk:** medium — this is where feature accumulation would sneak back in; gate every derived kind through the
   #14 registry.
8. **Novelty:** high.
9. **Verdict: Research further** — design note first (per `.agent/maintenance.md`), prototype with exactly one
   derived kind (Invariant or ProjectHealth), resist the general engine.
10. **Falsifier:** build ProjectHealth as a derived View and as a plain read-time query; if the read-time query is
    always cheap enough (few inputs, no recall involved), derived Views are premature — archive the layer.
+ **Removes:** the alternative is worse — each second-order capability hand-implemented as a scheduler task with
  private caching (the "bespoke fold per View" regression the architecture just escaped).

---

## F. Smaller transfers (compressed)

### 17. AS-OF reads (bitemporal time travel) — SQL:2011 / Snodgrass
Supersession chains + `valid_at`/`expired_at` already form a bitemporal table; add one read
(`fetch_as_of(key, world_time, belief_time)`) and belief evolution becomes queryable ("what did we believe about
X last Tuesday") with **zero new storage**. Complexity S, risk low, novelty low. **Prototype immediately** (it is
~a WHERE clause over `history()`). Falsifier: no user/debug session ever asks an as-of question in a quarter.
*Removes:* ad-hoc history spelunking; makes `include_superseded` recall the *worse* tool for belief debugging.

### 18. Upcasters — event sourcing schema evolution
Typed-event schemas will change; upcasters are pure functions old→new applied during replay/fold so folds stay
total over history and events are never migrated. Complexity S, risk low, novelty low. **Prototype with the first
schema change, not before.** Falsifier: none needed; it is insurance. *Removes:* event-log migrations forever.

### 19. Working-set View — OS working sets (Denning)
`WINDOW ∘ SET` over access events per session/agent = "currently hot subjects"; feeds `build_context` bootstrap
instead of flat recent+flagged. Complexity S, risk low (it's additive; bootstrap already exists), novelty low.
**Research further** — only worth it if bootstrap quality is a measured pain. Falsifier: A/B the bootstrap with
working-set vs recent+flagged on session-start recall relevance. *Removes:* nothing; pure addition — hold it to
that standard.

### 20. Novelty gating at perception — predictive coding (neuroscience)
Only prediction errors propagate. Menhir already implements this at the fold (`view_sig` no-op refresh!) —
the transfer is moving the check *earlier*: a cheap deterministic pre-check (does this episode's extracted-candidate
region even contain numbers/keywords for keyed subjects?) to skip LLM perception on obviously redundant content.
Pure cost optimization. Complexity M, risk medium (skipping true novelty), novelty medium.
**Archive** until perception cost is a measured problem. Falsifier: fraction of perception calls returning
already-known signatures; if <30%, no savings worth the risk. *Removes:* redundant LLM calls (cost, not complexity).

---

## Final section

### 1. Top 10 highest-leverage (ranked)

1. **#5 Identity View (union-find keying layer)** — keys gate every fold's correctness; makes keying versioned,
   auditable, replayable. The one idea that improves *all* existing Views at once.
2. **#1 Kappa replay (perceiver versioning + refold)** — deletes the migration/repair problem class; makes every
   other idea on this list safely retractable (bad extractor → drop events, refold).
3. **#7 Span-grounded verification** — the deterministic precision gate Arm B already demonstrated the need for.
4. **#2 Watermarks / valid_at discipline** — a known, named correctness hole; settled engineering; small.
5. **#4 Tombstones / `Ceased`** — the smallest missing verb in the belief lifecycle; unlocks endings everywhere.
6. **#16 Derived Views layer** — the structural gap (see below); highest ceiling, needs the most discipline.
7. **#15 Compaction-debt scheduler** — closes the D0 loop: measurement → prioritized consolidation.
8. **#14 Knowledge-compilation registry** — the anti-accumulation mechanism, mechanized; nearly free.
9. **#10 MAP reducer** — one generalization that pre-empts three one-offs; build on first demand.
10. **#12 Provenance semirings** — adopt the discipline now, the storage with the first SET/MAP kind.

### 2. Top 5 probably-wrong-but-worth-testing

1. **#11 DFG on telemetry** — adjacency may be pure noise at Menhir's event density; the falsifier is cheap and
   the payoff (agent experience with structure) is large.
2. **#19 Working-set bootstrap** — plausible OS transfer; may lose to the dumb recent+flagged baseline.
3. **#8 N-version consensus** — likely dominated by span verification at a fraction of the cost; test once, keep
   only for structure-poisoning extractors if it wins.
4. **Interval Views (abstract interpretation)** — represent perception uncertainty as deterministic bounds
   (`pages ∈ [200, 240]` from hedged statements); probably violates "answer cost" more than it helps, but it is
   the only honest representation of hedged assertions; smallest test: count hedged quantitative statements in
   real transcripts.
5. **#20 Novelty gating** — the saving is real only at scale Menhir may never reach; cheap to measure, likely archive.

### 3. Recurring patterns (what the sweep kept finding)

- **Everything wants to be a lattice.** LWW, union-find, G-Set, watermark merge, CRDT joins — the reconcile layer
  keeps converging on "merge = semilattice join". The fold algebra should say this out loud: reducers are
  commutative monoids; *reconciles* are join-semilattices; the idempotence line between them is Law-2.
- **The log keeps winning.** Replay (#1), upcasters (#18), tombstones-as-records (#4), identity-as-assertions (#5):
  every robust mechanism stores the *decision* as an event and treats derived state as disposable. Menhir's "raw
  events are ground truth" premise is validated from five unrelated domains — lean into it harder (Views are
  caches; say so).
- **Second-order folds keep knocking.** Invariants, project health, DFG-over-identity, delta reports, closure
  views — a third of all candidate ideas are folds over Views. The architecture keeps them out, and they keep
  coming back dressed as scheduler tasks.
- **Provenance wants to be algebraic, not decorative.** Semirings (#12), span grounding (#7), causal parents (#9),
  identity ancestry (#5) are all "annotations that flow through operations" — one mechanism, currently four ad-hoc shapes.
- **Deterministic verification of probabilistic extraction** (#7, #8, D0's precision guard) is a general pattern:
  the LLM proposes, a checker disposes. Menhir has it in one place (D0 guard); it belongs at the perception exit.

### 4. Is Event → Fold/Reconcile → View missing a fundamental abstraction?

Two candidates, one likely real:

- **Likely real: the derivation layer (Views over Views).** The current pipeline is exactly one fold deep. The
  evidence that this is a *structural* gap rather than a missing feature: (a) multiple independent wanted
  capabilities (invariants, health, sequences-over-identity, deltas) have no legal home; (b) the workarounds all
  reinvent private caching inside scheduler tasks — the "bespoke fold" regression; (c) build-system theory says
  the required machinery (content-hash early cutoff) is *already present* as `view_sig`. The abstraction is not a
  new node type — it is admitting that "event" in `Fold(events) → View` should be "any versioned, provenance-
  bearing input", which View versions already are. One sentence changes the architecture: **a View version is an
  event.** (Supersession already emits exactly the delta a downstream fold needs.)
- **Probably not missing, but under-specified: the keying layer.** Identity/keying is currently *inside*
  perception — invisible, unversioned, unreplayable. It doesn't need a new box in the diagram; it needs to be
  pulled out of the LLM stage into a deterministic, assertion-driven stage (#5). Same layers, one responsibility
  relocated across the probabilistic/deterministic boundary — in the direction the architecture's own rules demand.

### 5. Critique of the current architecture (nothing sacred)

1. **Perception is a god-stage.** Typing, valuing, keying, subject naming, and surface phrasing all happen inside
   the one LLM stage. The deterministic layers can only ever be as correct as the keys they receive, yet key
   decisions are the least observable part of the system. Split extraction (probabilistic) from canonicalization
   (deterministic, versioned, replayable). This is the single largest violation of the system's own stated boundary.
2. **Views compete with their own sources at recall.** Views are stamped `:Entity` into the same pool as the
   episodes they summarize. Additivity is a write-side virtue but a read-side liability: a query can retrieve the
   View *and* its source episodes, paying the token cost D0 exists to eliminate. The architecture says "never
   replacements", but recall needs a subsumption rule (a View shadows its MENTIONS-support below rank k, or the
   D0 footprint counts double evidence). Right now the win measured at rank-1 partially leaks back at ranks 2–5.
3. **Supersession conflates value-change with belief-change.** Fact edges are bitemporal; View versions are not —
   `expired_at` means both "the world changed" (sold a bike) and "we were wrong" (misperceived). These are
   different for debugging and for the belief gate. Tombstones (#4) + a `superseded_reason` enum would split them
   at near-zero cost.
4. **No completeness semantics.** A consumer cannot distinguish "counter = 3, final" from "counter = 3 so far,
   two episodes still in the enrichment queue". Watermarks (#2) — or even `complete_until = last folded event
   time` — are the missing honesty bit. The pending-episode wait machinery in recall exists precisely because
   this bit is missing elsewhere.
5. **The Timeline payload is a growth bomb.** `view_payload` holding all entries re-serialized per supersession is
   O(n²) write amplification over a subject's life and will not survive a busy code-project subject. LSM thinking
   (#15) applies *inside* the kind: windowed/leveled timeline segments with a small hot head. Known-shape problem,
   solved in every storage engine; fix before the first big namespace, not after.
6. **Single-writer is assumed, nowhere enforced.** Scheduler bridges, consolidation detectors, and future agent
   sessions can all reach `record_*`. Until #3 (or a lease) exists, this is a latent lost-update bug with no test.
7. **Fold admission is anecdotal.** Kinds get built when a session feels the need. The D0 instrument exists;
   without the registry (#14) and debt metric (#15), the system's growth is still taste-driven — the exact
   failure mode ("feature accumulation") this review was asked to bias against.
8. **What's right and should be defended:** the one-View-shape/SSOT discipline, stamps-like-ingest, additive
   projections over an immutable log, and the refusal to let probability into folds all survived contact with
   six other fields — every domain surveyed independently converged on some version of these. The architecture's
   core is not missing; it is *under-formalized at its seams* (keys, time, completeness, provenance algebra), and
   every high-ranked idea above is a seam formalization, not a feature.
