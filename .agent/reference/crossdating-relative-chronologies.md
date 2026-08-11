# Research Note: Crossdating and Relative Chronologies for Event-Sourced Memory

**Status:** Frontier research
**Priority:** Medium (post-MVP)
**Requires:** Event log, replay, provenance, deterministic Views (already present)

---

# Motivation

Menhir currently assumes that memories arrive with reliable timestamps or can be assigned one during perception.

Real conversations frequently violate this assumption.

Examples:

* "around when we switched ORMs"
* "before the migration"
* "after the flaky test incident"
* "during the GPU shortage"
* "when we were still using OAuth v1"

Humans often remember **order** long before they remember **dates**.

Today's systems either:

* assign an uncertain timestamp immediately, or
* permanently leave the event undated.

Neither is ideal.

This research explores a third state:

> **Ordered but not yet absolutely dated.**

---

# Observation

Several mature scientific disciplines solve exactly this problem.

Most notably:

* dendrochronology
* ice-core dating
* sediment chronology

These disciplines distinguish between:

* relative chronology
* absolute chronology

A sequence can be perfectly ordered without knowing where it belongs on the calendar.

Later, when enough evidence exists, the sequence is aligned to an existing dated chronology.

This process is called **crossdating**.

---

# Architectural Principle

Undated memory is **not** undatable memory.

Instead of forcing uncertain timestamps during ingestion, Menhir could preserve:

* event ordering
* local sequence
* causal relationships

until enough evidence exists to anchor the sequence.

---

# Relative Chronology View

Introduce a View representing only ordering.

It intentionally contains no absolute timestamps.

Example:

```text
Deploy

↓

Authentication failures

↓

Rollback

↓

Hotfix

↓

Postmortem
```

This structure is already useful for recall.

No wall-clock time is required.

---

# Crossdating

Later, one event becomes anchored.

Example:

```text
Rollback

↓

Git commit

↓

2026-04-18
```

Every surrounding event now inherits a constrained temporal estimate.

The system upgrades

```text
ordered only
```

into

```text
approximately dated
```

without changing the underlying event log.

No perception replay is required.

Only deterministic chronology alignment.

---

# Marker Horizons

Many projects naturally contain globally recognizable events.

Examples:

* Python migration
* React upgrade
* production outage
* repository split
* company rename
* database migration
* API v2 launch

These become **marker horizons**.

Different timelines can be aligned through these shared events even if they were created independently.

This allows:

* project memory
* Git history
* issue tracker
* documentation
* multiple agents

to synchronize retrospectively without requiring synchronized clocks during ingestion.

---

# Sensitive Chronologies

Tree-ring science uses trees that react strongly to environmental stress.

Those trees produce distinctive growth patterns and are far easier to align.

Memory systems appear to have an analogous property.

Routine events:

* daily standups
* successful builds
* routine commits

carry little chronological information.

Highly variable events:

* production incidents
* architecture migrations
* debugging sessions
* major feature work

produce distinctive temporal signatures.

Future work could automatically identify these "high-sensitivity" subjects using existing Menhir metrics:

* instability counters
* revision frequency
* entropy rate
* change density

These become preferred anchor candidates.

---

# Multi-Chronology Rather Than One Master Timeline

One modification to the original transfer is recommended.

Rather than maintaining one global chronology, maintain multiple overlapping chronologies.

Examples:

* project chronology
* repository chronology
* branch chronology
* conversation chronology
* subject chronology
* debugging chronology

Marker horizons become bridges between chronologies rather than forcing everything into one master timeline.

This is more resilient and better reflects distributed software projects.

---

# Relationship to Existing Menhir Architecture

This proposal does **not** replace Chronostratum.

Instead it fills a gap.

Current flow:

```text
Event

↓

valid_at

↓

Timeline
```

Extended flow:

```text
Event

↓

Relative chronology

↓

Crossdating

↓

Absolute chronology

↓

Timeline
```

The existing event log remains immutable.

Crossdating is simply another deterministic View.

---

# Potential View Types

Possible future Views:

