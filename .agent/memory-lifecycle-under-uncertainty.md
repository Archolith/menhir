# Forgetting under uncertain value

**A design reference for a class of problem, not a feature.** The fourth leg:
`memory-ingest-under-uncertainty.md` builds the substrate, `memory-aggregation-under-uncertainty.md`
derives from it, `memory-retrieval-under-uncertainty.md` selects from it — this layer decides what
**stops existing**. It describes what happens when a memory system must keep, compress, or destroy
memories whose future value is unknowable at decision time. The mechanisms live in
`services/lifecycle_service.py`, `domain/memory_types.py`, `infrastructure/consolidation_queries.py`,
and the conflict tooling; this is the *why* and the *shape*.

---

## 1. The problem

Storage is cheap; an unbounded store is not free — it dilutes retrieval with noise and grows the
candidate pool every gate must sift. So the system must forget. But the value of a memory is a
property of *future* queries, which do not exist yet. Three things make it hard, and they compound:

1. **Value is unobservable.** Every available signal — access recency, connectedness, uniqueness,
   type — is a proxy, and each proxy has a failure mode where it points confidently the wrong way.
2. **Destruction is the only irreversible act in the system.** A wrong View abstains, a wrong
   ranking is transient, even a wrong merge could (with trails) be undone — a deleted node's
   content is gone, and the other three layers all assume the substrate under them is durable.
3. **The strongest value signal is produced by another layer.** Retrieval touches
   `last_accessed` on everything it returns, and staleness is the primary decay input — so
   **what recall returns survives, and what recall misses dies**. The archive is curated by the
   ranker, feedback loop included.

## 2. The governing asymmetry: destruction is irreversible; retention is repairable

> **A wrongly kept memory costs noise and storage — repairable at any later sweep. A wrongly
> destroyed memory is unrecoverable, and its absence is silent.**

This is the ingest layer's merge asymmetry (irreversible ≫ repairable) with even less feedback:
a bad merge at least leaves a corrupted node someone might notice; a bad delete leaves nothing.
The rule that follows is the same one, applied to the destructive ladder:

> **Reversibility must be monotone in corroboration.** Each rung of the forgetting ladder —
> compress → delete — destroys more, so each rung must demand more evidence, a longer idle
> window, and a wider recovery path than the one before. A scalar threshold may *nominate* a
> node for destruction; it must never be the last thing that touches it.

## 3. The forgetting ladder and its value proxies

The implemented ladder: `ACTIVE → COMPRESSED → GONE` for PERSISTENT nodes (bridged deletion keeps
the graph connected), and `promote-or-delete` for SESSION nodes at consolidation. Transitions are
gated by a conjunction of proxies, each with a named failure mode:

| proxy | meaning | fails when |
|---|---|---|
| `last_accessed` staleness | nobody needed it | retrieval never surfaced it (the §1 loop) — unreturned ≠ worthless |
| `edge_count` | structurally load-bearing | value is intrinsic, not relational (a fact nothing links to yet) |
| `sharpness` (uniqueness) | redundant with neighbors | **the similarity scale is wrong** (§5) or neighbors are related-not-redundant |
| `user_flagged` | human said keep | humans rarely flag prospectively |
| type policy | category-level contract | type was misassigned at extraction |

Three structural protections do the real safety work: **IDENTITY is decay-exempt**, **flagged
nodes are untouchable**, and **rehydration count ≥ 3 exempts a node from further compression**
(anti-thrash: a memory that keeps coming back has proven the proxies wrong about it).

## 4. Compression must keep its receipt

Compression is lossy by intent — that is its function. The invariant that keeps it *safe* is:

> **The pre-compression content must remain reachable until the compression has proven harmless,
> and rehydration must consult it.** A rehydration that reconstructs from the summary alone
> ratchets detail away — each compress/rehydrate cycle is a generation of photocopy loss, exactly
> the "repeated summary-only revisions" hazard the policy names.

The revision sidecar already archives `old_value` on every compression; the fallback story is
complete only when rehydration *reads* that archive instead of LLM-merging the summary with new
context. Summary-merge is the enhancement path, not the recovery path.

## 5. Signals must be scale-lawful — the cross-layer law, third application

The retrieval doc's scale-coupling law ("never mix signals across a scale boundary without a
contract") binds hardest here, because lifecycle consumes similarity through the same search the
ranker uses, and the consequence is not a mis-rank — it is a deletion or a merge. A similarity
threshold calibrated for cosine `[0,1]` applied to a rank-fusion score (top hits ≈ 1.0–2.0
*regardless of absolute similarity*) makes "uniqueness" a rank artifact: every node has top-ranked
neighbors, so nothing is unique, so the uniqueness gate swings wherever the rank math puts it —
over-deleting where the threshold is high, never firing where it is low. **Any lifecycle decision
fed by a search score must pin the score's scale with a test, or compute true cosine itself.**

## 6. Conflict handling: the layer's best pattern

The contradiction path is abstention-shaped end to end and worth naming as the template:
similarity *nominates* (0.85–0.95 band), an **LLM judge confirms** before anything surfaces,
false positives are suppressed with a cooldown so they are not re-litigated, and staleness
resolves to **keep_both** — the do-no-harm default. Nothing in the conflict path destroys.
The asymmetry is honored: two contradictory memories held side by side cost a little confusion;
a wrongly discarded side costs the truth. The destructive paths (§2) should look like this one.

## 7. Summary — the principles, portable

1. **Destruction is the system's only irreversible act; its absence is silent.** Wrongly kept is
   repairable; wrongly destroyed is not. Tune every destructive gate to that asymmetry.
2. **Reversibility monotone in corroboration, rung by rung.** Scalars nominate; corroboration
   (a judge, a recovery window, a human) executes. No hard delete on a threshold alone.
3. **Every value signal is a proxy; conjoin them and keep the overrides structural**
   (type exemption, flags, rehydration-count) — a proxy conjunction can be argued with; a
   structural exemption cannot be out-tuned.
4. **Compression keeps its receipt.** Archive before you shrink; recover from the archive, not
   from the summary; treat repeated rehydration as the proxies being wrong.
5. **Scale-lawfulness is a lifecycle invariant, not a ranking nicety.** A wrong-scale similarity
   mis-ranks a query once — and mis-deletes a memory forever.
6. **The ranker curates the archive.** Access-based staleness inherits every retrieval bias;
   pair access signals with world-time and structural signals so recall's blind spots don't
   become the archive's holes.
7. **The conflict path is the template:** nominate by score, confirm by judge, suppress with
   cooldown, default to keep-both. Forgetting should be at least as careful as disagreeing.
