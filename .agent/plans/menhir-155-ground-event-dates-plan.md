---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- #155 ground event dates in source evidence

Project Scope: `Archolith/menhir`, worktree `C:/Users/thron/IdeaProjects/.agent/worktrees/menhir-155-event-dates`,
branch `fix/155-ground-event-dates`, base `origin/main` `1f6eb392`. Nothing outside this worktree.

## Problem (verified on current main)
`services/event_history_perception.py`: `parse_event_row` only checks that the model's optional `when`
parses as ISO (`when_parsed`, ~line 403), and `_resolve_event_valid_time` (~line 499) then prefers it and
stamps `time_basis="explicit"`. A date the source never states (e.g. `2099-01-01` for "I bought a notebook")
becomes the event's world time and is part of its identity (`domain/event_history.py` identity includes
`valid_at` + `time_basis`). A malformed `when` currently drops the WHOLE event (`malformed_when`).

## Behavior (contract)

1. **Grounding context.** `EventPerceptionProposal` gains `when_context: str = ""`: the sentence of the
   episode content containing the located span (from the last `.`, `!`, `?` or newline before
   `span_start` to the first one at/after `span_end`, inclusive of the span; whitespace-trimmed).
   `parse_event_row` fills it. It is NOT part of `source_key` or any identity.
2. **Malformed `when` no longer drops the event.** A non-parseable `when` is treated as absent
   (`proposal.when=None`) and reported through the existing `drop`-style observability as a NON-fatal
   note. Keep `parse_event_row`'s contract that `drop` is called at most once and only before returning
   None: add a separate optional `note: Callable[[str], None] | None` parameter (threaded from
   `extract_events_once`'s existing `on_drop` seam or a new `on_note`), reason `"malformed_when_ignored"`.
3. **Grounded resolution** replaces `_resolve_event_valid_time(when, episode_reference_time)` with
   `_resolve_event_valid_time(when, when_context, episode_reference_time) -> (valid_at, time_basis, note)`,
   called from `build_event_assertion`. Reuse the scalar side's deterministic date parsing; do NOT copy
   scalar expiration/temporal-disposition policy. Expose public aliases in
   `services/typed_scalar_rules.py`: `parse_source_date = _parse_source_date` and
   `same_calendar_day = _same_calendar_day` (no behavior change there), and import those.
   Rules, in order (`ref` = parsed episode reference time; all comparisons by calendar date in ref's tz):
   a. `src, unresolvable = parse_source_date(when_context, episode_reference_time)`.
   b. If `src` is a full date: if `src` is after `ref`'s date -> treat as no source date (future dates
      cannot be completed acquisitions; note `"future_source_date_ignored"`); else valid_at=`src`,
      basis `explicit` (note `"when_conflict_used_source"` when the model gave a different day; note
      None when it matched or was blank).
   c. Else if `unresolvable` (month+day stated, no year): if the model `when` has the same month and
      day, use that month/day in the most recent year such that the date is on or before `ref`'s date ->
      `explicit`; otherwise fall back to the episode reference (note `"undated_month_day_unmatched"`).
   d. Else if `when_context` contains a supported relative phrase -- `today`/`tonight` (= ref date),
      `yesterday` (= ref - 1 day), `N day(s) ago`, `N week(s) ago` (N a digit 1-60 or a word one..ten) --
      compute that date deterministically from `ref`. If the model `when` is on that same calendar day,
      or the model gave no `when`, use the computed date with basis `explicit`. If the model gave a
      different day, still use the computed date (note `"when_conflict_used_relative"`).
   e. Else (no stated date): ignore any model `when` (note `"ungrounded_when_ignored"` if one was given)
      and fall back.
   Fallback = today's behavior: episode reference -> `episode_reference`; unparseable reference ->
   `(None, None)` abstain (`no_valid_time`). `learned_at` is never used.
   Explicit dates resolve to 00:00 in ref's timezone (or UTC), as `parse_source_date` does.
4. `build_event_assertion` keeps its signature; `EventAssertionBuildResult` gains `note: str | None`
   (the resolution note) so callers can audit it. Wire the note into the existing event perception
   audit/drop accounting in the caller (`event_consolidation.py`) if a counter dict exists there;
   otherwise just expose it.
5. **Replay/version.** Bump the default `personal_memory_event_history_perceiver_version` in
   `config/settings_model.py` from `"v1"` to `"v2"` so re-perception supersedes v1 assertions via
   strict `perceiver_rank` (verify `perceiver_rank("v2") > perceiver_rank("v1")` in
   `domain/typed_assertion.py`). Trace whether a version change causes existing episodes to be
   re-perceived (watermark/scheduler in `scheduler_tasks.py` / `event_consolidation.py`) and document
   the actual behavior in the CHANGELOG (if not automatic, say existing graphs keep v1 events until a
   replay). Update `.env.example` if it shows the default.
6. Prompt text (`EVENT_SYSTEM_PROMPT`) stays unchanged.

## Tests (new file `tests/test_event_date_grounding_155.py`; do NOT edit existing test assertions)
- issue repro: source "I bought a notebook", ref 2026-09-23, model when 2099-01-01 -> valid_at = ref,
  basis `episode_reference`, note `ungrounded_when_ignored`; blank when -> same, note None.
- explicit full date in sentence ("On 2026-07-18 I bought a notebook", "I bought a notebook on July 18,
  2026"): matching model when -> that date, `explicit`; different model when -> source date +
  `when_conflict_used_source`; no model when -> source date, `explicit`.
- date stated in a DIFFERENT sentence of the same episode -> not used (fallback).
- future source date (after ref) -> ignored, fallback, `future_source_date_ignored`.
- month+day without year: matching model month/day -> most recent year <= ref (e.g. ref 2026-09-23,
  "on July 18" -> 2026-07-18; "on December 2" -> 2025-12-02); mismatched -> fallback note.
- relative: "yesterday" -> ref-1 `explicit`; "3 days ago", "two weeks ago"; conflicting model when ->
  computed date + note; "last week"/"recently" (unsupported) -> fallback.
- malformed model when (e.g. "notadate") -> event still produced, `malformed_when_ignored` note, no drop.
- unparseable episode reference + no source date -> `no_valid_time` (unchanged abstention).
- persistence: the built assertion's valid_at/time_basis survive into the repository write params
  (exercise `typed_event_repository` param building with the existing test doubles, or the consolidation
  path with its stub, whichever the existing tests use -- find them: grep tests for
  `build_event_assertion` / `TypedEventRepository`).
- version: settings default is "v2" and `perceiver_rank("v2") > perceiver_rank("v1")`.

## Docs
`.agent/CHANGELOG.md` dated entry (behavior + replay note); mention #155.

## Test policy
Executor: `uv run pytest -q tests/test_event_date_grounding_155.py` plus every existing test file that
imports `event_history_perception`, `event_consolidation`, `typed_event_repository` or `typed_scalar_rules`
(grep), and `uv run ruff check` on changed files; at most once more after a fix. No full suite (reviewer
runs it once). If an existing test fails because it encoded the old ungrounded-explicit behavior, STOP
and report it instead of editing it.

## Non-goals
No change to scalar behavior, prompts, identity/source_key, TIME_BASES, recall, or Graphiti.
