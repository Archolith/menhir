# Plan v6 (FINAL): menhir — Content-Vector Lane, via the existing R1 benchmark

**Status:** **CLOSED — NO-GRADUATE (2026-07-14).** P0/P1/P2 completed; P3 is
intentionally not authorized. The final run passed production/Arm-A parity and the no-write
fingerprint. Arm B improved generated paraphrases but tied every fixed human-anchor primary,
so the §6.4 + Appendix H.3 decision rule closes the plan. Evidence:
`archolith-bench/results/content-vector-final-guarded/result.json`.
**Author:** Claude Code session 2026-07-14
**Reviewers:** Codex × 6. All points accepted; all code claims independently verified.

> **The body (§1–§13) is NORMATIVE. The appendix is HISTORY ONLY.** If they conflict, the body is
> correct and the appendix is stale.

---

## 0. Revision history — what each version got wrong

| ver | claim | outcome |
|---|---|---|
| v1 | "Dramatic win: 8/10 golds improved." | **WITHDRAWN.** Strawman baseline — production also runs BM25 over `node_name_and_summary`, which alone gets 8/10 recall@5. |
| v1 | "All probes read-only against prod." | **FALSE.** `prepare_memory_runtime()` → `sync_edge_counts()` → `SET n.edge_count` on every Entity. |
| v2 | "Build a new recall-eval harness." | **WITHDRAWN.** It already exists (`archolith_bench/r1/`). |
| v2 | "Benchmark the lane, then build it." | **CIRCULAR.** Arms B/C require the lane to exist. |
| v3 | "Reuse `evaluate_win_gate` — saturation free." | **FALSE.** Hardcodes R1's metrics; demands *all* unsaturated metrics improve. |
| v3 | "Adjacency is unaffected by the clone." | **FALSE.** Zero relationships ⇒ `adjacency_bonus = 0` for every candidate. |
| v4 | Gate: *all* eligible primary metrics must improve. | **FALSE-NEGATIVE BUG.** `recall@5`/`recall@10` are nested. Fixed in §6.3. |
| **v5** | **Eval = 10 hand-curated gold UUIDs + R7 rot-repair machinery.** | **REPLACED (§2).** Over-engineered and carries curator bias ("the queries are mine"). The real requirement is a *stable, reproducible number*, not a curated set. **Auto-generate known-item queries from the clone**: bigger N, no UUID rot (regenerated each run), samples the true corpus distribution, and naturally produces the paraphrase queries that ARE the hypothesis. R7 rot machinery is **deleted** (nothing keyed on prod UUIDs survives between runs). |

**Self-corrected:** the v1/v2 BM25-vs-cosine comparison ran BM25 on **prod** (~52k Entity nodes) and cosine on the **clone** (7,402). Not comparable. Redo on one population (§3).

---

## 1. Proposal

Add a **content-vector retrieval lane** to menhir recall, to rescue **paraphrase queries** the lexical (BM25) lane cannot reach by construction. **Not** a BM25 replacement; **not** an across-the-board win; **not** approved for production until §6 says so.

### The one durable finding

| query | memory | BM25 | content-cos |
|---|---|---|---|
| "how is the **Google Analytics** tracking id configured?" | `PUBLIC_GA_ID environment variable` ("**GA**") | **#67** | **#2** |
| "which setting controls **how often** the decay lifecycle runs?" | `config fields lifecycle_decay_interval_s` ("**interval**") | **#29** | **#2** |

BM25 matches shared tokens; it cannot bridge a synonym. It symmetrically **wins** on exact-token queries. **The lanes are complementary.** That is the entire case.

---

## 2. NORMATIVE: evaluation set — auto-generate from the clone (revised v6)

The gate (§6) needs **a stable, reproducible number** (recall@k, MRR), not a hand-curated corpus. Curated golds are one way to get it and the way I inherited from throwaway probes; they are worse on every axis that matters. **A spot-check against the clone is not sufficient either** — eyeballing results yields vibes, not a number, and cannot feed `evaluate_win_gate`. The answer is a *generated, reproducible* known-item eval.

### 2.1 The workhorse: auto-generated known-item retrieval

Procedure, per run, against the frozen clone (§3):

