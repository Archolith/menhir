# HANDOFF → Fold Algebra design session

**Date:** 2026-07-02 · **For:** the next session (cold-start OK) · **Type:** design brief, not a code task
**Project:** menhir-frontier · **Companion charter:** `.agent/plans/fold-algebra.md`

## Your task, in one line

Write **one document** — the *minimal fold algebra* Menhir needs — and see whether the deterministic
operations collapse into a small vocabulary. **Design only. Do not implement.** ~30–45 min. A fresh
look is the point; this brief gives you the evidence so you don't re-derive it, not the answer.

## The question (sharp)

> **What is the minimal set of deterministic operations from which every Menhir View can be composed?**

Not "how do I count bikes." If you start implementing `COUNT`, you will next need `SUM`, then
`DATEDIFF`, then `DISTINCT COUNT`, then `WINDOW`, then `GROUP BY` — and you'll have built a tiny
stream processor out of one-offs. Answer the vocabulary question first.

- **If it collapses to a small set (maybe ~6):** you've found the seam — every future View is a
  *composition*, not a new special case. (Third such simplification: oracles→routed subsets,
  Views→one generic shape, now aggregation→a fold algebra.)
- **If it explodes into dozens of one-offs:** equally valuable — you've learned that *before*
  writing thousands of lines.

## The framing shift you're formalizing

```
   was:   Fold  →  View            (a bespoke fold per View)
   now:   Events  →  Fold Algebra  →  View(kind)
```
The "Fold" layer of `event-fold-view-architecture.md` becomes **explicit and shared**. Views stop
being bespoke; they name `(op[, op…], value slot)`.

## Evidence base — the demand is real and already mapped (grist, verify against question text)

The D0 experiment (both arms, full results in `archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`)
grounded which operations the 14 counting questions actually need. Use this as raw material; it
already *looks* like it collapses to a handful — test that.

| operation | questions that need it (qid → answer) | notes |
|---|---|---|
| **CURRENT / LATEST** (stated, supersede prior) | pages=220, bikes=4, playlists=20, pre-approval=$400k, **to-watch=25** | to-watch emitted 25 **and** 20 live → LATEST must pick current. This is the *stateful* corner. |
| **SUM** | bike-spend=$185, 36b9f61e=$2,500, 7527f7e2=$800 | totals never stated; sum of purchase events (move 2). |
| **COUNT / DISTINCT COUNT** | tanks=3, citrus=3, plants=3, 0a995998=3, 6d550036=2, 18dcd5a5=4 | items mentioned separately → count/dedup, not a stated total. |
| **DATEDIFF / WINDOW** | temporal-reasoning slice ("days between…", "last month") | not in the counting-14; comes from the temporal question type. |

Two hard-won facts from tonight to honor in the design:
1. **Representation is solved.** A single state fact collapses retrieval to rank-1 / 1-node / ~21
   tokens (Arm A, 12/14). So a fold's job is only to *produce the right value*; the View + recall
   already privilege it.
2. **Perception of stated totals is reliable; the blocker is the FOLD, not the model** (Arm B: 5/5
   stated totals perceived; the ~9 misses need SUM/DISTINCT/DATEDIFF, which are *deterministic*).
   So the algebra is the real leverage, and it's not model-gated.

## Candidate vocabulary (starting list — collapse/rename freely)

`COUNT · SUM · MIN/MAX · DISTINCT COUNT · CURRENT · LATEST · DELTA · DATEDIFF · WINDOW · TIMELINE · GROUP BY`

## Decisions the document should make

1. **The operation set** — the minimal list; each op as `(events) → value` with a signature.
2. **Pure vs stateful split** — pure reductions (COUNT/SUM/MIN/MAX/DISTINCT) vs stateful reconciles
   (CURRENT/LATEST — read prior View to supersede). *Reconciliation is where correctness bugs live*
   (already flagged in `event-fold-view-architecture.md`); name that corner precisely.
3. **Composition** — is a View(kind) one op, or a small pipeline (e.g. `WINDOW → SUM`, `GROUP BY →
   COUNT`)? Is `GROUP BY` an op or is per-subject keying (`view_key`) already enough?
4. **Map to View(kind)** — which ops feed which value slot; re-confirm whether `CurrentValueKind` is
   needed (tonight's evidence said **not yet** — the counter surface ranked #1 in 12/14).

## Deliverable & success test

- One doc: `.agent/plans/` (or promote the charter `fold-algebra.md` into the full design).
- **Success = the list collapses to a small, named algebra** with a clean pure/stateful split and a
  composition story, OR a documented finding that it doesn't (with the boundary drawn). Either is a
  win; a half-built `COUNT` implementation is not.

## Constraints / anti-goals
- **No implementation.** Resist building the stream processor.
- Minimal, not complete — the goal is the smallest vocabulary that covers real demand.
- Stay inside the Event→Fold→View frame; the algebra *is* the Fold layer made shared.

## Read-first (all committed)
- `menhir-frontier/.agent/plans/fold-algebra.md` — the charter (short).
- `menhir-frontier/.agent/plans/event-fold-view-architecture.md` — the architecture + the
  pure-vs-stateful (reconcile) note + the ViewKind SSOT § STRUCTURE.
- `archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md` — the Arm A/B results the demand
  table above is drawn from.
- Code: `menhir-frontier/src/menhir/infrastructure/view_repository.py` (ViewRepository + CounterKind/
  TimelineKind — where a fold's output lands; `record_counter` already does the LATEST/CURRENT
  supersession).

## Environment
The design needs **no live graph**. Everything is docs + reasoning. (WSL/Docker/`menhir-lme-neo4j`
are shut down; only bring them up if you decide to probe the graph, which this task shouldn't need.)
Branch: `claude/menhir-chain-handoff-doc-7iuat2`. Working tree clean except a few untracked
`scripts/_*.py` (pre-existing, unrelated).
