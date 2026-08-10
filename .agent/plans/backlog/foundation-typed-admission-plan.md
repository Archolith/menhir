# Plan: Foundation-typed admission (basis-class write-time gate)

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11
**Gap source:** `.agent/research/menhir-frontier-transfer-forensic-admissibility.md` §2.1 + Part 3
(elevated #1 — "largest complexity removal").
**Related:** `cessation-tombstone-primitive-plan.md` (CESSATION is a permitted perception emission),
`admission-capability-separation-plan.md` (that gates *who sets the user tier*; this gates *what basis
a claim has*).
**Absorbs:** the residual of the archived `span-grounded-extraction-verification-plan.md` — its span
checker is SHIPPED for the numeric consolidation path (`perception.py` `_stated_value_grounded` /
`_sum_arithmetic_grounded`); generalizing that deterministic check to the main ingest is now step 6 here.
**Unlocks:** `l3l4-semantic-overlay-sequencing-plan.md` (#2) — its L4 trust tier trusts *declared*
evidence anchors; until this plan verifies anchors resolve, that TRUSTED tier is decorative in an
all-LLM regime (see #2 notes 5–6). Verification here is the prerequisite for #2's trust tiering being worth building.

---

## The gap (one line)

Menhir admits by weight/plausibility — put everything in the pool, rank it, trust the top — with **no
categorical write-time admission keyed on *how* an assertion came to exist** (its basis/foundation).
Lay opinion is admitted because it sounds like fact.

## Current default (code-anchored — corrected 2026-07-11 after `perception.py` re-verification)

The "everything enters the pool" framing is **wrong for the numeric consolidation path**, which already
enforces much of this principle:

- `services/perception.py` is *literally* "confidence is a **conjunctive veto-gate**, abstain when
  uncertain" (module docstring), and it enforces the **stated-vs-fold-derived basis distinction**:
  `_KIND_REDUCER` (perception.py:62) folds item events; **`assertion` is deliberately NOT a reducer** —
  "a stated total is a cross-check (triangulation), never folded into the primary value." So on this
  path, a DERIVED value comes only from a lawful fold, and an unsupported stated claim is vetoed. The
  consolidation job pins all bias guards ON (`scheduler_tasks.py:424-427`).
- **But** this holds only for the **numeric measure → counter-View** path. On the **general Graphiti
  entity/fact ingest**, everything still enters the pool: `source_confidence` tiers by source *label*
  (`ingest_service.py:468`) with no basis classification, no categorical pre-membership exclusion. The
  **belief-gate is dormant** (computes, default-off) and has no activation predicate.
- **Reusable machinery already shipped** (so this is not a from-scratch firewall): the **CANDIDATE
  review tier** (`candidate_repository.py` — `scope='CANDIDATE'`, not recalled/scored until approved,
  contradiction-checked promotion to PERSISTENT, rejection deletes+records) is the "hold a weak claim
  instead of asserting it" half; a **preflight rejection** hook exists at ingest
  (`run_preflight_rejection`); `source_confidence` → `ReviewState` tiering exists. (Note: the existing
  `IngestGate` is *concurrency* control, not admission — do not overload it.)

So the residual is narrow: **a basis classifier at ingest that ROUTES low-basis claims into the
already-shipped CANDIDATE tier** instead of straight to durable memory — generalizing the perception
boundary's proven abstain-gate + STATEMENT/RECORD/DERIVED/OPINION distinction to the main path. Not
inventing admission; wiring a basis decision onto machinery that exists.

## Promotion criteria (default → admitted-record)

The default flips from **"weight compensates for admissibility"** to **"basis decides pool membership;
content never does."**

- **supported-by-spike** when every offered item carries a **foundation record** (declarant, basis ∈
  {STATEMENT, RECORD, DERIVED, OPINION}, personal-knowledge flag, execution date), and a deterministic
  write-time gate decides pool membership by basis:
  1. STATEMENT by identified declarant w/ personal knowledge → **admit**;
  2. RECORD from a regular reliable process → **admit**;
  3. lay OPINION / DERIVED → **excluded categorically** (not down-weighted);
  4. DERIVED from a **validated process** → admit iff on a short, enumerated, versioned Daubert list.
  Weight is computed **only over admitted items**; the belief-gate's activation predicate becomes
  "check foundations", not "check trust scores".
- **Falsifier (E1, no code):** hand-classify the held-out over-extraction set by basis; if bad and
  good emissions show the **same basis distribution**, the gate cuts nothing → falsified.

## Path (how to get there)

1. **Foundation fields on typed events** (declarant, basis, personal-knowledge flag, execution date) —
   populated at intake, never inferred later.
2. **Perception is a lay witness:** may emit only STATEMENT (quoted utterance + declarant) and
   CESSATION (GAP #4); **barred from emitting DERIVED** regardless of plausibility. Over-extraction
   becomes a mechanically detectable violation (DERIVED-shaped + basis=STATEMENT + no quoted utterance
   → fails foundation; runs on GAP #6's span check).
3. **Folds are the only lawful source of DERIVED facts**, each carrying a **Daubert card** (definition,
   input event types, known failure behavior, replay hash); fold output admissible per se.
4. **Admission gate precedes pool membership.** `CANDIDATE` becomes the **contested-items docket**, not
   the front door — rules admit; humans hear objections only.
5. **Narrow residual valve** (FRE 807 analog) — used rarely, on the record — guards over-exclusion.
6. **Generalize the span-grounding checker** (absorbed from the archived span-grounding plan) from the
   numeric consolidation path (`perception.py`, SHIPPED) to the **main Graphiti ingest**: any DERIVED /
   STATEMENT claim's value/subject must be verifiable against a **character-offset span** of the raw
   event, else it fails foundation. This is the deterministic predicate the whole basis gate runs on.

## Non-goals

- Do not screen on content/plausibility (that is the failure mode being closed).
- Do not build the intake exhibit-registry or absence-horizon heuristics here (not elevated in the
  review; the registry belongs with `identity-keying-layer-plan.md`).

## Risks

- **Over-exclusion** — needs the residual valve.
- **Exception creep** — the fold vocabulary will feel FRE 803's 23-exception pressure; keep the
  validated-process list short and versioned.
- **Fabricated foundations** — perception mislabels inference as statement; counter with voir-dire spot
  audits of foundations against sources.
- **It touches the working ingest** (unlike #3/#8, which are additive/read-side) — so roll out **behind
  a flag, shadow-mode first**: classify + record what *would* be routed to CANDIDATE without changing
  behavior, compare against the current path, and only then flip it on. Same pattern the belief-gate
  already uses (`_run_assertion_shadow`). The system is successful today; this must not regress it.

## Source

`.agent/research/menhir-frontier-transfer-forensic-admissibility.md` §2.1, Part 3 (translation), Part 5
(the ADMITTED predicate as a conservation law). "The courthouse is built; the judge is missing."