1. **Sample** `N` semantic `:Entity` nodes that have real text (`summary` or `content` non-empty). Deterministic: seed the RNG and record the seed in the manifest (§8). Stratify the sample (e.g. across `namespace`, presence/absence of `summary`) so it mirrors the corpus, not the head.
2. **Generate a PARAPHRASED query** whose answer is that memory — one LLM call per node, instructed to **use different words than the memory's own text** (ask about the fact, do not quote it). Paraphrase is the point: a query that reuses the memory's tokens tests BM25's strength, not the content lane's.
3. **Known-item metric:** run the query through `RecallService.recall()`; the sampled node is the gold. Record its final rank.

No prod UUIDs persist between runs, so **there is no anchor rot and no R7 repair machinery** — the whole class of problem v5 built scaffolding for is deleted by regenerating the key each run.

**This removes the curator (me) from the loop.** The v5 weakness "the queries are mine" is resolved: queries are sampled from the real distribution and generated mechanically.

### 2.2 Duplicate handling (REQUIRED — the clone is a dupe of prod)

The clone is a faithful duplicate of prod, and **prod contains near-duplicate memories** (repeated facts, re-ingested content). So "did the sampled node come back" is the wrong question when a semantically-identical sibling exists: retrieving the sibling is a correct answer, not a miss.

**Rule:** before scoring, compute a **lane-neutral lexical duplicate cluster** for each sampled gold — normalized recall-text exact hash or high token Jaccard (for example ≥ 0.9, recorded in the manifest). Do **not** cluster by `content_embedding`, `name_embedding`, or BM25 score: using any lane's ranking signal to define what counts as a hit privileges that lane. A hit credits retrieval of **any** lexical-cluster member at the gold's rank position. Report the lexical cluster-size distribution and, separately, content-cosine neighborhood sizes; a large gap is evidence of semantic redundancy rather than literal deduplication.

### 2.3 Negatives — small held-out curated set (cannot be auto-generated)

A "query with no answer" cannot be generated *from* a memory (generating-from-a-memory always has an answer). Negatives stay a small curated list (~10–20 off-topic topics) in `archolith-bench/corpora/menhir_recall_negatives.json`, each with a keyword asserted absent AND a recorded max-cosine bound (keyword absence ≠ semantic absence — §7). This is the only hand-authored artifact that remains.

### 2.4 A tiny curated ANCHOR set — regression tripwire, not the corpus

Keep the specific known paraphrase cases (GA → `PUBLIC_GA_ID`, "how often" → `lifecycle_decay_interval_s`) as a handful of **fixed** known-item cases in `corpora/menhir_recall_anchors.json`, purely so a regression in the exact phenomenon this plan targets is caught deterministically. It is **not** the statistical corpus and its N is not a sample size. These *do* key on prod UUIDs, so they get a minimal existence check (fail loudly if the anchor memory is gone), but no rot-repair machinery — an anchor that dies is retired by hand, deliberately.

### 2.5 Location

`archolith-bench/corpora/` (version-controlled). `fixtures/` is barred by bench policy (`.agent/README.md:64`: fixtures are *"NOT a headline number"*). The generated query set is written to `results/` per run (it is regenerable, not a durable spec); only the **negatives**, the **anchors**, and the **generation config** (seed, model, N, stratification, dup threshold) are durable in `corpora/`.

---

## 3. NORMATIVE: clone construction

The clone must reproduce production **scoring**, not merely contain the right nodes.

| scoring input | mechanism | requirement |
|---|---|---|
| `adjacency_bonus` | `fetch_adjacency_pairs`: `MATCH (a)-[r]-(b)`, **untyped**, both endpoints must be candidate uuids (always `:Entity`) | **Clone EVERY Entity↔Entity relationship type** (`RELATES_TO`, `DEFINES`, `CONTAINS`, `CALLS`, `IMPORTS`, `ANCHORED_TO`, …) with properties. Zero relationships ⇒ `adjacency_bonus = 0` for **every** candidate. |
| `prominence_bonus` | `scoring_service.py:142`: `gamma * log(1+edge_count)/max_log_edge` | `edge_count` counts **every incident relationship except `ANCHORED_TO`** — including `MENTIONS` from `Episodic` (15,461 on prod, vs `RELATES_TO` 17,108) and `CONCERNS` from `Todo`. |

