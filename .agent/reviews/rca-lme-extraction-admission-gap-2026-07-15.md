# RCA: Extraction/admission coverage gap — specific facts never captured into the graph

**Date:** 2026-07-15 (updated same day: 2 of the original 6 cases reclassified — see Revision note)
**Severity:** High — even after reclassification this is still the largest confirmed genuine-miss
category (4 of 11 verified) and, unlike the ranking gap, **no amount of retrieval tuning fixes
it**: if the fact was never extracted into a graph node, no scoring formula can rank it. The
bottleneck is upstream, in the ingest-time extraction/enrichment pipeline (what content becomes a
queryable `Entity`), not in `recall_service`/`RetrievalTuningConfig`.
**Status:** Ground truth CONFIRMED for all 4 cases (ran the verification step this RCA originally
deferred: grepped the raw LME haystack sessions directly — every fact is genuinely, plainly stated
in source). Root cause hypothesis refined with a specific, evidenced mechanism for one case
(`89527b6b`) but still not confirmed against the actual extraction/admission-gate code — that
remains the next step, not yet done.

## Revision note (same-day)

The original version of this RCA included `852ce960` (mortgage pre-approval amount) and
`2698e78f` (therapy frequency) as pure extraction gaps. Deeper investigation — reading the full
raw haystack text, not just searching for the gold-answer keyword — found both are actually
**knowledge-update cases**: the fact changes value partway through the conversation (mortgage:
$350,000 → $400,000; therapy: "every two weeks" → "every week"), and the gold answer is the
*updated* value. Both have been moved to `rca-lme-stale-fact-retention-2026-07-15.md`, which they
strengthen considerably (see that document's revision note). This RCA now covers only the 4 cases
confirmed to be simple, non-conflicting, single-statement facts that were never captured at all.

## Summary

For 4 of the 22 genuine LME misses examined, the gold-answer fact is a plain, unambiguous,
single-statement fact (no value changes elsewhere in the conversation) — confirmed present
verbatim in the raw source conversation — yet a full-namespace search of the graph's
`Entity.name`/`summary` found **nothing** related. Not "ranked too low": absent entirely, despite
adequately populated namespaces (24-48 episodes; one exception noted below).

## Evidence

### `5d3d2817` — previous occupation
- **Source (session 0, user turn):** *"I've used Trello in my previous role as a **marketing
  specialist at a small startup** and I'm familiar with its features..."*
- **Graph:** only a loosely-related ClickUp/"marketing campaign progress" mention — not the
  occupation fact itself. 24 episodes in the namespace; not sparse.
- Single, plain statement. No ambiguity, no restatement, no conflicting later value.

### `6a1eabeb` — personal-best 5K time
- **Source (session 1, user turn):** *"...I'm hoping to beat my personal best time of **25:50**
  this time around."* (Stated once, as the target to beat; later restatements in the same session
  drop the number but don't change it — this is the user referring back to the same fixed value,
  not an update to a new one.)
- **Graph:** zero hits for "25," "personal best," or "5k" anywhere in the namespace (48 episodes).

### `89527b6b` — Plesiosaur's body color (children's book, image description)
- **Source (session 0, one long assistant turn):** *"The Plesiosaur has a **blue scaly body**, and
  its eyes are fixed on something in the distance..."*
- **Graph:** only 4 episodes total for this namespace — sparsest in the entire sample. Confirms the
  Plesiosaur is *featured in the book*, but no color/visual attribute survived.
- **Mechanism identified (not just hypothesized) for this specific case:** the source turn is one
  very long assistant message describing **four different dinosaurs in parallel structure**, each
  with its own `::[Name] Image::` block and its own body-color detail — T-Rex: green scaly,
  Pterodactyl: green scaly, Plesiosaur: **blue** scaly, Triceratops: (not checked). This is a
  dense, multi-entity turn where four near-identical-structure facts (dinosaur → color) compete
  within one episode. The specific per-entity attribute for the *third* of four parallel entities
  was lost, while topic-level content (Plesiosaur is *in* the book) survived. This is a concrete,
  checkable hypothesis for *why* density causes loss, not just an observation that it did.

### `118b2229` — daily commute duration
- **Source (session 0, user turn):** *"I've been listening to audiobooks during my daily commute,
  **which takes 45 minutes each way**."*
