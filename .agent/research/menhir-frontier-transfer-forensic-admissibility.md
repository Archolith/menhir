# Frontier Transfer Review — Evidence Law & Admissibility -> Menhir

- Date: 2026-07-03
- Skill: frontier-transfer-review
- Discipline: forensic evidence scholarship / trial admissibility (35-year persona)
- Target: menhir / LME write-time representation
- Context anchors: belief-gate exists on frontier fork (compute/expose only, no active filtering); CANDIDATE tier wired (feat/candidate-tier)

---

## Part 1 — The assumption AI memory gets wrong

AI memory assumes weight can compensate for admissibility: put everything in the pool, rank it, trust the top. Evidence law ran that experiment for four centuries and closed it. A fact-finder cannot reliably discount unreliable evidence after exposure, so reliability is enforced by categorical exclusion before exposure (FRE 104(a)), keyed on HOW the assertion came to exist (its foundation), never on whether it looks true. Content-based screening is what courts refuse to do, because plausibility is the failure mode: the most damaging inadmissible evidence is the plausible kind. Menhir symptom 2 restated: lay opinion is admitted because it sounds like fact, hoping voir dire happens at ranking time. It never does.

Second wrong assumption: one clock. Every instrument bears its execution date; every docket bears its filing date; no court confuses the two. Symptom 3 (arrival-ordered supersession) is a probate court revoking wills in filing order. A later-discovered EARLIER will does not revoke a later one, ever — structurally impossible, not an edge case.

## Part 2 — Mechanisms

### 2.1 Foundation-typed admission (FRE 104(a), 602, 701/702, 803(6), Daubert)

Data structure: every offered item carries a foundation record — declarant, basis in {STATEMENT, RECORD, DERIVED, OPINION}, personal-knowledge flag, execution date. Populated at intake, never inferred later.

Gate algorithm (write time, before pool membership):
1. Statement by identified declarant with personal knowledge -> admitted (FRE 602).
2. Record from a regular reliable process, made at/near the event, by someone with knowledge -> admitted (FRE 803(6)); every condition checkable from the foundation record alone.
3. Opinion/derived conclusion from a LAY source -> excluded categorically (FRE 701). Not down-weighted. Excluded.
4. Derived conclusion from a VALIDATED process -> admitted iff process is testable, has known error behavior, applied reproducibly (Daubert). Validated-process list is short, enumerated, versioned.

Invariants: nothing reaches the fact-finder without a foundation class; weight computed only over admitted items; judge and jury never swap jobs.

Failure modes: (a) exception creep (FRE 803 has 23 exceptions; the fold vocabulary will feel the same pressure); (b) fabricated foundations (perception mislabels inference as statement — counter with voir dire = spot audits of foundations against sources); (c) over-exclusion (keep a narrow residual valve, FRE 807 analog, used rarely and on the record).

### 2.2 Execution-date ordering with anchored dates (wills doctrine; fixing the time)

Supersession compares asserted_at (execution), never recorded_at (filing). Docket stays append-only in arrival order; validity ordering is a separate recomputable sort key. An undated instrument gets no guessed date — it becomes a factual dispute and cannot supersede a dated incumbent; burden on the proponent.

For vague time: never force a timestamp. Store the relation — BEFORE(anchor), AFTER(anchor), AROUND(anchor) — against established anchor events. Chronology is a partial order that tightens deterministically as anchors acquire dates. "Before the migration" is an upper bound, i.e., a date constraint, not a missing date.

Invariant: ordering decisions are monotone — new evidence can tighten an interval, never silently reverse a resolved ordering without passing through contested status.

Failure modes: contradictory anchors -> impeachment: ordering enters contested status, excluded from supersession until resolved. Misdated instruments -> objection path as in 2.1(b).

### 2.3 Summary substitution (FRE 1006)

Voluminous records may be proved by a summary. The summary IS the evidence. The originals must be made available for examination but do not go to the jury. A successful accuracy challenge excludes the summary and restores the originals — atomically, one or the other, never both.

Invariant: for a given issue, summary XOR sources before the fact-finder.

Failure mode: an inaccurate/incomplete summary hides truth in unexamined sources. Remedy = inspection right + objection procedure, NOT showing both. Juries double-count: a summary corroborated by its own sources reads as two pieces of evidence.

### 2.4 Authentication at intake (FRE 901; chain of custody) — scored, not elevated

Nothing is "the gun" until a 901 finding. Identity assigned at intake (exhibit number, against a registry); all later references cite the number. Symptom 5 (mint-per-episode, merge post hoc) is re-collecting the same gun at every hearing. Fix: intake-time resolution against a registry; unresolvable mentions filed as unidentified-pending (CANDIDATE used for identity), never minted as recallable identities.

### 2.5 Presumption of continuance + terminating instruments — scored, not elevated

A condition shown to exist is presumed to continue until evidence of change. Statuses end only through recognized terminating instruments (death certificate, decree, discharge) or the deterministic absence rule (7 years unexplained absence -> rebuttable presumption). Symptom 8 needs a first-class CESSATION event and a View lifecycle state CLOSED, distinct from SUPERSEDED — a thing that ceased is not a thing with a newer value.

## Part 3 — Translation into Menhir

Event changes: two timestamps per typed event — asserted_at (instant | interval | relation-to-anchor) and recorded_at (exists). Foundation fields: declarant, basis in {STATEMENT, RECORD, DERIVED, OPINION}. New event kind: CESSATION(subject, asserted_at).