**The rule:**
1. Clone the **full `:Entity` population** — **not** only the ~7,400 with `name_embedding`. Production's BM25 index covers every `:Entity`; a narrower clone hands BM25 an unrealistically small competitor set.
2. Clone **every Entity↔Entity relationship**, all types, with properties.
3. **Omit** non-Entity incident topology (`Episodic`–`MENTIONS`, `Todo`–`CONCERNS`), and compensate by —
4. **Copy `edge_count` verbatim from prod; NEVER recompute it.**
   - **Never call `prepare_memory_runtime()` on the clone** — it invokes `sync_edge_counts()` unconditionally (`bootstrap.py:276`), which would recompute `edge_count` from the partial topology and **flatten prominence**.
   - Create graphiti indices **directly via Cypher** (`node_name_and_summary` fulltext; vector indices for `name_embedding` and `content_embedding`).
   - `build_memory_services()` alone does not sync — `RecallService` constructs safely.
5. **Assert per-node `edge_count` parity vs prod before any arm runs.** Drift **voids** the run. (Enforced by the fingerprint of §4.)
6. **Freeze the clone.** All arms, the parity test, and the auto-gen sampling run against the **same frozen clone**.

---

## 4. NORMATIVE: the benchmark must not mutate the graph (P0 BUG — DONE)

`RecallService.recall()` defaults to **`update_access=True`** (`recall_service.py:843`) — touches `last_accessed`, reinforces edge weights, schedules rehydration.

**Two call sites omitted it; both fixed (commit `8ea3127`):** `r1/retriever.py:132` and `scripts/run_r1_dummy.py:130`. Any pre-existing multi-condition R1 result is order-contaminated and should be re-run.

**Enforced (commit `a7c6cc4`):** `r1/graph_fingerprint.py` hashes every write-sensitive surface (`last_accessed`, `edge_weight`, `freshness`, `rehydration_count`, `edge_count`) as sorted `uuid|value` pairs; `assert_no_writes(before, after)` wraps the condition loop and fails loudly on drift. A hash, not a sum — a sum is blind to offsetting changes.

---

## 5. NORMATIVE: attribution ≠ admission

`scoring_service.py:107` gates the floor on the **singular** field:

```python
candidate.source in FLOOR_EXEMPT_SOURCES   # BM25, PENDING, FILE_LINKED, FACET, STRUCTURE, FACT_EDGE
```

If "has BM25 among its sources" conferred floor exemption, candidates would become exempt that are **not exempt in production** — **Arm A would diverge and fail parity (§6.1)**, silently invalidating every arm.

| field | semantics | affects ranking? |
|---|---|---|
| `admission_source` | **one** value, assigned by **today's production policy**, unchanged | **YES** — floor exemption, priors |
| `contributing_sources` | **set** of every lane that produced the candidate | **NO** — trace/provenance only. **Never read by `ScoringService`.** |

---

## 6. NORMATIVE: scale contract + decision gate

### 6.1 Scale contract + parity test (blocks §7)

A candidate ranked #1 in *n* lanes accrues *n* reciprocal-rank contributions — **Arm B could win purely by having one more lane to score in.** Concrete: `scoring_service.py` pins `GRAPHITI_RRF_DUAL_METHOD_MAX = 2.0` (literally *"DUAL"*) and `MIN_SIMILARITY_THRESHOLD = 0.15`. A third lane raises the ceiling to ~3.0, **silently rescaling the floor and every `[0,1]` `SOURCE_PRIORS` value relative to it.**

**Required before any arm runs:** (1) exact three-lane RRF formula + per-lane weights; (2) **common normalization applied identically across A/B/C** (constant ceiling); (3) multi-source attribution per §5; (4) **PARITY TEST: Arm A must reproduce production ranking exactly — both paths on the SAME FROZEN CLONE.** Parity tests the *fusion code*, so the data must be identical on both sides. Failure ⇒ **run VOID.**

### 6.2 Metric definitions

