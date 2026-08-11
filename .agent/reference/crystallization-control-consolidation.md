# Research Note: Crystallization Control for Memory Consolidation

**Project:** Menhir / Archolith
**Status:** frontier research
**Priority:** high for identity/consolidation research; medium for MVP
**Core idea:** memory systems should control when canonical structures form, grow, merge, quarantine, and leave the active recall pool.

---

## One-sentence thesis

Menhir should treat canonical memory structures like crystals: **hard to nucleate, easy to grow once stable, directionally merged when small/duplicate, and periodically refined so low-quality evidence is segregated rather than silently trusted or silently lost.**

---

## Why this matters

Menhir has already moved from read-time selection toward write-time representation:

```text
Event
→ Fold / Reconcile
→ View
→ Recall
```

But one problem remains: the system can still create too many weak canonical objects too early.

That creates "memory powder":

```text
same real-world identity
→ many tiny nodes / facts / views
→ higher recall cost
→ contradictory or redundant candidates
→ poorer agent behavior
```

The crystallization transfer gives Menhir a control theory for avoiding this.

---

# 1. Nucleation Control

## Original mechanism

In crystal growth, a new crystal does not form just because material is present.

A tiny cluster has a surface-energy cost. Below a critical radius, it dissolves. Above that radius, growth becomes favorable.

Industrial crystallizers operate inside a **metastable zone**:

```text
existing crystals can grow
new crystals cannot freely nucleate
```

This prevents a nucleation shower: thousands of tiny useless crystals.

## Menhir translation

A new canonical memory object should not be created just because perception saw one mention.

Instead:

```text
single weak mention
→ subcritical candidate

repeated independent support
→ crosses critical evidence mass
→ canonical View / Entity / identity cluster
```

Normal operation should prefer:

```text
grow existing structure
```

over:

```text
create new structure
```

## Design law

```text
New Views / canonical nodes should be difficult to create.
Existing Views / canonical nodes should be easy to strengthen.
```

## Menhir target

This applies especially to:

* Entity identity
* Candidate memories
* QuantState / View creation
* Similar near-duplicate facts
* Recurring failure/attempt categories
* Preference/state facts

---

# 2. Ostwald Ripening

## Original mechanism

In a population of crystals, smaller crystals dissolve and redeposit onto larger crystals.

The direction is not subjective.

Small dissolves into large.

## Menhir translation

When two clusters appear to represent the same identity or memory structure:

```text
smaller / weaker cluster
→ dissolves into
larger / better-supported cluster
```

The provenance does not disappear. It is redeposited onto the survivor.

## Design law

```text
Duplicate consolidation should be directional.
The weaker cluster should dissolve into the stronger one.
Provenance must move, not vanish.
```

## Why this matters

Today, Menhir can end up paying for many small overlapping nodes. Ripening gives a deterministic default policy:

```text
merge direction = evidence mass / support size / provenance strength
```

rather than an arbitrary or LLM-mediated choice.

---

# 3. Zone Refining

## Original mechanism

Zone refining purifies material by moving a molten zone through it. Impurities prefer one phase and are dragged toward the end.

Purity is not achieved at growth time.

Purity is achieved by repeated refinement passes.

## Menhir translation

Do not expect ingestion/perception to be perfect.

Instead, run periodic refinement passes:

```text
canonical memory
→ verifier pass
→ weak / contradicted / ungrounded support segregated
→ quarantine
→ possible crop/archive
```

Bad evidence is not deleted silently.

It moves to a known address.

## Design law

```text
Purity is a process product, not an ingestion property.
```

## Practical Menhir passes

Possible refinement checks:

* span-grounded verification
* source confidence checks
* contradiction checks
* transient fact checks
* currentness checks
* namespace/scope stamp checks
* identity-twin checks
* provenance completeness checks

Each pass should measure:

```text
k = fraction of known-bad material moved per pass
```

If k is near zero, the pass is useless.

---

# 4. Quarantine and Cropping

## Original mechanism

Zone refining concentrates impurities at one end of the ingot. Then the dirty end is cropped.

A refinery that never crops has not refined; it has merely moved dirt around.

