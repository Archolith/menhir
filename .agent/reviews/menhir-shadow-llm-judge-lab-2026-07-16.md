# Shadow Context Composition — LLM Judge Validation Experiment Results

**Date:** 2026-07-16
**Scope:** third step in the response chain following the semantic-similarity lab
(`.agent/reviews/menhir-shadow-semantic-similarity-lab-2026-07-16.md`), which found similarity
separates unrelated content cleanly but no single threshold achieves both perfect precision and
recall — a real `wrong_state_family` decoy ("boundaries", `lme-2698e78f`) scored 0.5794, higher
than every true positive (max 0.4537).
**Proposal tested:** per direct instruction, use the LLM judge (not similarity thresholds) as the
final decision-maker on plausible candidates. Test it against all 21 Phase-4b fixtures, measuring
whether it preserves true positives while rejecting the hard negative.
**Status:** offline validation only, same as the similarity lab. Nothing wired into production.
**Headline result: not yet — the naive binary judge failed twice, in the opposite failure mode
similarity had.** Where similarity over-accepted the hard negative, the judge under-accepted real
positives while *still* accepting the same hard negative in 2 of 3 attempts. This is a genuine,
informative negative result, reported honestly rather than iterated silently until something
looked good.

## Method

New `src/menhir/explorer/shadow_llm_judge_lab.py` (prompt + response parsing + scoring) and
`scripts/run_shadow_llm_judge_lab.py` (real `gpt-4o-mini` calls). Reuses
`shadow_semantic_similarity_lab.build_similarity_lab_rows()` directly — the same 69
(message, candidate) rows, same categories, same ground truth as the similarity lab, so results
are comparable row-for-row. Judge output is binary (`MATCH`/`NO_MATCH`), scored via confusion
matrix (TP/FP/TN/FN) rather than a threshold sweep.

## Attempt 1 — "is this the same fact" framing

```python
JUDGE_SYSTEM_PROMPT = """You are verifying whether a CANDIDATE FACT from a memory graph is
actually the specific fact/state that a CURRENT MESSAGE is about.
...
Answer MATCH if... the SAME specific fact/state the message is actually about or referencing --
not a nearby, related, or merely topically-adjacent one.
Answer with exactly one word: MATCH or NO_MATCH."""
```

**Real result (69 rows): TP=0, FP=2, TN=55, FN=12 — precision=0.000, recall=0.000, f1=0.000.**
Every single true positive was rejected. Root cause, confirmed by inspecting the raw fixture text
directly:

- Message (`lme-830ce83f`): *"...Rachel actually just moved back to the **suburbs** again..."*
- Correct candidate (`c1`): *"Rachel previously moved to an apartment in **Chicago**."*

These fixtures are knowledge-*update* scenarios — the correct candidate is deliberately the PRIOR
value the message is updating, not a restatement of the message's new value. "Is this the same
fact" is the wrong question; a judge asked that will correctly notice Chicago ≠ suburbs and reject
every real positive. This was a genuine design mistake in the prompt, not a limitation of the
underlying concept — caught by running the real experiment rather than assumed away.

Despite being maximally strict on positives, this framing still **wrongly accepted the boundaries
decoy twice** (`lme-2698e78f_2_wrong_state_family` / `lme-2698e78f_5_near_neighbor_decoys_only`,
both `c2`) — the one case that most needed rejecting.

## Attempt 2 — "same slot, value may differ" framing

Rewrote the prompt to explicitly instruct the model that the candidate does not need to match the
message's value — only the same real-world topic/state/slot (e.g. "current residence," not
"housing preferences"), with the exact Rachel/Chicago/suburbs case as a worked example inside the
prompt itself. Full prompt in `shadow_llm_judge_lab.py`.

**Real result (69 rows): identical to attempt 1 — TP=0, FP=2, TN=55, FN=12, same specific rows
wrong.** Verified this was not a caching/wiring bug — an isolated debug call with `max_tokens=200`
and an explicit "explain your reasoning" instruction showed the model's actual reasoning:

> *"The CURRENT MESSAGE discusses Rachel's recent move back to the suburbs, which is a specific
> state regarding her current residence. The CANDIDATE FACT, however, refers to a previous move to
> an apartment in Chicago, which is a different specific state and does not relate to her current
> living situation. NO_MATCH"*

`gpt-4o-mini` reasoned about this exactly backwards from the intended framing, even with an
explicit instruction and matching worked example: it treated differing *values* (Chicago vs.
suburbs) as differing *states*, rather than the same state with a new value. The instruction did
not override the model's default value-equality intuition.

