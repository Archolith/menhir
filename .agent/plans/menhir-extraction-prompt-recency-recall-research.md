# Menhir Recall Labs Task: Extraction Prompt Failure on Distant Knowledge Updates

**Status: Research / Prototype — SAVED, NOT ACTIVE.** Explicitly scoped to Recall Labs
experimentation only. Do not modify production ingestion prompts from this document without a
completed prompt comparison showing clear improvement (see the doc's own "Deliverables" section).

**Provenance:** authored by the user working with Codex (2026-07-15), pasted into this Claude Code
session for safekeeping. Not written or edited by Claude — saved verbatim below.

**Cross-reference (added by Claude, 2026-07-15):** this plan is a direct, well-targeted follow-on
to `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md`, which root-caused and confirmed
(via a controlled A/B code test, not just theory) the exact failure this plan analyzes: with the
Chicago-establishing episode visible, extraction correctly proposed `Rachel`/`suburbs`/etc (5
entities); with zero prior context (simulating graphiti-core's real `RELEVANT_SCHEMA_LIMIT=10`
recency window once a conversation exceeds ~10 episodes since an entity was last mentioned),
extraction dropped to 1 entity (`user` only). That RCA also ran one additional check this plan
should incorporate: **the same zero-context test was re-run with `gpt-4o-mini`** (the model Zep and
Mem0 both use for LongMemEval extraction; menhir's LME harness previously defaulted to the
cheaper/faster `gpt-4.1-nano`, changed to `gpt-4o-mini` the same day — see
`archolith-bench/scripts/longmemeval/config.sh:LME_EXTRACT_MODEL`). Result: mini extracted 3
entities under zero context (`user`, `Miami Beach`, `Rachel` — a real improvement over nano's 1) and
correctly deduped Rachel to her existing graph entity, but **still did not extract `suburbs` or any
residence-update fact** — confirming this plan's core thesis that the failure is a **prompt/
extraction-conservatism problem, not purely a model-capability problem**. A stronger model recovers
some entities but not the specific "when in doubt, do NOT extract" casualties this plan targets.
Whoever runs this plan's prompt ablation should run it against `gpt-4o-mini` (now the harness
default), not `gpt-4.1-nano`, since that's what production LME runs use going forward, and treat
the RCA's zero-context mini result as an existing partial data point rather than re-deriving it
from scratch.

---

## Original document (verbatim from here down)

## Scope

Focus only on the extraction failure demonstrated by LongMemEval case `830ce83f`.

Do not implement:

* supersession chains
* belief-family ranking
* retrieval reranking
* evidence-oracle changes
* graph schema changes

Those depend on the updated fact first being captured.

The immediate question is:

> Why does Graphiti's extractor omit valid entities and propositions when relevant conversational context is absent, and can prompt changes improve extraction recall without creating unacceptable noise?

---

# Confirmed Failure

The source conversation contains two residence states for Rachel:

```text
Session 0:
Rachel moved to Chicago.

Session 1:
Rachel actually just moved back to the suburbs again.
```

The second statement was successfully ingested as an episode, but it produced no graph representation of Rachel's updated residence.

It was not later removed.

It was never extracted.

The relevant empirical result:

| Previous context supplied      | Entities extracted from identical current message |
| ------------------------------ | ------------------------------------------------- |
| Rachel/Chicago episode visible | user, Miami Beach, Rachel, suburbs, major city    |
| No previous episodes           | user only                                          |

This suggests the current prompt and extraction model become excessively conservative when conversational context is sparse.

---

# Current Prompt Risk

The node-extraction prompt includes conservative language such as:

```text
NEVER extract abstract concepts, feelings, or generic words.
```

```text
When in doubt, do NOT extract.
```

```text
Only extract entities that are specific enough to be uniquely identifiable.
```

These rules may suppress false positives, but they also appear to suppress valid content in conversational updates.

The model may interpret:

```text
Rachel actually just moved back to the suburbs again.
```

as insufficiently self-contained because:

* `Rachel` may not appear uniquely identifiable
* `the suburbs` is not a formally named location
* `again` implies omitted history
* `moved back` implies a prior state
* the full meaning depends on conversational continuity

However, these are exactly the kinds of statements a conversational memory system must preserve.

---

# Primary Research Question

Can the extraction prompt be changed so that it:

1. extracts explicit people and objects from the current message even when identity resolution is uncertain;
2. captures concrete propositions involving relative or colloquial values;
3. preserves uncertainty for later resolution rather than omitting the content;
4. still avoids extracting meaningless abstractions and generic filler?

The desired behavior is:

> Extract first when a concrete statement is present. Resolve identity and canonical form later.

---

# Important Distinction

The current prompt appears to conflate three different decisions.

## 1. Mention detection

Was a concrete person, place, organization, object, or concept explicitly mentioned?

Example:

```text
Rachel
```

## 2. Identity resolution

Which existing graph entity does the mention refer to?

Example:

```text
Rachel from the prior conversation
```

## 3. Fact resolution

What structured proposition does the sentence assert?

Example:

```text
Rachel — moved/residence changed to — suburbs
```

An extractor should not refuse to perform mention detection merely because identity resolution is uncertain.

The prompt should explicitly separate these responsibilities.

---

# Prompt Design Principle

Replace:

```text
When in doubt, do NOT extract.
```

with behavior closer to:

```text
If the current message explicitly mentions a concrete entity or asserts a concrete fact,
extract it even when its canonical identity or exact interpretation is uncertain.

Represent uncertainty in the output rather than omitting the mention.
```

The extractor should be conservative about invention, not conservative about preserving explicit source content.

---

# Proposed Prompt Variant A: Minimal Recall Patch

This is the smallest possible prompt change.

## Goal

Determine whether the conservative language alone is causing the omission.

## Proposed instructions

```text
Extract entities explicitly mentioned in the CURRENT MESSAGE.

Do not require an entity to be globally unique before extracting it.
If a person is explicitly named, extract that person mention even if additional
context is needed to resolve which existing entity it refers to.

Concrete but non-canonical places or values should still be extracted when they
are important to the meaning of the message. Examples include:

- the suburbs
- downtown
- her old apartment
- the previous company
- a new school

Do not invent entities that are not present in the current message.

When uncertain about identity or canonical naming, extract the mention and mark
it as unresolved rather than omitting it.
```

## Specific removal or modification

Replace:

```text
When in doubt, do NOT extract.
```

with:

```text
When in doubt about whether content was explicitly stated, do not invent it.
When the content is explicit but identity resolution is uncertain, extract it
and preserve that uncertainty.
```

---

# Proposed Prompt Variant B: Mention-First Extraction

This variant makes the separation explicit.

## Proposed instructions

```text
Your task has two stages.

STAGE 1 — MENTION CAPTURE

Identify concrete entities explicitly mentioned in the CURRENT MESSAGE.

Capture explicit mentions even when:

- the entity is not globally unique;
- the mention is informal;
- the mention depends on earlier conversational context;
- the entity cannot yet be linked to an existing graph node.

Examples that should be captured:

- Rachel
- the suburbs
- her old job
- their new apartment
- the previous doctor

STAGE 2 — NORMALIZATION HINT

For each mention, provide the most reasonable normalized name and type.

If normalization is uncertain:

- preserve the original text;
- mark resolution as uncertain;
- do not omit the mention.

Never invent a person, place, or object not explicitly present in the current message.
```

## Expected advantage

This reduces pressure on the extractor to solve entity resolution during extraction.

## Expected risk

It may increase unresolved or generic entity nodes unless downstream handling is careful.

For Recall Labs, that is acceptable. Measure it rather than prematurely preventing it.

---

# Proposed Prompt Variant C: Update-Aware Extraction

This variant explicitly targets corrections and state changes.

## Proposed instructions

```text
Pay special attention to statements that update, correct, reverse, or refine
earlier information.

Update indicators include phrases such as:

- actually
- now
- no longer
- moved back
- changed to
- instead
- again
- recently
- turns out
- I was wrong
- correction

When one of these indicators appears, extract all concrete participants and the
newly asserted state from the CURRENT MESSAGE, even if the prior state is not
visible in the supplied context.

The absence of the prior state must not prevent extraction of the new state.

Example:

CURRENT MESSAGE:
"Rachel actually just moved back to the suburbs again."

Required extraction:

- Rachel: person mention
- suburbs: location or residence-area mention
- proposition: Rachel moved or resides in the suburbs

Do not require the prior Chicago statement to be visible in order to capture
the new proposition.
```

This may be especially useful for LongMemEval because many questions depend on explicit updates.

---

# Proposed Prompt Variant D: Proposition-First Extraction

The existing node extractor may be overly focused on entities.

A proposition-first prompt could ask the model to identify claims before deciding what entities to emit.

## Proposed instructions

```text
First identify every concrete factual proposition asserted by the CURRENT MESSAGE.

A concrete factual proposition describes a person, object, organization, event,
preference, possession, location, relationship, or state that could matter in a
future conversation.

Then identify the entities required to represent each proposition.

Do not omit a proposition merely because:

- one entity is informal;
- an entity requires later resolution;
- the value is relative rather than canonical;
- the proposition refers to a prior state not included in context.

Example:

"Rachel actually just moved back to the suburbs again."

Concrete proposition:

- Subject: Rachel
- Relation: moved to / currently lives in
- Object or value: the suburbs
- Update language: actually, moved back, again
```

This may help prevent a situation where the model rejects `suburbs` as an entity and consequently loses the entire fact.

---

# Proposed Prompt Variant E: Structured Uncertainty

If the output schema can be extended inside Recall Labs, test an explicit uncertainty field.

Example output:

```json
{
  "mentions": [
    {
      "text": "Rachel",
      "type": "PERSON",
      "resolution_status": "UNRESOLVED",
      "confidence": 0.98
    },
    {
      "text": "the suburbs",
      "type": "LOCATION_DESCRIPTION",
      "resolution_status": "NON_CANONICAL",
      "confidence": 0.95
    }
  ],
  "propositions": [
    {
      "subject_text": "Rachel",
      "predicate": "MOVED_TO",
      "object_text": "the suburbs",
      "is_update": true,
      "confidence": 0.96
    }
  ]
}
```

The important design principle is:

```text
uncertain identity ≠ absent fact
```

---

# Recommended First Experiment

Run a controlled prompt ablation using the exact same:

* extraction model
* current message
* previous episodes
* temperature
* output schema
* retry behavior

Only change the prompt.

Test these conditions:

1. Existing Graphiti prompt
2. Existing prompt with `When in doubt, do NOT extract` removed
3. Minimal Recall Patch
4. Mention-First Extraction
5. Update-Aware Extraction
6. Proposition-First Extraction
7. Mention-First plus Update-Aware
8. Proposition-First plus Structured Uncertainty

Run each condition multiple times if the extraction model is nondeterministic.

---

# Required Test Messages

Do not test only the Rachel fixture.

Create a small prompt-evaluation set covering several ambiguity patterns.

## Direct named update

```text
Rachel actually just moved back to the suburbs again.
```

Expected:

```text
Rachel
suburbs
Rachel moved to or currently resides in the suburbs
```

## Pronoun update

```text
She actually moved back to the suburbs again.
```

This should likely require context. Measure whether the prompt invents an identity without it.

Expected without context:

```text
unresolved female/person reference
suburbs
unresolved person moved to suburbs
```

Expected with Rachel context:

```text
Rachel moved to suburbs
```

## Informal location

```text
David is living downtown now.
```

Expected:

```text
David
downtown
David currently lives downtown
```

## Corrected monetary value

```text
Actually, I spent $400,000, not $350,000.
```

Expected:

```text
user
$400,000
possibly $350,000 as rejected prior value
current expenditure amount is $400,000
```

## Reversal

```text
Maya no longer works at Google; she joined Microsoft.
```

Expected:

```text
Maya
Google
Microsoft
Maya no longer works at Google
Maya works at Microsoft
```

## Generic non-fact

```text
Moving is really stressful and confusing.
```

Expected:

```text
no durable person/location proposition unless explicit context warrants it
```

## Unsupported implication

```text
Rachel has been packing boxes all week.
```

The extractor must not invent:

```text
Rachel moved
```

This checks that higher recall does not become speculative inference.

---

# Evaluation Criteria

Prompt variants should be evaluated at the proposition level, not only by entity count.

For every test message record:

* required mentions captured
* required proposition captured
* unsupported mentions introduced
* unsupported propositions introduced
* identity incorrectly resolved
* update language preserved
* malformed output
* token usage
* latency

Suggested metrics:

```text
mention recall
mention precision
proposition recall
proposition precision
update capture rate
unsupported inference rate
```

The most important metric is:

```text
explicit proposition recall
```

---

# Desired Behavior for `830ce83f`

Even with no previous episodes, the extractor should produce something equivalent to:

```text
Entity mention:
Rachel — PERSON

Entity or value mention:
the suburbs — LOCATION_DESCRIPTION

Proposition:
Rachel moved to / currently resides in the suburbs

Metadata:
update_language = ["actually", "moved back", "again"]
requires_prior_resolution = true
```

It does not need to know yet:

* which Rachel node is correct;
* what exact suburb is meant;
* whether this supersedes Chicago;
* the exact date of the move.

Those are later pipeline decisions.

---

# Prompt Recommendation to Test First

Start with the following combined prompt change because it directly addresses the observed failure without redesigning the schema:

```text
Extract concrete entities and factual propositions explicitly stated in the
CURRENT MESSAGE.

Do not require an entity to be globally unique before extracting it. A named
person should be extracted even when additional context is required to resolve
which existing graph entity the name refers to.

Concrete informal or relative values may be important and should be preserved.
Examples include "the suburbs," "downtown," "her old apartment," and "the
previous company."

Pay special attention to statements that update, correct, reverse, or refine
earlier information. Indicators include "actually," "now," "no longer,"
"moved back," "instead," "again," and "correction."

The absence of the prior fact from PREVIOUS MESSAGES must not prevent extraction
of the newly asserted fact from the CURRENT MESSAGE.

When identity or normalization is uncertain, preserve the original mention and
mark it for later resolution rather than omitting it.

Do not invent facts, entities, or relationships that are not explicitly
supported by the CURRENT MESSAGE.
```

This should be the first Recall Labs candidate.

---

# Questions the Worker Must Answer

1. Which exact prompt sentence causes the largest recall loss?
2. Is the problem primarily node extraction or edge/fact extraction?
3. Does extracting `Rachel` and `suburbs` reliably lead to the residence edge?
4. Does update-aware language improve other LongMemEval update cases?
5. How much precision is lost by relaxing uniqueness requirements?
6. Can uncertainty be represented without changing Graphiti's production schema?
7. Does the smaller extraction model need examples more than instructions?
8. Would a few-shot prompt outperform a longer rules-based prompt?
9. Does the behavior persist with a stronger extraction model?
10. Is the current model capable of the desired behavior consistently once correctly prompted?

---

# Deliverables

Produce:

1. The current exact extraction prompts used for nodes and facts.
2. An annotated prompt critique.
3. A reproducible `830ce83f` prompt fixture.
4. Results for each prompt variant.
5. Proposition-level precision and recall.
6. A recommended prompt patch.
7. A list of remaining failures that prompt changes cannot solve.
8. A decision on whether broader conversational context is still required after prompt improvement.

Do not modify production ingestion until the Recall Labs prompt comparison demonstrates a clear improvement.
