# recall_timeline phase A -- review fixes (binding; do exactly these, nothing else)

**F1. Cypher has no tuple comparison.** `src/menhir/infrastructure/memory_queries.py::timeline_page`,
around lines 503-520, uses `(a, b, c) > (x, y, z)`.

Replace both the `after` and `before` predicates with the explicit lexicographic form. With
`V = n.valid_at`, `C = coalesce(n.created_at, datetime($epoch))`,
`KV = datetime($after_valid_at)`, `KC = coalesce(datetime($after_created_at), datetime($epoch))` and
`KU = $after_uuid`:

```
(V > KV OR (V = KV AND (C > KC OR (C = KC AND n.uuid > KU))))
```

`before` is the same with `<` and the `$before_*` params. Keep everything else in the method unchanged.

**F2. `subject` is a thread filter, not a start mode.** In
`src/menhir/services/timeline_service.py::run_recall_timeline`:
- Remove the `if subject is not None and starts: raise ValueError("subject must be the only starting point")`
  check (around line 265).
- `subject` combines with every mode (`at`, window, `around`, `cursor`, `query`) and restricts that mode
  to the subject thread. `subject` with no other start still means "latest page of the subject thread".
- Exactly one of `at` / window / `around` / `cursor` / `query` remains the rule when any is given.
- With `cursor`, the decoded cursor's `s` must match the resolved `subject_uuid`; `decode_cursor`
  already enforces that, so resolve `subject` BEFORE decoding.

**F3. No double reversal and no string sort.** `timeline_page` already returns ascending rows,
including for `before`.
- Replace `page_rows = list(reversed(before_rows)) + after_rows`, the anchor `append` and
  `page_rows.sort(key=_row_key)` (around lines 412-415) with:
  `page_rows = before_rows + ([anchor_row] if anchor_row is not None else []) + after_rows`.
- Do not sort anywhere in Python. Neo4j `toString` of datetimes is not lexicographically ordered.
- `prev_cursor`/`next_cursor` stay the first/last row keys.

**F4. Facts query must start from the page's episodes.** Replace the body of `timeline_facts` with an
anchored query:

```
UNWIND $episode_uuids AS episode_uuid
MATCH (ep:Episodic {uuid: episode_uuid})-[:MENTIONS]->(:Entity)-[r:RELATES_TO]-(:Entity)
WHERE episode_uuid IN coalesce(r.episodes, []) AND <tenant_scope_cypher("r")>
WITH DISTINCT episode_uuid, r
ORDER BY r.valid_at
WITH episode_uuid, collect({fact:..., valid_at:..., invalid_at:..., expired_at:...})[0..20] AS facts
RETURN episode_uuid, facts
```

Keep the same params, return shape and docstring intent.

**F5. Tests.** In `tests/test_recall_timeline.py`:
- Fix the stub's `timeline_page` (around line 285) to return `before` rows ASCENDING, as the real
  repository does.
- Add tests for:
  - (a) the query string contains no `) > (` / `) < (` tuple comparison and does contain the explicit
    OR-form for both directions;
  - (b) `subject` + `cursor` pages a subject thread (the cursor issued for a subject thread is accepted
    with the same subject, and rejected without it);
  - (c) `subject` + `at` and `subject` + `around` are accepted;
  - (d) page order with string timestamps that would misorder lexicographically, for example
    `2026-01-01T00:01Z` and `2026-01-01T00:01:30Z`, is preserved exactly as the stub returns it;
  - (e) the `timeline_facts` query string starts from `(ep:Episodic {uuid: episode_uuid})`.
- Update any existing test in this new file that asserted the removed "subject must be the only
  starting point" behavior.

**Test policy:** `uv run pytest -q tests/test_recall_timeline.py tests/test_cf127_tenancy_scope_predicate.py tests/test_recall_history.py`
plus `uv run ruff check` on the changed files. At most once more after a fix. No commit.