- **Graph:** entities about audiobook *preferences during commutes* (topically adjacent, shares the
  word "commute") but not the duration fact. 24 episodes; not sparse.

All 4 facts are confirmed present, plainly and unambiguously, in the raw source conversation —
this is not a dataset issue. All 4 also share a structural trait: **a specific, low-salience
numeric or descriptive detail** (a job title, a race time, a color, a duration) embedded inside a
turn that is otherwise about something else (Trello features, running tips, a children's story, or
audiobook recommendations) or is part of a set of parallel near-duplicate facts.

## Root cause hypothesis (refined, one case mechanism-confirmed, still not code-verified)

**Working hypothesis, now better evidenced than the original two-hypothesis draft:** extraction
favors the *topical/explanatory content* of a turn (what the assistant elaborates on, or what the
conversation is "about") over **specific, low-salience factual details stated in passing** — a
name, number, color, or duration mentioned once, inside a turn whose main subject is something
else. `89527b6b` makes this concrete: when several structurally-similar facts compete within one
dense turn (4 dinosaurs, 4 colors), only some survive — consistent with a summarization/compression
step that keeps the narrative shape but drops parallel per-entity specifics.

Still not confirmed: whether this happens at the **LLM extraction step itself** (the fact is never
proposed as a candidate entity/fact) or at a downstream **admission gate** (extracted but rejected
by a sharpness/salience filter — see `archolith-bench/scripts/longmemeval/README.md`'s note that
consolidation "deletes low-sharpness one-off facts," though consolidation itself is off in
benchmark mode; if a similar filter exists earlier in the pipeline, it would explain this without
touching consolidation). Distinguishing these requires reading the actual extraction/admission code
path, which has not been done in this investigation.

## Recommended fix direction (not implemented)

1. **Trace one case end-to-end through the extraction pipeline.** `89527b6b` is now the strongest
   candidate (not `852ce960`, which moved to the other RCA) — it has the clearest, most specific
   mechanism hypothesis (dense multi-entity turn) and the smallest namespace (4 episodes — easiest
   to trace exhaustively). Confirm: did the Plesiosaur-color fact ever reach the extraction LLM
   call's output at all, or was it proposed and then dropped by a later filter?
2. Once root-caused: if extraction itself never proposes it, the fix is in the extraction
   prompt/schema (e.g. explicit instruction to extract per-entity attributes even in dense/parallel
   structures, or chunking long turns before extraction rather than processing them whole). If an
   admission gate rejects it post-extraction, the fix is a threshold/heuristic adjustment for
   short, specific, factual statements — a different, narrower change.
3. Consider whether turn length/density itself is a signal worth tracking — if long, multi-topic
   turns are systematically lossier, that's a distinct, testable, and independently fixable
   observation (e.g. chunk extraction by paragraph/section rather than whole-turn) regardless of
   what the deeper LLM-extraction-vs-admission-gate root cause turns out to be.

## Verification plan

- Ground truth: **DONE** — all 4 facts confirmed present verbatim in raw haystack sessions (see
  Evidence above; this was the deferred step from the original RCA draft).
- Trace `89527b6b`'s ingest through the extraction/admission code path with tracing/logging enabled
  for that one namespace (only 4 episodes — small enough to inspect exhaustively), specifically
  checking whether all 4 dinosaurs' colors were proposed by extraction and only some admitted, or
  whether extraction itself only surfaced some of them.
- After a fix candidate exists: re-run these 4 (at minimum) through a fresh ingest and confirm the
  facts are now present as queryable entities, using this RCA's same keyword-search method.

## Related

- `.agent/reviews/rca-lme-retrieval-ranking-gap-2026-07-15.md` — the other major genuine-miss
  category (fact exists but ranks low); distinguishing required direct graph inspection per
  question since both categories present identically from outside (`/api/recall` returns nothing
  useful).
- `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md` — where `852ce960` and `2698e78f`
  moved to; both are now part of a confirmed n=3 knowledge-update pattern instead of anecdotal n=1.
- `archolith-bench/scripts/longmemeval/results/lme-recall-lab-investigate/investigate-2026-07-15.md`
  — full per-question judge output.