- **`recall@k`** — fraction of eval queries whose gold (or any duplicate-cluster member, §2.2) is in the top-k of **`RecallService.recall()` final results**. Not raw lane rank.
- **gold rank** — 1-based rank in the final results; **absent ⇒ `limit + 1`** (total, monotone).
- **MRR** — mean `1/rank(gold)`, using the `limit + 1` convention.
- **per-query rank regression** — `rank_B > rank_A + rank_tolerance`, where **`rank_tolerance` is a separate INTEGER** (ranks are integers; do not reuse the float `regress_tolerance`).
- **`negative_query_false_positive_rate`** — fraction of negative queries returning ≥1 result above the floor. **NON-REGRESSION guard, not an expected improvement** — an embedding lane alone does not make negatives abstain (separate admission-gate work, §9.6).

### 6.3 The gate — `evaluate_win_gate`, PARAMETERIZED (DONE, commit `4632eb0`)

The gate now takes `baseline_condition`, `challenger_prefix`, `primary_improvement_metrics`, `improvement_mode`, and directional guards. For this plan:

| parameter | value |
|---|---|
| `primary_improvement_metrics` | `recall@5`, `recall@10`, `mrr` |
| `improvement_mode` | **`"any"`** — beat ≥1 eligible primary, regress none. **Required** because `recall@5`/`recall@10` are nested: a gold moving rank 6→2 improves `recall@5`+MRR but not `recall@10`, so `"all"` rejects a real win. |
| `guards_lower_is_better` | `stale_hit_rate`, `wrong_scope_injection_rate`, `negative_query_false_positive_rate`, per-query gold rank (`rank_tolerance`) |
| `guards_higher_is_better` | `exact_string_recall`, `symbol_recall` (must not be traded away) |

A metric the gate is configured to consult but the run did not produce **raises** (not a vacuous `0.0` pass). R1's own ladder keeps `improvement_mode="all"` over its independent exact/symbol primaries — behaviour preserved.

### 6.4 Decision rule — mutually exclusive

| outcome | condition | action |
|---|---|---|
| **GRADUATE** | gate returns true for **B over A** | proceed to §9 |
| **NO-GRADUATE** | no eligible primary improved, or any metric/guard regressed | **CLOSE THIS PLAN** — the lane is redundant with, or worse than, BM25 |
| *(VOID)* | §6.1 parity fails | invalid; fix and re-run |

**`C ≥ B`:** re-run **the same gate** with **B as baseline, C as challenger**. If C graduates over B, prefer C (*replace* the name lane). Evaluated **only if B graduates over A**.

---

## 7. The fusion benchmark (P2)

| arm | lanes | question |
|---|---|---|
| **A (control)** | BM25 + name_vector | **must reproduce production exactly** (§6.1) |
| **B** | BM25 + name_vector + content_vector | does the lane add anything? |
| **C** | BM25 + content_vector | does it *replace* name_vector? |

Run on the frozen clone (§3), `update_access=False` (§4), auto-gen + anchor + negative eval sets (§2), gated per §6.

**Negative-query fidelity:** keyword absence does **not** prove semantic absence. Pair the keyword assertion with a **recorded max-cosine bound**; do not claim absolute absence.

---

## 8. Provenance manifest (required on every run)

Reuse the LME pattern (`results/lme-recall-*/run_manifest.json`). Record: menhir + bench **commit hashes** + dirty state; **graph fingerprint** (node/edge counts, index schema, clone freeze id, `edge_count` parity result); **embedding provider / model / dimensions** (query-side *and* stored-side); **text-builder version** (§9.1); **auto-gen config** (RNG seed, generator model, N, stratification, duplicate-cluster threshold + cluster-size distribution); **candidate depth** (`candidate_k`), `limit`, floor value, per-arm tuning config; **the §6.1 parity result**; **negatives/anchors file hashes**.

**A result without this manifest is not evidence.** With the seed + generator model recorded, an auto-gen run is reproducible despite being generated.

---

## 9. Production hardening (P3 — only if §6.4 GRADUATES)

**9.1 Recall-text builder — PRESELECTED, versioned:** `summary → content → name` (first non-empty). This is the builder every measurement used. **`summary` vs `name + summary` is NOT answerable by arms A/B/C** (they vary lanes, not the text builder) — it needs its own pre-registered builder-variant arms, out of scope for P2.