Perception changes: perception is a lay witness. May emit only STATEMENT events (quoted utterance, declarant) and CESSATION where stated. Barred from emitting DERIVED (counts, sums, "current state") regardless of plausibility. Over-extraction becomes a mechanically detectable rule violation: DERIVED-shaped assertion with basis=STATEMENT and no quoted utterance fails foundation. The existing belief-gate gets its activation rule: check foundations (deterministic predicate), not trust scores.

Fold changes: folds are the only lawful source of DERIVED facts. Each fold carries a Daubert card: definition, input event types, known failure behavior, replay hash. Fold output admissible per se. Symptom 1 named precisely: the 9 unanswerable derived questions are missing folds, not missing extraction.

Reconcile changes: supersession compares asserted_at, never recorded_at. Undated assertions cannot supersede dated incumbents; they queue as contested. One comparison-key change plus one guard.

View changes: lifecycle state CLOSED (via CESSATION or absence rule), distinct from SUPERSEDES. Views record the recorded_at high-water mark of folded events, defining 1006 coverage.

Recall changes (mechanical pool membership, not a reranker): (a) admission gate precedes pool membership — only foundation-passing nodes are recallable; replaces per-item human approval for the common case; humans arbitrate objections only. (b) 1006 substitution: episodes MENTIONS-covered by an ACTIVE View excluded from the default pool for that namespace/kind; still reachable via provenance drill-down (inspection right); challenge/invalidation lifts the exclusion atomically.

## Part 4 — Impossible question made trivial

"Strike that from the record." When a source proves unreliable (declarant lied, session corrupted, extractor version buggy), exclude everything that entered through it and re-evaluate on the remaining record (fruit of the poisonous tree). Menhir has the expensive half (replay); foundations supply the cheap half: exclude declarant D or extractor v at the gate, replay folds, diff Views. "What would we believe if that session had never happened?" becomes a deterministic query.

## Part 5 — Conserved quantity

The record. Nothing reaches the fact-finder except through the record; everything in the record has a foundation; the verdict is a function of the admitted record and nothing else. Menhir's conservation law: belief is conserved from admitted evidence. Folds reorganize admitted facts, never create them; perception transcribes, never concludes. Any recallable node whose provenance chain fails to terminate in admitted raw events is a conservation violation — mechanically checkable once basis exists. Menhir has provenance; it lacks the ADMITTED predicate that turns provenance into a conservation law.

## Part 6 — Attack

Primary: "Views compete in the same pool as ordinary memories" is provably wrong in this field. Symptom 7 is not a leak; it is the designed consequence of handing the jury the summary AND the sources and asking ranking to reconcile. FRE 1006 exists because juries double-count corroborated-looking duplicates. Fix = substitution with inspection right, not competition.

Secondary: CANDIDATE tier as per-item human approval is a gate no court could operate (caseload). Rules admit; humans hear objections. The tier should be the contested-items docket, not the front door.

## Part 7 — Cheapest falsifying experiments

E1 (foundation gate): hand-classify the held-out over-extraction set (symptom 2) by basis (quoted statement vs derived/opinion). If bad and good emissions show the same basis distribution, the gate cuts nothing — falsified. No code.

E2 (execution-date supersession): replay 25->20->25 in all six arrival permutations with asserted_at attached; current View must equal temporally-latest in all six. Starvation check: on N real state-change events, measure the fraction with recoverable asserted-time distinct from arrival. Low fraction -> anchors become load-bearing -> falsified as a simple transfer.

E3 (1006 substitution): on retrieval-entropy queries where a View ranks first, filter its covered episodes from the candidate pool (query-side, experiment only). Measure rank 2-5 contamination and answer correctness. Correctness drop -> Views under-cover -> substitution would hide truth -> falsified.

## Scoring

| Mechanism | Novelty (AI memory) | Arch impact | Effort | Risk | MVP | Research |
|---|---|---|---|---|---|---|
| 2.1 Foundation-typed admission | High | High (replaces trust scoring, over-extraction filtering, CANDIDATE-as-front-door with one rule) | Medium | Medium (over-exclusion; needs residual valve) | High | High |
| 2.2 Execution-date + anchored ordering | Medium (bitemporal DBs settled; anchor-relations at perception are not) | High | Low core / Medium anchors | Low | High | Medium |
| 2.3 1006 summary substitution | High | Medium (deletes symptom 7) | Low | Low (E3 guards the real failure) | High | Medium |
| 2.4 Exhibit registry at intake | Medium | Medium | Medium | Medium | Medium | Low |
| 2.5 Continuance presumption + CESSATION | Medium | Low-Medium | Low | Medium (horizon tuning heuristic) | Medium | Medium |

## Elevated — three ideas worthy of further research

1. Foundation-typed admission (2.1). One deterministic write-time rule — basis decides pool membership, content never does — replaces over-extraction filtering, trust scoring, and the human-approval front door, and gives the dormant belief-gate its activation predicate. Largest complexity removal.
2. Execution-date supersession with anchored dates (2.2). Fixes symptom 3 with a comparison-key change plus one guard; dissolves symptom 4's false dichotomy (guess a timestamp vs stay undated) by storing the relation and propagating.
3. 1006 summary substitution (2.3). Summary XOR sources, inspection right, atomic un-substitution on challenge. Deletes symptom 7 by construction rather than by ranking.

Not elevated: 2.4 adds an intake service rather than removing complexity; 2.5's absence horizons are the only non-deterministic element offered.

Closing: the architecture already keeps the docket (append-only events), the transcript (provenance), and the ability to retry the case (replay). It lacks a rule of decision for what the fact-finder is permitted to see. The courthouse is built; the judge is missing.