* RelativeChronologyView
* MarkerHorizonView
* ChronologyAlignmentView

None require changes to replay or provenance.

---

# Research Questions

1. How accurately can relative event sequences be anchored after additional evidence arrives?

2. What types of events become reliable marker horizons?

3. Can existing instability and entropy metrics automatically identify high-value chronology anchors?

4. How many LLM timestamping errors disappear if events remain "ordered but undated" until later alignment?

---

# Smallest Falsification Experiment

Construct a dataset of conversational memories containing temporal phrases without explicit dates.

Examples:

* before X
* after Y
* around Z
* during migration
* shortly following deployment

Compare two approaches:

**Baseline**

LLM assigns timestamps during perception.

**Crossdating**

Store only relative ordering.

Later align against dated project events.

Measure:

* timestamp accuracy
* temporal reasoning accuracy
* replay correctness
* confidence calibration

If delayed alignment consistently outperforms immediate timestamp estimation, the mechanism earns a place in Menhir.

---

# Assessment

This proposal does not introduce a new storage model.

Instead it introduces a missing temporal state:

```text
Unknown
↓

Ordered

↓

Approximately Dated

↓

Precisely Dated
```

Current memory systems generally collapse directly from "unknown" to "dated."

The additional intermediate state may allow significantly more accurate temporal reasoning while preserving deterministic replay.

The core architectural insight is simple:

> **A chronology can exist before absolute time exists.**

That distinction may become increasingly important as memory systems evolve beyond timestamped event stores toward richer temporal reasoning.

---

# Transfer-Fidelity Review (appended 2026-07-03)

Reviewed against the originating dendrochronology/ice-core transfer (session of 2026-07-02) and the current
codebase. **Verdict: endorsed.** The one deliberate divergence from the original transfer — multiple overlapping
chronologies bridged by marker horizons, instead of one master timeline — is an improvement, and is truer to the
source discipline than the original was (dendro in practice builds regional chronologies bridged by shared
signals; a single global master was a simplification). The Chronostratum reference is real and the coexistence
claim is coherent: Chronostratum (`src/menhir/domain/temporal.py`, bitemporal happened-vs-learned fact metadata)
handles *known* timestamps; this proposal handles the *ordered-but-undated* state upstream of it.

Four sharpenings recorded for whoever picks this up (none change the proposal's shape):

1. **"Approximately Dated" must be an interval, not a fuzzy point.** Crossdating yields bounds — after commit X,
   before incident Y ⇒ `valid_at ∈ [t₁, t₂]` — and the ChronologyAlignmentView should store exactly that.
   Collapsing to a point estimate re-creates the false-precision failure this proposal exists to eliminate, and
   intervals are what the belief gate / temporal lens can actually reason over honestly.
2. **Ordering assertions are perception outputs, not ground truth.** "Before the migration" is an extraction; it
   needs the same span-grounding verification gate as extracted values. The relative-chronology *fold* is
   deterministic given the ordering assertions — the assertions themselves are probabilistic and must stay on the
   perception side of the boundary, provenance-linked like any typed event.
3. **Ordering evidence forms a DAG with conflicts, not a chain.** The diagrams above are linear; real assertion
   sets will contain cycles and contradictions (A before B, B before A). The fold needs a declared conflict
   policy: maintain a partial order, demote contested edges (CANDIDATE tier), never silently linearize.
4. **Two distinct alignment modes should be named.** (a) *Constraint propagation* — interval arithmetic over the
   ordering DAG from anchored nodes (the rollback→commit example above); (b) *pattern correlation* — matching a
   floating sequence's event-signature against a dated chronology (the actual dendro crossdating mechanism).
   Both are deterministic; they fail differently (under-constraint vs. false match) and should be measured
   separately in the falsification experiment.

Related: fold-algebra design (`.agent/reference/fold-algebra.md`) — RelativeChronologyView is a LIST/DAG-shaped kind
and ChronologyAlignmentView is arguably the first *derived* View (a fold over Views), which connects this note to
the derivation-layer finding in
`.agent/reviews/menhir-cross-domain-representation-research-2026-07-02.md` §4.