**9.2 Freshness (R2).** `summary`/`content` are **mutable** (compression, rehydration, merge, unmerge, conflict resolution). An ingest-time embedding goes **silently stale**. Store `recall_embedding`, `recall_embedding_text_hash`, `recall_embedding_model`, `recall_embedding_dimensions`, `recall_embedding_updated_at`. Every mutation path regenerates or invalidates + queues repair. **Embedding failure must not fail the episode's extraction**; recall falls back to the name lane while repair is pending.

**9.3 The lane.** Menhir-owned, namespace-aware vector pass → `CandidateSource.CONTENT_VECTOR`. **Do NOT add `RetrievalScoreKind.COSINE`** — raw cosine is the lane's *input*; the fused candidate keeps **`WEIGHTED_RRF_NORMALIZED`**. Raw cosine lives **only** in lane-level R0 trace metadata. Do **not** repoint graphiti's search — `search_utils.py:764` hardcodes `n.name_embedding`.

**9.4 Provider consistency (R3/R8).** Backfill must use menhir's **resolved** embedding provider/model/dims. Stored and query vectors must match. Also makes a privacy regression structurally impossible (§10).

**9.5 Migration order (R4).** dual-write/invalidate (no read change) → backfill with compare-and-set → verify coverage/dims/hashes → shadow mode → canary behind a **default-off** setting → rollback = flip reads to the name lane, **never delete either vector property**.

**9.6 Admission gate — OUT OF SCOPE, separate plan (R5).** A universal content-cosine gate breaks source contracts (pending/file-linked/fact-edge/exact-identifier BM25). Admission must be **source-aware**. Measured margin +0.0168 (1.7%) at N=10 — not a safe threshold basis regardless.

---

## 10. Privacy — CLOSED, not a blocker

ctharvey (2026-07-14): not a concern. **And not a new exposure:** menhir **already** transmits full episode content to OpenAI at ingest — graphiti's `add_episode` runs LLM entity-extraction over the raw episode text (observed: episode `39f99546`, 11 `chat.completions.create`, `gpt-4.1-nano`). Embedding the summary is a **strict subset** of text already sent. **Reversal condition:** a fully-local menhir would send nothing; §9.4 prevents an accidental external call by construction. *(Note: the §2 auto-gen step also sends memory text to the generator model — same posture, same subset-of-ingest argument.)*

---

## 11. Phasing

| phase | scope | gate |
|---|---|---|
| **P0** ✅ complete | Mutation fixes, no-write fingerprint, parameterized gate, generated/anchor/negative corpora, metrics, manifest, and persistent production-dump clone. | complete |
| **P1** ✅ complete | Default-off experimental lane, multi-source attribution, and fixed-ceiling fusion. | production/Arm-A parity passed |
| **P2** ✅ complete | Frozen-clone A/B/C benchmark with generated, anchor, negative, exact-string, and symbol guards. | **NO-GRADUATE** |
| **P3** ⛔ not run | Production hardening and rollout. | correctly skipped because P2 did not graduate |

---

## 12. Known weaknesses of the current evidence

| weakness | status |
|---|---|
| BM25 measured on prod (~52k), cosine on clone (7,402) | **RESOLVED.** Final benchmark used one persistent offline-dump clone: 55,267 nodes and 104,289 relationships. |
| ~~Golds/queries are hand-curated by me (N=10)~~ | **RESOLVED in v6** — auto-generated from the corpus, curator removed (§2.1). |
| **NEW: LLM-generated queries carry their own bias** | The generator may write in the memory's own register (understating paraphrase difficulty → conservative, biases *against* our hypothesis) or drift off-topic (a bad gold). Mitigate: instruct for paraphrase; sanity-check a sample by hand; the anchor set (§2.4) is the human-verified backstop. |
| Duplicate memories in prod (inherited by the clone) | Handled by cluster-crediting (§2.2); cluster-size distribution is itself reported. |
| Content-less nodes (`summary`+`content` empty, ~11% of the embedded subset) | Excluded from auto-gen sampling (no text to paraphrase); fall back to name lane in recall — no benefit, no regression. |
| Fusion benchmark not run | **RESOLVED.** The gate returned NO-GRADUATE; P3 was not run. |

