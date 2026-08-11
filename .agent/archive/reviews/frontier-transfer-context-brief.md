# Menhir context brief — for frontier-transfer-review in a fresh session

> **Archived 2026-08-11.** This transfer input describes an earlier architecture and symptom set;
> regenerate a fresh context brief before commissioning another independent transfer review.

**Purpose:** paste this (plus the skill) into a chat with no Menhir context. It contains architecture and
measured symptoms only — **no existing research hypotheses** — so that convergence with prior transfers is
evidence, not echo. Do not add conclusions from `.agent/research/` or `.agent/reviews/` to this brief.

---

## What Menhir is

Menhir is a long-lived graph memory system for autonomous coding agents. It stores conversation and work
history as events in Neo4j and answers recall queries ("how many times did X fail", "what do I currently
believe about Y", "when did Z happen") for agents working on software projects over months.

## Architecture (accepted, do not relitigate)

```
Raw events (immutable, append-only, provenance-bearing)
  → Perception (LLM allowed ONLY here: prose → typed events)
  → Fold / Reconcile (deterministic, replayable)
  → View (generic supersedable state node; one node shape, many kinds)
  → Normal recall (vector + BM25; Views compete in the same pool as ordinary memories)
```

- Views are additive projections over events, never replacements. Old View versions are kept,
  linked by SUPERSEDES, excluded from default recall.
- Fold vocabulary today: SUM/COUNT, EXTREME (latest/min/max), SET (distinct), LIST (timeline).
- Everything recallable carries namespace, scope (SESSION / CANDIDATE / PERSISTENT), freshness
  (ACTIVE / COMPRESSED / GONE), and provenance links (MENTIONS → source episodes).
- A CANDIDATE tier exists: low-trust nodes excluded from recall until human-approved.
- Replay exists: raw events are ground truth; Views can in principle be recomputed.

## Accepted conclusions (assume true)

- Read-time reranking showed diminishing returns; write-time representation is the highest-leverage area.
- A single consolidated state fact collapses retrieval cost ~6× (measured); representation beats selection.
- Probabilistic reasoning belongs only at the perception boundary; folds must stay deterministic.
- Goal: REMOVE complexity — prefer abstractions that replace multiple special cases over new features.

## Measured symptoms (raw, unsolved — this is where transfers should aim)

1. **Extraction is precise on stated facts, absent on derived ones.** An LLM detector found 5/5 totals that
   users stated outright, and correctly abstained on ~9 questions whose answers were never stated but must
   be computed over scattered events (sums, distinct counts, date arithmetic).
2. **Over-extraction is moderate but real.** On held-out data the detector emitted plausible-but-irrelevant
   durable facts, single possessions as "count = 1", and one-off transient amounts. A wrong current-state
   fact would out-rank the truth at recall.
3. **Supersession is arrival-ordered.** When state changes over time (a list went 25 → 20 → 25), the current
   version is whichever wrote LAST, not whichever is temporally latest. Fed out of order, stale state wins.
4. **Temporal vagueness.** Users say "before the migration", "around when we switched ORMs", "after the flaky
   test incident". Perception either forces an uncertain timestamp or leaves the event undated forever.
5. **Entity duplication.** Ingestion mints graph entities per episode; the same real-world thing ("my bike",
   "the trek", "bike #2") ends up as several small recallable nodes. A post-hoc correlation service merges
   some of them. Nodes-per-real-identity has never been measured but is believed > 1 on busy subjects.
6. **Answer cost is the objective.** The house metric ("retrieval entropy") measures how much evidence a
   query must assemble before it is answerable: rank of the first sufficient node, memories walked, tokens
   walked. Consolidation exists to drive this toward rank 1 / 1 node / ~20 tokens.
7. **Recall double-pays.** A View and the episodes it summarizes can both surface for the same query; the
   consolidation win at rank 1 partially leaks back at ranks 2–5.
8. **No end-of-life for facts.** The system can record that a value changed, but has no way to record that
   a thing ceased ("I sold the bike", "we abandoned that service") other than hoping a new value shows up.

## Constraints for the review

- Do NOT propose: rerankers, embedding schemes, vector indexes, graph databases, prompts.
- Prefer write-time, deterministic, provenance-preserving mechanisms.
- Prefer mechanisms that remove complexity (replace several special cases with one rule).
- Every proposal must include its failure modes and the cheapest experiment that would falsify it.
- Optimize for: autonomous coding agents, long-lived project memory, temporal reasoning, code archaeology,
  provenance, belief evolution, debugging. A public benchmark exists but is validation only.

## Usage

1. Start a fresh session (no Menhir memory/context).
2. Paste this brief.
3. Invoke the frontier-transfer-review skill with a discipline of your choice (or let it pick).
4. Compare output against `.agent/research/` notes: independent convergence on a mechanism already proposed
   strengthens that mechanism; genuinely new mechanisms extend the lane; contradictions are the most
   valuable outcome — record them.
