# Ingest-time identity under uncertain reference

**A design reference for a class of problem, not a feature.** The third leg of the triptych —
`memory-aggregation-under-uncertainty.md` (write-time derivation) and
`memory-retrieval-under-uncertainty.md` (read-time selection) both stand on what this layer
produces. It describes what happens when prose episodes must become durable graph state: raw
capture, LLM extraction, **reference resolution** (does this mention denote an existing node or a
new one?), trust stamping, and crash-safe processing. The mechanisms live in
`services/ingest_service.py`, `services/enrichment_steps.py`, `services/correlation_service.py`,
and the episode-lifecycle repositories; this is the *why* and the *shape*.

---

## 1. The problem

An episode arrives as prose. The pipeline must (a) capture it durably before anything fallible
runs, (b) extract typed entities and edges with a model, (c) decide for every extracted mention
whether it *is* an existing entity re-narrated or a new one, (d) stamp everything with the trust
metadata recall depends on, and (e) survive crashes, timeouts, and rate limits without losing or
double-processing work. Three things make it hard, and they compound:

1. **Reference is ambiguous.** People re-mention the same entity in different words and mention
   *different* entities in nearly identical words. No lexical or embedding signal separates the
   two cases reliably at the tails.
2. **Extraction is probabilistic and expensive.** The model can return nothing, garbage, or a
   timeout; every retry costs budget; the pipeline must distinguish "nothing to extract" from
   "extraction broke".
3. **Everything downstream trusts this layer blindly.** An unstamped node is silently invisible
   to recall; a wrongly-merged entity poisons perception's events and retrieval's candidates with
   no signature that anything is wrong.

## 2. The governing asymmetry: irreversible beats repairable

The identity decision has two failure directions with fundamentally different costs:

> **A false merge is irreversible** — two entities' provenance, edges, and content mix, and no
> later process can un-mix them. **A duplicate is repairable** — it fragments adjacency and
> prominence across copies (a real retrieval cost), but the conflict/merge machinery can heal it
> at any later time with better evidence.

Hence the standing policy: *err toward creating new nodes and cleaning up later.* This is the
ingest instance of a rule that generalizes across all three layers:

> **Reversibility must be monotone in corroboration.** The more irreversible an action, the more
> independent the evidence it requires. A repairable action may run on a cheap scalar; an
> irreversible one needs a judge, a stated confirmation, or provenance sufficient to undo it.

The corollary for identity: the escalation ladder (§5) must put its *strongest* gate on its most
destructive rung — high similarity alone is exactly the "confident bias" shape (two distinct
things with near-identical names embed nearly identically), so the near-duplicate band is where
the judge belongs most, not least.

## 3. The durability spine: capture first, derive forever after

The raw episode anchor is written **before any model runs**, and everything after it is a
re-runnable derivation:

- **At-least-once with idempotent reconcile.** Work is claimed by atomic lease (worker id +
  expiry + heartbeat); a crash between Graphiti completion and finalization is healed by
  *reconcile-existing* (find the completed artifact from the prior attempt, stamp, mark ready)
  instead of re-extracting; orphaned and stale leases are swept back to pending; exhausted
  retries are marked failed rather than looping.
- **Ownership fencing.** Every finalizing write re-checks that this worker still holds the lease,
  so a recovered episode never gets two writers.
- **Budget as backpressure, not failure.** A session that exceeds its rolling LLM window is
  *requeued with a retry-after*, not failed — cost control must not destroy work.

The obligation this spine exists to meet: **the raw episode is the system's ground truth and the
other two layers' fallback.** The write side abstains freely *because* raw episodes always
answer; the read side treats the store's raw layer as the floor below every gate. Any path by
which a raw episode's content becomes unreachable — terminal failure, preflight rejection,
zero-extraction — is a hole in a guarantee two other architectures assume is absolute (§6, and
the standing gap list).

## 4. A taxonomy of ingest failure modes

### 4a. Identity collapse (false merge)
Two entities become one. Irreversible; corrupts both provenance chains; invisible afterward (the
merged node looks like any node). The tail risk is *highest* exactly where the similarity score
is highest — near-identical names for distinct things — so scalar confidence is anti-correlated
with safety on this rung (the write-side §4a argument, applied to reference).