---

## 13. Measured dead ends — do not re-litigate

| lever | verdict |
|---|---|
| `similarity_scale="normalized"` | **No-op.** Live A/B: identical set + order. Floor membership preserved "by construction" per its own code comment. |
| Recency weighting | **Exonerated.** Top email-query hit has `recency_bonus=0.032` and still ranks #1 on similarity alone. |
| Score-gap / "cliff" cutoff | **Not discriminating.** No-answer and genuine queries both decay smoothly. |
| Cosine gate on `name_embedding` | **Not viable.** Collisions outscore real hits (`qwen_servers` 0.703 > `stale-anchor labeling` 0.681); ranges overlap 0.189. |
| `enable_cross_encoder_rerank` | **DEAD FLAG.** Declared in `RetrievalTuningConfig`, never read anywhere in `src/menhir`. Silent no-op. Implement or delete. |
| Cross-encoder rerank | **Not recommended.** True absolute-relevance signal (0.000 on both no-answer queries; 0.953 on an answerable one) but ~1s + an LLM call per recall, and its *ordering* buried the top hit to rank 9 on the one positive tested. |

---

## Appendix — HISTORY ONLY (NON-NORMATIVE)

**Not executable guidance.** The Codex reviews that produced §1–§13. Where any appendix text conflicts with the body, **the body is correct.**

**#1** — experiment doesn't reproduce production recall (BM25 already searches name+summary; must exercise `RecallService.recall()` e2e); embedding freshness unspecified; graphiti hardcodes `name_embedding`; a universal gate breaks source contracts; backfill/provider consistency incomplete; privacy boundary *(later closed — §10)*; the probes were not read-only.

**#2** — the harness already exists (`r1/`); `fixtures/` is policy-barred; the dependency order is circular; three-lane fusion has no scale contract; the evaluation mutates itself; clone/negative fidelity underspecified; results need a provenance manifest.

**#3** — clone scoring won't match production (`sync_edge_counts` counts Entity–Episode edges); decision rules overlap and saturate; `RetrievalScoreKind.COSINE` is wrong for the fused candidate.

**#4** — body contradicted the appendices; `edge_count` preservation insufficient with zero relationships (clone all Entity↔Entity types); `run_r1_dummy.py` also omits `update_access=False`; `evaluate_win_gate` is not generic; multi-source attribution must not confer floor exemption.

**#5** — the gate still required EVERY unsaturated primary to improve, rejecting a real win on nested `recall@5`/`recall@10`; A/B/C cannot answer `summary` vs `name+summary`; define missing gold rank as `limit+1`; separate integer rank tolerance; parity on the same frozen clone; hash-not-sum fingerprint.

**#6 (this session, ctharvey)** — why a curated gold set rather than spot-checking against the full clone? The real requirement is a reproducible *number*, not curation, and a spot-check yields only vibes. **Auto-generate known-item queries from the clone**: bigger N, no UUID rot, samples the true distribution, removes curator bias, naturally produces paraphrase queries. Keep a small held-out negative set (can't be generated) and a tiny human-verified anchor set (regression tripwire). Clone confirmed to be a duplicate *of* prod, so prod's near-duplicate memories are inherited — credit any duplicate-cluster member as a hit.

**Infrastructure.** prod = `neo4j-memory` @ `bolt://192.168.86.33:7687` (OVM box). test = `menhir-neo4j-test` @ `bolt://192.168.86.33:7688` — ephemeral tmpfs, `neo4j/testpassword`, **no APOC** (clone with plain Cypher). Graphiti fulltext: `node_name_and_summary` → `['name','summary','group_id']`.

**Throwaway probes** in `menhir/scripts/` (`_switch_diff.py`, `_gate_threshold_test.py`, `_content_vs_name_search.py`, `_cosine_gate_probe.py`, `_ce_rerank_probe.py`, `_bm25_baseline_probe.py`, `_clone_and_embed_content.py`) — fold into R1 and delete.

**Shipped 2026-07-14:** `02da0fb` (exclude structural project-scan nodes from semantic recall) · `96e114a` (flag stale `root_path` projects) · `8ea3127` (R1 read-only fix) · `4632eb0` (parameterized win gate) · `a7c6cc4` (write-sensitive fingerprint).

