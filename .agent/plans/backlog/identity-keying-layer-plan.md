# Plan: Identity / keying layer — union-find IdentityView + twin-probe merge guard

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11
**Gap source:** `.agent/research/menhir-cross-domain-representation-research-2026-07-02.md` §B.5
(verdict: *Prototype immediately*; ranked **#1** highest-leverage of the review) +
`.agent/research/crystallization-control-consolidation.md` §6 (twinning guard / false-merge probe).
**Related:** `kappa-replay-perceiver-versioning-plan.md` (identity changes are events → re-key = refold),
`menhir-memory-supersession-and-dedup-plan.md` + `ingest-identity-merge-gating` (today's LLM-only
merge defense this plan hardens).

---

## The gap (one line)

Entity resolution / keying is smuggled inside the LLM perception stage (unversioned, unreplayable,
invisible), so every keyed fold is only as correct as an invisible key — and merges have **no
deterministic false-merge probe**, only an LLM classifier plus coarse scope gating.

## Current default (code-anchored — corrected 2026-07-11 after `perception.py` re-verification)

Partial identity machinery **is already shipped** on the numeric consolidation path — the original draft
overstated the gap:

- `services/perception.py` runs `coreference_candidates` (deterministic candidate generation,
  `fold_algebra.py:125`) → `resolve_coreference` (LLM judge, memoized) → and vetoes on
  `VETO_UNRESOLVED_COREFERENCE` (perception.py:196) when a same-item cluster is left ambiguous; plus
  `canonicalize_measure_key` / `_canonicalize_identities` for key canonicalization. This is live
  (`enable_coref=True` in the pinned consolidation job).
- The **main entity path is also more robust than the draft claimed.** `correlation_service` merges
  via `merge_entity` (absorb + `merged_from`) but **defaults to never-merge**: non-unanimous or
  judge-unavailable → no merge (`correlation_service.py:367,389`, fail-safe). And a **deterministic
  false-merge guard already ships**: `correlation_queries.py:326` vetoes merging **structural or
  path-shaped nodes** (names with `/`, `\`, or a file extension) — the archived merge-eligibility
  guardrail, live in code. So the "prod port under staging port" risk is bounded by fail-safe + the
  judge; it is not wide open.

So the genuinely-absent residual is **two distinct things, of very different sizes** (and only one fits
a "keep the working system" bias):

1. **Deterministic twin-probe — additive, ethos-aligned.** Extend the shipped structural-only merge
   veto (`correlation_queries.py:326`) into a general **attribute-split** check: refuse a merge when
   dates / values / co-mentions / namespaces / branches split systematically across the boundary, even
   for non-structural names the LLM judge might wrongly unify. This *hardens* the existing merge path;
   it changes nothing that works.
2. **Union-find `IdentityView` + `SameAs` fold — a re-architecture, defer.** Pulling keying out of the
   LLM into a first-class deterministic assertion-fold (perception emits `SameAs`; `IdentityView` folds
   via union-find; every fold keys through `find()`) is the review's #1 idea, but it **replaces how a
   working identity system operates** — not additive. Given the current system succeeds (fail-safe +
   structural veto + unanimous judge), this is a later, bigger bet, not near-term.

## Promotion criteria (default → deterministic identity)

- **supported-by-spike** when (1) perception emits identity **assertions** (`SameAs(a,b)`,
  probabilistic, at the boundary, provenance-linked) rather than resolving identity internally; (2) a
  deterministic **IdentityView** folds them via union-find, `find()` yields a canonical rep, and every
  keyed fold keys **through `find()`**; and (3) a deterministic **twin-probe** refuses a merge when
  attributes (dates / values / co-mentions / source-namespaces / branches-repos / contradiction
  profile) split systematically across the proposed boundary.
- **Falsifier (the acceptance test):** hand-label identity clusters for the 6 dedup-count LME
  questions; measure fold accuracy with perception-internal keying vs `SameAs`+union-find keying. No
  delta → identity wasn't the bottleneck → archive.

## Path (how to get there)

1. **Pull keying out of perception.** Perception emits `SameAs(a,b)` assertions only (probabilistic,
   provenance-linked); it no longer silently canonicalizes.
2. **Deterministic IdentityView** — a union-find state (payload-backed View kind); `find()` returns
   the canonical representative; congruence closure extends merges through structure. Identity changes
   are events, so re-keying existing Views on a merge = `refold` on the affected keys (**depends on
   GAP #3**).
3. **Twin-probe merge guard** (crystallization §6): before any union (dedup ripening *or* supersession
   merge), probe whether the two halves behave as one object; **refuse** the merge on systematic
   attribute split. Ripening/merge **without** the probe is the one configuration to forbid outright.
4. **Route existing merges through the probe.** Make the twin-probe a precondition of the shipped
   `classify_relation`/dedup path and `ingest-identity-merge-gating`, so LLM judgment is no longer the
   *sole* false-merge defense.
5. **Every keyed fold keys through `find()`** so SET/COUNT/LWW correctness inherits the resolved
   identity.

## Non-goals

- Not a new box in the architecture diagram — it is one responsibility (keying) relocated from the
  probabilistic (perception) side to a deterministic, assertion-driven stage.
- Do not make merges an irreversible hard hash — keep them reversible via replay (drop the bad
  assertion, refold).

## Risks

- **Wrong merges poison folds** — mitigate with span-grounded assertions (GAP #6) and replay-based
  reversibility; the twin-probe is the structural guard the current LLM-only path lacks.

## Source

Doc §B.5 (Identity View / union-find) + crystallization §6 (twinning guard). Reconciles the earlier
twin-probe sub-gap recorded against the dedup plan. No `union_find`/`IdentityView`/`SameAs` in `src`
(confirmed 2026-07-11).