## Menhir translation

Menhir currently tends to preserve everything near the working graph.

That is safe for provenance but risky for recall.

A better phase model:

```text
canonical
candidate
quarantine
cropped/archive
```

Cropping does **not** mean deletion.

It means removal from the active working recall volume, with recovery possible through provenance/debug tools.

## Design law

```text
Evidence should not vanish.
But low-quality evidence must be allowed to leave the active recall pool.
```

## Caution

This is powerful but dangerous.

Cropping should come after:

* provenance is conserved
* quarantine is observable
* recovery path exists
* metrics prove active recall improves

---

# 5. Evidence-Mass Conservation

## Original mechanism

In refining, mass is conserved. Impurities move, but they do not magically disappear.

## Menhir translation

Every consolidation/refinement pass should conserve provenance-weighted evidence mass.

If evidence leaves canonical memory, it must appear somewhere else:

```text
canonical
→ candidate
→ quarantine
→ cropped archive
```

A pass is invalid if evidence disappears without a recorded destination.

## Design law

```text
No consolidation pass may create or destroy evidence mass.
It may only move evidence between phases.
```

## Why this is important

This gives Menhir a deterministic audit:

```text
before pass:
  total evidence mass = X

after pass:
  canonical + candidate + quarantine + cropped = X
```

If not, the pass is buggy.

This is a very strong invariant for a memory system.

---

# 6. Twinning Guard for False Identity Merges

## Original mechanism

In crystals, twins can masquerade as a single crystal. They share a boundary but respond differently under probes such as polarization or diffraction.

The false union is detected by response differences, not by staring harder at the boundary.

## Menhir translation

Two identities may look similar enough to merge:

```text
same name
same topic
similar embeddings
```

But they may respond differently to probes:

* different dates
* different values
* different co-mentions
* different source namespaces
* different associated actions
* different branches/repos/projects
* different contradiction profiles

## Design law

```text
Before merging identities, probe whether the two halves behave as one object.
```

If attributes split systematically across the proposed merge boundary, refuse the merge.

This is especially relevant for the future IdentityView / SameAs fold.

---

# What this removes

This transfer could simplify or replace several scattered mechanisms:

* ad hoc dedup heuristics
* weak candidate promotion rules
* arbitrary merge direction
* silent trust/dismissal of questionable evidence
* unbounded growth of near-duplicate recallable nodes
* manual cleanup of bad extractions

It does **not** replace provenance, replay, Views, or folds.

It strengthens them.

---

# Fit with current Menhir architecture

Current architecture:

```text
Event
→ Fold / Reconcile
→ View
→ Recall
```

Crystallization control adds phase discipline:

```text
Event
→ subcritical candidate
→ canonical View / Entity
→ refinement passes
→ quarantine
→ cropped archive
```

And evidence conservation across all phases:

```text
canonical + candidate + quarantine + cropped = total evidence mass
```

---

# Highest-value MVP experiment

## Nucleation/ripening experiment

Take one namespace.

Measure current "memory powder":

```text
cluster similar Entity nodes
estimate nodes per real-world identity
measure token cost of duplicate/subcritical nodes in recall
```

Then replay or simulate a nucleation rule:

```text
do not create canonical node until ≥2 independent evidence sources
otherwise attach to nearest existing node or hold as candidate
```

Measure:

* cluster size distribution
* duplicate count
* D0 / answer cost
* recall token footprint
* support reachability
* regressions from missed real identities

## Falsifier

If cluster distribution does not coarsen and recall footprint does not improve, nucleation control is not worth pursuing.

---

# Second experiment

## Zone-refining experiment

Use known-bad perception outputs from Arm B:

* transient one-off costs
* single possessions extracted as value=1
* weak or ungrounded totals

Run verifier passes:

```text
pass 1: span/transience check
pass 2: contradiction/currentness check
pass 3: provenance/support check
```

Move failures to quarantine.

Measure:

```text
k = fraction of known-bad evidence segregated per pass
```

## Falsifier

If the passes cannot distinguish dirt from crystal, then quality remains deposition-limited and the refinement approach fails.

---

# Risk

The largest risks:

1. **Over-starving real new entities**

   * If nucleation threshold is too high, legitimate new memories stay subcritical too long.

2. **Over-merging**

   * Ripening could dissolve minority-but-real identities into larger wrong clusters.

3. **Over-cropping**

   * Valuable historical evidence could leave active recall too aggressively.

4. **Operational complexity**

   * Phase labels and evidence-mass audits add machinery.

These risks are manageable only if the system preserves provenance and keeps recovery paths.

---

# Recommendation

Preserve this as a research lane, not immediate MVP work.

However, two parts are strong enough to become near-term design laws:

```text
1. New canonical structures should require critical evidence mass.
2. Consolidation passes must conserve evidence mass.
```

The first prevents memory powder.

The second makes refinement auditable.

Together they turn consolidation from an opaque cleanup process into a measurable physical process:

```text
grow
ripen
refine
quarantine
crop
audit mass
```

---

# Strongest sentence

Memory systems should not try to make perception perfect.

They should assume as-grown memory is impure, then engineer repeatable refinement passes that move impurities out of canon while conserving evidence.

---

# Transfer-Fidelity Review (appended 2026-07-03)

Reviewed against the originating gemcutting/crystal-growth transfer (session of 2026-07-03) and the current
codebase. **Verdict: endorsed**, including the recommendation split — research lane overall, with the two design
laws (critical evidence mass for nucleation; mass-conserving consolidation passes) promotable near-term. Four
sharpenings and one honesty note, recorded rather than silently edited in:

1. **The twinning guard (§6) is a precondition of ripening (§2), not a sibling.** Ripening makes dissolution
   directional and *unconditional*; that is exactly where risk #2 (minority-but-real identities absorbed into
   larger wrong clusters) lives. Wire them: every ripening merge runs the twin probe first; split extinction
   refuses the dissolution. Ripening without the probe is the one configuration this note should forbid outright.
2. **Evidence mass must be counted over distinct event identities, or the ledger double-counts.** Re-observation
   of an already-folded event refreshes provenance without adding mass — the existing `view_sig` no-op path
   already behaves this way, and the fold algebra's Law-2 (replay/dedup) is the same constraint. Define
   mass = |distinct supporting event identities|, phase-tagged; otherwise replays inflate the books and the
   §5 audit passes on garbage.
3. **Express the phase model in the existing lattice, not beside it.** Menhir already has `scope`
   (SESSION / CANDIDATE / PERSISTENT), `freshness`, and `view_current`. Canonical/candidate/quarantine/cropped
   should be coordinates in that existing space (e.g. quarantine ≈ CANDIDATE + a segregation reason;
   cropped ≈ a freshness state) — a parallel phase taxonomy would be precisely the added complexity this
   transfer exists to remove, and is risk #4 realized.
4. **Crop has an embryo in the codebase.** The lifecycle layer already implements COMPRESSED freshness with
   recall-triggered rehydration (`recall_service._schedule_rehydration`) — a crop-with-recovery-path in
   miniature. Build cropping as an extension of that machinery (compression → archive tiers), not as a new
   exit door.

**Prior-art note (method hygiene):** the individual pieces have CS relatives and the note should not claim
otherwise — Ostwald ripening/crop ≈ LSM-tree compaction and tombstone GC; refinement passes ≈ the iterative
data-cleaning literature; quarantine ≈ truth-maintenance out-lists; nucleation thresholds ≈ burst/dedup gating
in stream entity resolution. What appears genuinely uncommon is the *combination under a single conservation
invariant* — consolidation audited by mass balance across phases (§5). That invariant, plus the metastable-zone
operating rule (§1), is the transfer's actual contribution.

Related: `.agent/reference/crossdating-relative-chronologies.md` (same research lane, temporal axis);
`.agent/reviews/menhir-cross-domain-representation-research-2026-07-02.md` (identity View §B.5 — the twin probe
is its missing false-merge guard; derivation-layer finding §4). Both MVP experiments here are cheap replays over
existing data and instruments (correlation clustering + the D0/view-entropy footprint) — they should run before
any further transfer reviews are commissioned, per the eat-what-you-kill rule adopted for this research method.