### 4b. Identity scatter (fragmentation)
One entity as many nodes. Repairable, but costly while it lasts, and the cost lands in the
*other* layers: adjacency and prominence dilute across the copies, so retrieval ranks the entity
below its true standing, and perception's coreference sees more candidates than exist. Duplicates
are a retrieval-quality tax, which is why "clean up later" must actually happen (conflict band →
review → merge with evidence).

### 4c. Substrate loss
The raw-episode fallback guarantee breaks: content captured at queue time never becomes
recallable because enrichment terminally failed (oversize rejection, zero extraction, exhausted
retries). Each such episode is a hole in the floor the other layers stand on. Terminal failures
need a recallability story, not just telemetry.

### 4d. Unstamped writes
A node missing namespace, scope, or embedding is not wrong — it is **silently absent** from
recall, the worst failure shape (bitten twice in this workspace). Guard: one stamping choke point
through which every extracted artifact passes; any write path that bypasses it (raw Cypher,
ad-hoc scripts) re-opens the hole.

### 4e. Double-processing and lost work
Crashes between expensive completion and cheap finalization. Guarded by the §3 spine (lease +
reconcile + fencing); the residual is Graphiti-side state the reconcile can't see.

### 4f. Path divergence
Multiple ingest entry points that enrich unequally: an episode's quality depends on which door it
entered. Divergent paths also rot — the less-traveled one accumulates dead code that its
blanket exception handlers hide. One pipeline, or an explicit contract for what the lesser path
skips.

## 5. The escalation ladder: identity actions by evidence band

The correlation pass sorts each new entity against its nearest existing neighbors into bands, and
the **action escalates with the band**:

```
sim < 0.70          novel        → store normally (no action)
0.70 – 0.85         related      → RELATES_TO edge (reversible annotation)
0.85 – 0.95         ambiguous    → flag for LLM review (the conflict path — a judge)
> 0.95              near-dup     → merge into survivor
```

The *shape* is right — it is a claim-strength lattice for identity, escalating commitment with
evidence — and the middle band correctly hands ambiguity to a judge. The §2 rule, however, binds
the top rung: **merge is the irreversible action, so it needs corroboration beyond the scalar
that proposed it** — a judge confirmation (determinism proposes, the model judges, confidence
gates), a shared-provenance check, or at minimum an audit trail sufficient to unmerge. A
threshold alone is a scored gate on the one action that can never be taken back.

Concurrency is part of identity correctness: same-namespace episodes are **serialized** through
the ingest gate precisely so entity resolution never races itself; parallelism is spent across
namespaces, where identities cannot collide.

## 6. Terminal failure needs a recallability story

"Failed" conflates two different endings that deserve different treatment:

- **Nothing to extract** (a chatty episode with no memorable content) is a *successful*
  determination, not a failure. Marking it failed inflates failure telemetry and — worse — can
  strand the raw content outside recall.
- **Extraction broke** (parse errors, timeouts, oversize) is a real failure, but the episode's
  raw content was durably captured at queue time and should remain reachable: the write-side
  fallback argument does not care whether enrichment succeeded, only that the prose survives
  where recall can find it.

The principle: **capture is the commitment; enrichment is best-effort.** A terminal enrichment
outcome may skip entities, edges, and Views — it must never orphan the episode text itself.

## 7. Summary — the principles, portable

1. **Irreversible beats repairable.** False merges cannot be undone; duplicates can. Err toward
   new nodes; make "clean up later" a real, evidenced process.
2. **Reversibility monotone in corroboration.** The more destructive the action, the more
   independent the evidence — the merge rung needs a judge, not just the highest scalar; high
   similarity is anti-correlated with safety exactly at the tail that matters.
3. **Capture first; everything after is a re-runnable derivation.** Durable raw episode before
   any model call; at-least-once processing with idempotent reconcile and ownership fencing.
4. **The raw substrate is two other layers' load-bearing fallback.** No terminal outcome may
   orphan episode content from recall; "nothing to extract" is success, not failure.
5. **Unstamped is invisible.** Trust metadata flows through one choke point; every bypass
   re-opens the silent-absence hole.
6. **Escalate identity actions by evidence band** — novel → relate → flag → merge — and spend
   concurrency only where identities cannot collide (serialize per namespace).
7. **Budget is backpressure, never failure.** Requeue with retry-after; cost control must not
   destroy work.
8. **One pipeline.** Divergent entry points enrich unequally and rot silently; if a lesser path
   must exist, its skipped steps are a documented contract, not an accident.