## Attempt 3 (spot-check only, not run at full scale) — "field/slot" (git-blame) framing

Reframed again around "would updating the candidate with the message's value mean editing the same
field, like `git blame` on a record" — a more concrete metaphor than "state/slot." Tested on 3
cases only (both real positives + the boundaries hard negative), not the full 69-row set:

| Case | Result |
|---|---|
| positive (`lme-830ce83f`, Rachel/Chicago→suburbs) | **MATCH** (correct) |
| positive (`lme-2698e78f`, Dr. Smith weekly vs. biweekly) | **MATCH** (correct) |
| hard negative (`lme-2698e78f`, "boundaries" decoy) | **MATCH** (incorrect — still fooled) |

This framing fixes recall on the two spot-checked positives, but the boundaries decoy *still*
passes — the "field" framing is apparently loose enough that "discussing boundaries during
therapy" reads to the model as close enough to "session frequency" to pass. Not run at the full
69-row scale — this is a spot-check, not a validated result, and is not committed as the module's
prompt.

## Findings

**1. This is a genuinely hard discrimination task for a single binary LLM call, not a prompt-
wording nuisance.** Three real attempts, three different failure shapes: (a) reject everything
including real matches; (b) identical to (a), same model reasoning holds despite explicit
counter-instruction; (c) fix recall, but the specific adversarial case that most needs rejecting
still passes. Across all three, the boundaries decoy passed whenever the prompt was loosened
enough to accept real positives — precision and recall pulled directly against each other on
exactly this one case, in every attempt.

**2. The judge is NOT simply "similarity but smarter" on this hard case — it failed to solve what
similarity also failed to solve.** Similarity scored the boundaries decoy at 0.5794 (above every
positive). The judge, even in its best (recall-preserving) framing, also accepted it. Neither
signal alone has yet demonstrated it can separate this specific pair.

**3. Every negative category OTHER than `wrong_state_family` was rejected cleanly and consistently
across attempts 1 and 2** — `cross_family_control` (36/36), `stale` (3/3), `wrong_subject` (3/3),
`missing_metadata` (3/3), `wrong_scope` (6/6). The difficulty is narrow and specific: same-subject,
lexically-overlapping, different-specific-topic decoys — exactly the category the report predicted
would be hardest.

## Practical read

The proposed pipeline shape (temporal filter → similarity recall/ranking → LLM judge for final
decision) remains directionally right — nothing here contradicts it. But the LLM-judge stage as
specified (single binary call, "does this candidate match") has not yet been demonstrated to work,
across three real attempts. The boundaries case is proving to be a genuinely hard example, not an
edge case to wave away: it requires distinguishing "the specific therapy topic being discussed"
from "the frequency of therapy sessions" — a fine-grained state_family distinction that a cheap
model's first instinct does not reliably make regardless of how the question is framed.

## Recommendation (still not integrating anything into production)

Not yet done, in priority order:
1. **Do not commit to attempt 3's framing** — it was only spot-checked on 3 rows, not validated at
   the full 69-row scale the way attempts 1 and 2 were.
2. **Try a decomposed judge** instead of one holistic binary call: ask the model to name the
   specific state/slot for the message and the specific state/slot for the candidate as two short
   separate extractions, then compare those — closer to Stage 1's original `shadow_facet` idea, but
   as one intermediate reasoning step inside a single call rather than two independently-generated
   free-text labels compared across separate calls (which is what made the original exact-match
   approach fail). This might combine the judge's contextual reasoning with something closer to
   the label-based approach's original intent, without repeating its independent-generation flaw.
3. Only after a full 69-row run demonstrates real separation on the boundaries case specifically,
   revisit wiring any of this into `shadow_context_composition.py`.

## References

- `.agent/reviews/menhir-shadow-semantic-similarity-lab-2026-07-16.md` — the similarity experiment
  this responds to
- `.agent/reviews/menhir-shadow-context-composition-facet-instability-2026-07-16.md` — the original
  finding that started this chain
- `.agent/plans/menhir-context-composition-production-integration.md` — Stage 1 spec, cross-referenced
- `src/menhir/explorer/shadow_llm_judge_lab.py` — prompt (attempts 1→2, attempt 2 is what's
  committed), parsing, scoring
- `scripts/run_shadow_llm_judge_lab.py` — runner (real LLM calls)
- `results/shadow_llm_judge_lab.json` — full raw per-row judgments (attempt 2's real 69-row run)
- `src/menhir/explorer/extraction_lab_eligibility_fixtures.py` — reused ground-truth fixtures
