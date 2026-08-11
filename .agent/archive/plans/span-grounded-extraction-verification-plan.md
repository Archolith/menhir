# Plan: Span-grounded extraction verification — generalize beyond numeric consolidation

<!-- Filename convention: <feature>-plan.md -->

**Status:** ARCHIVED 2026-07-11 — **CORE SHIPPED** (numeric consolidation, live default-on), and the
only residual (generalize span-grounding to the main ingest path) is the deterministic-check component
of `foundation-typed-admission-plan.md`, which now carries it. This plan is retained for the shipped
code anchors below; it is not standalone backlog work. Superseded-by: `foundation-typed-admission-plan.md`.

> The original sweep draft wrongly claimed "no perception-exit verifier in src" — a keyword-grep miss.
> `services/perception.py` (`_stated_value_grounded` / `_sum_arithmetic_grounded`, guards pinned on in
> `scheduler_tasks.py:424-427`, `sum_grounding` default-on `settings.py:218`) is the shipped verifier.
**Gap source:** `.agent/reference/menhir-cross-domain-representation-research-2026-07-02.md` §C.7.
**Design of record for what shipped:** `.agent/for-review/HANDOFF-2026-07-02-perception-boundary.md`.

---

## The gap (one line, corrected)

Span-grounded extraction verification is **SHIPPED and live-default-on for the numeric personal-memory
consolidation path**; the residual gap is **generalizing it to the main Graphiti ingest path** (arbitrary
extracted facts, not just numeric measures) and sharpening spans to character offsets.

## Current default — what is ALREADY BUILT (code-anchored)

`services/perception.py` is a precision-first, abstaining perception boundary — "when uncertain, do not
write the View" — with the deterministic span checks the §C.7 mechanism asks for:

- **`_stated_value_grounded`** (perception.py:759): a STATED_MEASURE's value must be **literally present
  (as digits) in a linked source span**, else `VETO_UNSUPPORTED_STATED` (line 197). "No source span, no
  stated fact."
- **`_sum_arithmetic_grounded`** + **`_price_token_count`** (807 / 782): deterministic proof a SUM is
  sound from source text, with **anti-double-count** (two `$40` events grounded in one span that says
  `$40` once → not grounded).
- **Conjunctive veto-gate** (`gate`, 969): self-consistency entropy + fold triangulation + embedding
  dedup; any red flag → abstain. Fail-closed by design.
- **Live wiring, guards pinned ON:** the `consolidate_personal_memory` scheduler job
  (`scheduler_tasks.py:424-427`) runs with `enable_cross_check / enable_coref / enable_verify /
  enable_stated_span_guard = True` and `enable_sum_grounding` from
  `personal_memory_consolidation_sum_grounding` (**default True**, `settings.py:218`).

**Scope boundary (the actual residual):** all of the above governs the **numeric measure → counter-View
consolidation** path only. Arbitrary facts flowing through the **main Graphiti entity/fact ingest** are
**not** span-verified; spans are whole-episode `content`, **not character offsets**.

## Promotion criteria (residual → generalized)

- **supported-by-spike** when the span-grounding principle extends **beyond numeric measures to the main
  ingest extraction path**: a non-numeric extracted claim is admitted only if its stated value/subject
  is verifiable against a **character-offset span** of the raw event; failing claims are demoted to
  CANDIDATE, never asserted.
- **Falsifier (unchanged):** re-run the Arm-B detector + verifier on the 12 held-out non-counting
  namespaces; if over-extraction doesn't drop at unchanged true-positive rate, the extension is dead
  weight.

## Path (how to get there — residual only; the numeric core is done)

1. **Char-offset spans** on typed events (sharpen the current whole-episode `content` span to exact
   offsets).
2. **Generalize the grounding checker** from `_SCALAR_REDUCERS` (sum/count/distinct) to arbitrary
   extracted assertions on the main ingest path — same "value literally present in cited span" rule.
3. **Route failures to CANDIDATE** on the general path (the numeric path already abstains/vetoes).
4. Feed the deterministic check into the **belief-gate activation predicate** (join with
   `foundation-typed-admission-plan.md`).

## Non-goals

- Do not rebuild the numeric-path grounding — it is shipped and live.
- Do not screen on content plausibility — verify *grounding*, not truth.

## Risks

- **Low** — recall loss is the safe direction; the numeric path already proves the pattern works.

## Source

Doc §C.7. Confirmed 2026-07-11: numeric grounding SHIPPED + default-on in `perception.py` /
`scheduler_tasks.py` / `settings.py`; `graph-verifiers.md` remains a distinct mechanism
(belief-freshness sync, not extraction grounding).
