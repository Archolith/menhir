---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- #172 build_context packing: declare tiktoken, skip oversized memories

Project Scope: `Archolith/menhir`, worktree `C:/Users/thron/IdeaProjects/.agent/worktrees/menhir-172-context`,
branch `fix/172-context-packing`, base `origin/main` `1f6eb392`. Nothing outside this worktree.

## Why (measured, #172 comment 5933508631)
1. `tiktoken` is not a declared dependency, so every install runs `context_builder` in heuristic mode,
   which packs only `floor(max_tokens * 0.5)`. On the LME copy at the default 2000 tokens, 186/187
   contexts truncate (median 4 of 10 recalled memories packed). With tiktoken: 113/187, median 10.
   Measured cost: ~140 ms once per process, ~0.27 ms per 4 KB encode.
2. The ranked-memory loop in `services/context_builder.py` (`for idx, mem in enumerate(memories, start=1)`,
   ~line 440) does `truncated = True; break` at the first memory that does not fit, so one long entity
   summary drops every lower-ranked memory even when they would fit.

## Behavior (contract)
1. `pyproject.toml` `[project] dependencies`: add `"tiktoken>=0.8,<1",` with a one-line comment
   (`# build_context/hook token counting; without it budgets fall back to the halved heuristic`).
   Run `uv lock` so `uv.lock` matches. No other dependency changes.
2. `services/context_builder.py`, ranked-memory loop ONLY: at
   ```python
   if running_tokens + total_tokens > effective_budget:
       truncated = True
       break
   ```
   replace `break` with `continue` (keep `truncated = True`). The skipped memory contributes nothing
   (no line, no stale advisory, not in `memory_ids`, no tokens). Later memories that fit are packed in
   recall order. Labels keep the recall rank (`[Memory {idx}]`), so a skipped rank leaves a gap.
   Stale memory + advisory stay atomic (the combined `total_tokens` check is unchanged).
3. Do NOT change: the scalar-authority loop, the event-authority loop, the source-memory block, the
   timeline gate (`not fail_closed and self.brief_builder_enabled and not truncated`), the heuristic
   halving, `estimate_tokens`, the tiktoken import/except block, `cli/output.py`, or any recall code.
4. Update the comment directly above the loop only if it states the old stop-at-first behavior (it does
   not today; leave it otherwise).

## Tests (new file `tests/test_context_packing_172.py`; do NOT edit existing tests)
Use the helpers in `tests/test_context_builder.py` (`_mem`, `_recall_result`, `_build_service`) by
importing them, or copy minimal equivalents. Patch `menhir.services.context_builder._ESTIMATION_MODE`
and `_tiktoken_available` explicitly where the arithmetic matters, like the existing tests do.
- oversized memory at rank 2 (content far above the budget) with small memories at ranks 1, 3, 4:
  result contains `[Memory 1]`, `[Memory 3]`, `[Memory 4]`, not memory 2's content; `truncated is True`;
  `memory_count == 3`; `token_estimate <= max_tokens`.
- recall order preserved among packed memories (index of `[Memory 3]` < index of `[Memory 4]`).
- stale-anchored oversized memory is skipped together with its advisory (no orphan advisory line);
  a later stale memory that fits is packed WITH its advisory.
- all memories fit -> identical output to before (`truncated is False`, all labels present).
- fail-closed event verdict still packs no ranked memories (reuse the existing fail-closed fixture
  pattern from `tests/test_context_builder.py` if present; otherwise skip this case and say so).
- `tiktoken` is importable and `menhir.services.context_builder._ESTIMATION_MODE == "tokenizer"` in
  the test environment (pins the declared dependency).

## Docs
`.agent/CHANGELOG.md`: one dated entry (2026-10-01) mentioning #172: tiktoken declared (budget no
longer halved on standard installs) and skip-and-continue packing.

## Test policy
Executor: `uv sync` then `uv run pytest -q tests/test_context_packing_172.py tests/test_context_builder.py`
and `uv run ruff check` on changed Python files; at most once more after a fix. No full suite
(reviewer runs it once). No commit, no push.

## Non-goals
No budget/default changes, no renumbering of labels, no truncation of oversized memory content, no
changes to the timeline gate or other packing blocks.