---

## Appendix H — Historical review addendum (absorbed into the normative body)

This appendix records the review that produced the controls now stated in the
normative sections above. If wording here conflicts with the body, the body wins.

> Body-normative like §1–§13. Prompted by ctharvey: "make sure content-embedding won't skew it."
> Three ways content_embedding could bias the benchmark; verified, with required controls.

### H.1 Passive presence on the dummy — VERIFIED HARMLESS

`content_embedding` sits on all 7,402 dummy nodes. It cannot skew Arm A because:
- **menhir reads `content_embedding` nowhere** (`grep -rn content_embedding src/menhir` is empty — the lane does not exist yet);
- the dummy carries **no vector index** on it (only a `uuid` RANGE index + default LOOKUPs).

**Guard:** the §6.1 parity test (Arm A must reproduce production ranking exactly) is the backstop — if a future change makes Arm A read content_embedding, parity fails and the run is VOID. No action needed now beyond keeping that test.

### H.2 Duplicate-cluster definition — MUST be lane-neutral (was a real skew)

The auto-gen harness (§2.2) credits a hit if ANY duplicate-cluster member is retrieved. **v6 defined the cluster by `content_embedding` cosine — the exact metric the content lane ranks by.** That structurally favors Arm B: clusters are "close under the embedding on trial," so the content lane is more likely to surface a member near the top than BM25 is.

**REQUIRED CHANGE:** define duplicate clusters by a **lane-neutral signal** — lexical near-identity that represents "the same fact stored twice," independent of any lane's ranking function:
- normalized-text exact hash (whitespace/case-folded), OR
- high token Jaccard (e.g. ≥ 0.9) on the recall-text.

Do **not** cluster by `content_embedding`, `name_embedding`, or BM25 score — any of those privileges the corresponding lane. `CorpusReader.duplicate_cluster` (autogen_eval.py) must be implemented lexically. The threshold and method go in the manifest (§8). *(Report both the lexical cluster sizes AND, separately, the content-cosine neighborhood sizes — a large gap between them is itself a finding about dedup debt vs semantic redundancy.)*

### H.3 Synthetic-query inflation — inherent; bounded by the anchor set

The paraphrase query is generated by an LLM reading the summary; `content_embedding` embeds that same summary. Generated queries can be systematically closer in embedding space than a real human's ("synthetic queries are easier"), and the inflation lands on the **content lane specifically** — i.e. on the A→B delta the gate reads, not evenly across arms. This cannot be fully removed while queries are generated, only bounded:

1. **Generator model ≠ embedding model.** Generate with a chat model; embed with `text-embedding-3-small`. Prefer cross-provider if available. Record both in the manifest.
2. **The anchor set (§2.4) is the ground-truth delta.** Anchors are human-authored real queries. **Report the A→B delta on auto-gen and on anchors SEPARATELY.** If the anchor delta is materially smaller than the auto-gen delta, the auto-gen is inflated — **trust the anchors** and treat the auto-gen delta as an upper bound.
3. **The leak check (autogen_eval.looks_like_leak)** already prevents the inverse skew (queries echoing identifiers, trivially favoring BM25). It does not address embedding-side inflation — that is what (1) and (2) are for.
4. **Decision-rule consequence:** graduation (§6.4) requires the win to hold on the **anchor** set too, not auto-gen alone. A B-over-A graduation driven only by auto-gen, absent on anchors, is treated as NO-GRADUATE (suspected synthetic-query artifact).

### H.4 Dummy incompleteness (not skew, but blocks Arm A parity)

The dummy currently has **no `node_name_and_summary` fulltext index and no graphiti vector indexes** — so `RecallService.recall()` Arm A would run with **no BM25 lane at all** and fail §6.1 parity trivially. The prior cosine-only experiments worked only because they called `vector.similarity.cosine()` directly in Cypher, bypassing indexes. Top-up (§3) must add: the fulltext index, the vector index(es), all Entity↔Entity relationships, and the ~44.7k structural Entity nodes (prod has 52,071 Entity total vs the dummy's 7,402). The expensive part (content embeddings) is already done and is retained.
