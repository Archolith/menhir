# Handoff: Resolve the Real Ingest Failure Before Any More Downstream Composition Work

> **Archived 2026-08-11.** Combined extraction fixed the failure and the isolated production replay
> verified the current-value result and clean namespace teardown.

Status: NEW SESSION SCOPE. Written 2026-07-16, at the point where the prior session recognized it
had spent an entire day building and validating downstream shadow-mode context-composition
machinery (Extraction Lab Phases 1-5, Stage 1 shadow composition, 3 candidate-selection/judge
validation experiments) without ever confirming the actual production bug that motivated all of
it is any closer to fixed. It is not. This doc exists to redirect the next session's effort back
to the root problem.

## Explicit scope for this session

**Work on fixing real production extraction — the "suburbs" failure below — not on continuing the
context-composition rollout (Stages 2-4 of
`.agent/plans/menhir-context-composition-production-integration.md`), and not on running more
shadow-mode candidate-selection/judge validation labs (`shadow_semantic_similarity_lab.py`,
`shadow_llm_judge_lab.py`, `shadow_contrastive_judge_lab.py`).** Those are all downstream of an
assumption — "once we know which prior context to show the extractor, extraction will correctly
use it" — that has never been tested, because the extractor still cannot reliably capture a new
proposition from the CURRENT message in the first place. Fix that first. The context-composition
work is not wasted (it will matter once real extraction is trustworthy), but it is not this
session's job to extend it further.

## The confirmed-live bug (verified directly against the real graph, 2026-07-16, same session)

