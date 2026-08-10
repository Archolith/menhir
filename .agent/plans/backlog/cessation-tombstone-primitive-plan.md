# Plan: Cessation / tombstone primitive (the `Ceased` verb)

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11
**Gap source:** `.agent/research/menhir-cross-domain-representation-research-2026-07-02.md` §A.4
(verdict: *Prototype immediately*) + `.agent/research/menhir-frontier-transfer-forensic-admissibility.md`
§2.5 (CESSATION event + View lifecycle CLOSED).
**Related:** `span-grounded-extraction-verification-plan.md` (cessation statements need span grounding),
`menhir-memory-supersession-and-dedup-plan.md` (supersession = value-change; this = retraction).

---

## The gap (one line)

Menhir can represent "the value changed" but not "the thing **ended**" — retraction/contraction. "I
sold my bike" is neither `bikes=3` nor a stated `bikes=0`; it is a contraction on an ownership key,
and the model literally cannot express it.

## Current default (code-anchored)

- Supersession handles **value-change** (`SUPERSEDED_BY` / temporal `expired_at`), but there is **no
  `Ceased`/CESSATION event** and **no View lifecycle `CLOSED` state** distinct from `SUPERSEDED` —
  confirmed absent from `src/menhir`.
- Endings can only be encoded as magic scalar values (`0`, `"none"`, `"n/a"`) — the anti-pattern this
  plan exists to prevent.

## Promotion criteria (default → representable)

- **supported-by-spike** when a `Ceased(subject, asserted_at)` event + one fold rule produces an
  *ended* View state (kept, provenance-linked), and a View lifecycle state **CLOSED** exists,
  **distinct from SUPERSEDED**.
- **Falsifier (demand gate):** count LME knowledge-update questions (and real agent transcripts) whose
  gold answer requires a **retraction** rather than a new value. If ~zero, archive until demand.

## Path (how to get there)

1. **Add the `Ceased(subject, asserted_at)` event type** — one type in the event schema.
2. **Fold rule:** a tombstone supersedes the current version with an *ended* state, kept and
   provenance-linked — reuses `_write_version` supersession + recall's existing non-current exclusion,
   so ended state falls out of the machinery already present.
3. **Add View lifecycle `CLOSED`** (via CESSATION or the absence rule), plus a `superseded_reason` enum
   to split "the world changed" (sold a bike) from "we were wrong" (misperceived) — the two meanings
   `expired_at` currently conflates (Doc 17 arch-critique #3).
4. **Perception gate:** require an explicit cessation statement (abstain otherwise) — wire through the
   span-grounded verifier (GAP #6) so "stopped being true" is not confused with "stopped being
   mentioned".

## Non-goals

- Do not encode endings as magic values in scalar slots.
- Do not infer cessation from mere absence of mention (only from an explicit, span-grounded statement).

## Risks

- **Perception ambiguity:** "stopped being true" vs "stopped being mentioned" — mitigated by requiring
  an explicit cessation statement and gating it with span verification.

## Source

Doc §A.4 (tombstones / AGM contraction) + forensic-admissibility §2.5 (continuance presumption +
terminating instruments). "Removes: the future temptation to encode endings as magic values."
