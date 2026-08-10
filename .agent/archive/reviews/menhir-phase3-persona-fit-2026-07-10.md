# Phase 3 personal-memory consolidation — persona fit and positioning

**Date:** 2026-07-10
**Companion to:** `menhir-phase3-live-operator-run-2026-07-10.md` (M2 rollout evidence)
**Status:** analysis + recommendation (no code decision committed yet)

## What prompted this

The M2 live run + a full Views inventory of the production graph surfaced a structural
observation about who Phase 3 is actually for.

## Observation: zero genuine personal Views exist in real use

Current Views in the production graph: **35** (157 incl. superseded history), across 4 namespaces:

| Namespace | Current Views | Nature |
|---|---|---|
| `agent-experience` | 24 | System telemetry (QuantState agent-memory counters: enrichment/scheduler failure counts). Legit. |
| `IdeaProjects` | 6 | 3 `user::` Views (artifacts, see below) + 3 `perception::` telemetry receipts. |
| `agent-status` | 3 | System config Views (`api_port`, `experience_counter_enabled`, `structure_watcher_enabled`). Legit. |
| `phase3-scn-711bede9-ambiguous-correction` | 2 | archolith-bench phase3 scenario residue. |

Of the **5** `subject=user` ("personal memory") Views, **all 5 are artifacts**:

- `IdeaProjects` `bike_owned=125`, `bike_spend=125`, `movies=20` — the Phase 3 consumer extracted the
  **quoted example sentences** ("I have 25 movies on my watch list.", "I bought one bike for $50 and
  another for $75.", "Actually it is 20, not 25.") out of a **real 2026-07-07 planning prompt** that
  was discussing how to test Phase 3. It folded quoted examples into durable personal Views.
- `phase3-scn-...` `books=25`, `movies=25` — leftover from a bench test scenario namespace.

There are **no genuine personal Views** from real usage. The 27 legitimate Views are all system
telemetry/config, not "personal memory consolidation" output.

## Why: the feature targets a different persona than the current user

Phase 3 extracts **first-person personal measures** — counts, amounts, possessions, spend, preferences
("I have N X", "I bought X for $Y", "I spend $Z/week"). That is a **personal-assistant / chatbot**
capability. On a coding workspace the user's turns are coding instructions ("fix this bug", "run the
smoke", "what's next for MVP"), which contain no personal measures, so the precision-first consumer
correctly commits nothing. The only time it fired on real input, it fired on a **quoted example inside
a meta-prompt** — a false positive, not the use-case.

This is not a defect in the mechanism. The fold, self-consistency gate, correction/supersession, and
"no wrong current-state write" invariant all work (proven live: SUM=125, movies 25->20). It is a
**persona-fit mismatch**: precision-first + coding-workspace content = correct silence.

## Two readings

1. **"Better utilized as a chatbot" — yes.** For a personal-assistant user who states personal facts
   (habit tracking, finances, collections, preferences), Phase 3 materializes real, valuable Views.
   If menhir's audience includes "memory for a personal chat agent", M2's yield lands there and the
   feature is doing its job.
2. **"Should be adjusted" — only if the target user is a coder.** The valuable "measures" for coding
   work are not personal but **project/work facts**: passing-test counts, budgets, defect counts
   ("2,003 tests pass", "2,679 bad merges", "budget $X"). The extractor's measure taxonomy is scoped
   to personal possessions/spend, so it cannot capture those. Re-targeting to project measures is a
   **taxonomy redesign**, not a tweak.

## Recommendation

For the stated MVP ("trusted during real coding work"):

- **Do not over-invest in Phase 3 yield for the coding MVP.** Its value for this MVP is the
  **mechanism + safety proof** (no wrong writes, correct fold/supersession), which is demonstrated.
  The coding-relevant memory value lives in menhir's other surfaces — semantic recall of
  decisions/bugs/preferences, the structure graph, and M3 stale-anchor detection — which fit and work.
- **Keep Phase 3 enabled** (mechanism proven, safe) but **add a quoted/hypothetical-measure guard**
  so it stops manufacturing junk Views from meta-prompts (see follow-up 1).
- **Position "personal-measure Views" as a chatbot/personal-assistant-facing feature**, and treat the
  **project-measure re-target** as a separable opportunity if menhir should earn Phase 3 value in
  coding work (see follow-up 2).

## Follow-ups

1. **Consumer guard: don't fold quoted/hypothetical measures.** The extractor/triage should not
   commit measures that appear inside quotes or example/hypothetical framing. The IdeaProjects
   `bike_*`/`movies` Views are the exact reproduction case. Precision improvement; bench it against
   this fixture. (Tracked as a todo.)
2. **Persona/positioning decision: personal-measure vs project-measure consolidation.** Decide whether
   Phase 3 stays a personal-assistant feature (chatbot audience) or gets a project-measure taxonomy to
   serve coding-agent users. Redesign-scale; not an MVP blocker. (Tracked as a todo.)

## Note on the existing artifacts

The 3 `IdeaProjects` `user::` Views (from the quoted-example leak) and the 2 `phase3-scn-*` scenario
Views remain in the graph by choice (harmless clutter; owner opted to keep them as a war story rather
than hand-delete from the production graph). They will re-fold on the next `IdeaProjects` consolidation
unless follow-up 1 lands.