Namespace `lme-830ce83f` (Rachel/Housing, `menhir-lme-neo4j`, bolt://localhost:7689) already has
**29 real episodes** from the original LME benchmark ingest. Six real ingest calls into this
namespace all contained the literal sentence *"Rachel actually just moved back to the suburbs
again."* — five from today's Stage 1 smoke-test runs (15:43–15:55 UTC), one from the original
2026-07-15 benchmark ingest. **In every single one, extraction captured only `Rachel`** (one run
also captured `Miami Beach`/`user` from the fuller original message). **`suburbs` has never once
been captured — not as an entity, not as a fact-edge — in any real ingest, including the ones run
today, after all of today's shadow-mode work.**

Verification queries (re-run these first to confirm the bug is still live before doing anything
else):

```python
from neo4j import GraphDatabase
driver = GraphDatabase.driver('bolt://localhost:7689', auth=('neo4j', 'lmedata123'))
with driver.session() as session:
    # Entities mentioning "suburb" -- expect 0
    session.run("""MATCH (n:Entity) WHERE n.group_id = $ns AND (
        toLower(coalesce(n.name,'')) CONTAINS 'suburb' OR
        toLower(coalesce(n.summary,'')) CONTAINS 'suburb')
        RETURN n.uuid, n.name""", ns='lme-830ce83f')

    # Fact-edges mentioning "suburb" -- expect 0
    session.run("""MATCH (n:Entity)-[r:RELATES_TO]-(m:Entity)
        WHERE n.group_id = $ns AND toLower(coalesce(r.fact,'')) CONTAINS 'suburb'
        RETURN r.fact""", ns='lme-830ce83f')

    # What DID get extracted from each real episode containing "suburb"
    session.run("""MATCH (e:Episodic)-[:MENTIONS]->(n:Entity)
        WHERE e.group_id = $ns AND toLower(coalesce(e.content,'')) CONTAINS 'suburb'
        RETURN e.uuid, collect(DISTINCT n.name)""", ns='lme-830ce83f')
```

## Root cause, as previously diagnosed — and one open nuance found just now, not yet resolved

`.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md` (original RCA) + Phase 3 of
`.agent/archive/plans/menhir-extraction-context-ablation-handoff.md` (the controlled A/B trace). Summary
of the existing diagnosis: `graphiti_core.search.search_utils.RELEVANT_SCHEMA_LIMIT = 10` caps how
many prior episodes ever reach the extraction LLM call; with the real namespace at 29 episodes,
this cap is in play. An isolated trace with 1 prior episode of context correctly extracted 5
entities including "suburbs"; with 0 prior episodes it extracted only "user" — attributed to the
extraction prompt's own "when in doubt, do NOT extract" instruction dropping entity re-mentions
once establishing context ages out of the window. **Phase 3 tested "just raise the limit" directly
(130 real-window trials) and found it chaotic/non-monotonic — ruled out as a direct fix.**

**Open nuance found during this session's direct verification, not yet reconciled with the above:**
"Rachel" is `scope: SESSION` in the real graph (checked just now), not `PERSISTENT` — so she does
NOT match the "known PERSISTENT entity" hint mechanism referenced in
`extraction_lab.py`'s `_lookup_known_entities` (which explicitly scopes to `PERSISTENT`). Yet
Rachel is reliably extracted in every one of the 6 real runs, while "suburbs" never is. This means
either (a) Rachel survives purely because she's a syntactically obvious person-name the model
extracts regardless of window state, and "suburbs" is failing for a *different or additional*
reason than pure window-position (e.g. the model just doesn't reliably treat "moved back to the
suburbs" as an extractable attribute/proposition at all, independent of RELEVANT_SCHEMA_LIMIT), or
(b) something else about real production's extraction prompt/config differs from the isolated
Phase 3 trace in a way not yet accounted for. **This needs fresh, direct investigation — do not
assume the original RCA's mechanism is the complete explanation for what's happening in today's
real runs specifically.** A good first step: trace one real `add_episode` call for this exact
message with full prompt logging (what the extraction LLM actually receives and returns), the same
method Phase 3 used, but against the *current* real namespace state (29 episodes) rather than a
synthetic 0/1-episode simulation.

## What has NOT been tried yet (real fix candidates, none implemented)

1. **Re-verify the mechanism fresh** (see nuance above) before picking a fix — don't assume Phase
   3's explanation transfers unchanged to today's real namespace state.
2. **A graphiti-core config override or patch** to raise/bypass `RELEVANT_SCHEMA_LIMIT`
   selectively — Phase 3 ruled out a blanket raise, but a *targeted* rule (e.g. always include
   entities/attributes mentioned in the CURRENT message body itself, regardless of window
   position, distinct from raising the window for ALL prior-episode context) has not been tested
   and is a narrower, more defensible claim than "just raise the limit."
3. **Directly patch the extraction prompt's conservatism** ("when in doubt, do NOT extract")
   specifically for content in the current message being processed, not prior episodes — never
   attempted.
4. **A recency-independent "already-known entity" hint**, scoped correctly this time (the existing
   `_lookup_known_entities`-style probe only checks `PERSISTENT` scope, which — per the nuance
   above — doesn't even explain why Rachel survives; a fix candidate here needs its own scope
   analysis, not a copy-paste of the lab probe's filter).
5. **Do not treat Stage 1 shadow composition as a fix for this.** It answers a different question
   ("given a value update happened, what's the correct prior context to show as reference") — not
   "why does the extractor fail to propose `suburbs` as a new fact about Rachel at all." Confirm
   which of these two problems is actually blocking "suburbs" specifically before reusing any of
   that machinery — it may not apply here.

## Where the related research lives (context, not required reading before starting)

- `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md` — original RCA
- `.agent/archive/plans/menhir-extraction-context-ablation-handoff.md` — Phase 1-5, the full downstream
  investigation (candidate selection given correct metadata — assumes extraction already
  succeeded, which is exactly the assumption this handoff is questioning)
- `.agent/plans/menhir-context-composition-production-integration.md` — the 4-stage rollout plan
  for wiring Stage 1's shadow composition into production; explicitly paused pending more
  validation, and irrelevant to fixing the extraction failure itself
- `.agent/reviews/menhir-shadow-context-composition-facet-instability-2026-07-16.md`,
  `menhir-shadow-semantic-similarity-lab-2026-07-16.md`, `menhir-shadow-llm-judge-lab-2026-07-16.md`,
  `menhir-shadow-contrastive-judge-lab-2026-07-16.md` — the 4 downstream validation experiments
  run today, all shadow-mode/observe-only, none of which touch real extraction

## Definition of done for this session

Not "shadow composition validated further." The bar is: re-run the verification queries above
against `lme-830ce83f` (or a fresh test namespace) after a real fix, and see `suburbs` actually
appear as a captured entity or fact-edge from a real `add_episode` call — the same concrete,
falsifiable check used to confirm the bug is still live today.
