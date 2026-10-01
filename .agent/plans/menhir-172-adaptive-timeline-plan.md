---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- #172 adaptive brief timeline: order by instant, append only for a real history

Project Scope: `Archolith/menhir`, worktree `C:/Users/thron/IdeaProjects/.agent/worktrees/menhir-172-timeline`,
branch `feat/172-adaptive-timeline`, stacked on `fix/172-context-packing` (`4fa53916`, PR #211).
Nothing outside this worktree.

## Why (measured, #172 comments 5933508631 / 5934663456)
- On LongMemEval, every timeline's question-relevant lines share one date. The timeline orders nothing
  and can show a stale fact as `(current)`.
- On AMA, facts DO carry real per-step histories (one minute per step, all on 2026-01-01; 988 of 4,673
  edges invalidated). But `domain/brief_builder.py::_timeline_lines` sorts and de-duplicates by DAY
  (`_day()` = `valid_at[:10]`), so a same-day history:
  - comes out in arbitrary order;
  - collapses repeated facts from different steps;
  - labels every line `[2026-01-01]`.

## Behavior (contract)
1. **Order by instant.** In `_timeline_lines`, sort events by the full `valid_at` instant, not `_day()`.
   - Parse ISO-8601 instants, accepting a trailing `Z` and an optional `[Zone]` suffix such as
     `2026-01-01T00:01:00Z[UTC]`.
   - Unparseable values sort as their raw string after all parseable ones, never raising.
   - Ties keep their current relative order (stable sort).
   - The dedup key becomes `(instant, text)` instead of `(day, text)`.
2. **Adaptive label precision.**
   - If no two DISTINCT instants in the timeline fall on the same calendar day (UTC), labels stay
     `[YYYY-MM-DD]`. Today's output is byte-identical for that case.
   - Otherwise every line is labelled `[YYYY-MM-DD HH:MM]` (UTC; seconds dropped). If two distinct
     instants share the same minute, use `[YYYY-MM-DD HH:MM:SS]`.
   - `superseded until <x>` uses the same precision as the line labels.
3. **Long-running gate.**
   - `build_timeline_bundle(memories, *, min_points: int = 1)` returns `None` when the number of
     DISTINCT `valid_at` instants among the lines it would render is `< min_points`. `min_points=1`
     preserves today's behavior for direct callers.
   - `EvidenceBundle` is unchanged.
4. **Wiring.**
   - `ContextBuilderService` gains `brief_min_timeline_points: int = 3`, passed to
     `build_timeline_bundle(memories, min_points=self.brief_min_timeline_points)` at the existing call
     site. The gate is `not fail_closed and self.brief_builder_enabled and not truncated`, unchanged.
   - Settings: `frontier_brief_min_timeline_points: int = 3`, env `MENHIR_FRONTIER_BRIEF_MIN_TIMELINE_POINTS`.
     Parse it with `_parse_int` like `frontier_source_memory_limit`. Values `< 1` clamp to 1.
   - Pass it in BOTH `src/menhir/core/bootstrap.py` (~line 240) and `src/menhir/cli/bootstrap.py`
     (~line 70), next to `brief_builder_enabled`.
   - `.env.example`: one commented line under `MENHIR_FRONTIER_BRIEF_BUILDER` (~line 268).
5. **Do not change:**
   - the brief flag default (stays off);
   - `build_bundles`;
   - `render_bundles`;
   - the packing loop;
   - the source-memory block;
   - recall code;
   - `_MAX_LINE_CHARS` / `_clip`;
   - lifecycle-note text.

## Tests (new file `tests/test_adaptive_timeline_172.py`; do NOT edit existing tests)
Build `ScoredMemory` with `temporal_facts=(TemporalFact(...),)`. Copy the positional field order from
`tests/test_context_builder.py::test_brief_lifecycle_note_survives_long_fact_clipping`.
- **Same-day multi-step history** (valid_at `2026-01-01T00:01:00Z`, `...00:05:00Z`, `...00:03:00Z`
  given in that order):
  - lines come out in instant order 00:01, 00:03, 00:05;
  - labels are `[2026-01-01 00:01]` etc.;
  - the same fact text at two different instants yields two lines.
- **Distinct-day history:** labels stay `[YYYY-MM-DD]`, and output equals what `main` produces for the
  same input. Hard-code the expected lines.
- **Same-minute distinct instants:** labels use seconds.
- **`superseded until`** follows the same precision (a same-day case shows `HH:MM`).
- **`[UTC]` suffix and an unparseable valid_at:** no exception; the unparseable one sorts last.
- **Gate:**
  - `build_timeline_bundle(mems, min_points=3)` is `None` with 2 distinct instants and not `None`
    with 3;
  - the default `min_points=1` returns a bundle for a single dated fact.
- **ContextBuilderService** (`brief_builder_enabled=True`, generous budget; heuristic mode patched as
  in `tests/test_context_packing_172.py`):
  - 2 distinct instants means no `=== Timeline ===`;
  - 3 means it is present;
  - `brief_min_timeline_points=1` restores it for 2.
- **Settings:** `MENHIR_FRONTIER_BRIEF_MIN_TIMELINE_POINTS=5` parses to 5; `0` clamps to 1; the default
  is 3.

## Docs
`.agent/CHANGELOG.md`: one dated entry (2026-10-01) for #172, covering instant ordering, adaptive
labels, the long-running gate (default 3) and the env var.

## Test policy
- Executor runs:
  `uv run pytest -q tests/test_adaptive_timeline_172.py tests/test_context_builder.py tests/test_context_packing_172.py`
  plus the settings tests that cover `frontier_` parsing (grep `frontier_brief_builder|MENHIR_FRONTIER_` in tests).
- `uv run ruff check` on changed Python files.
- At most once more after a fix. No full suite (the reviewer runs it once). No commit, no push.

## Non-goals
- No relevance filtering of timeline lines.
- No change to how `invalid_at` is produced (the ingest-time "superseded until" on LME is an
  extraction issue).
- No v2 "was/now" rendering.
- No default flip.
