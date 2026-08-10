# Conflict Suggestion Remediation

Status: **planned; implementation not started**

RCA: `.agent/reviews/rca-conflict-suggestion-destructive-default-2026-08-09.md`

## Why

`list_conflicts` recommends `replace` with `keep_uuid` = oldest member on every 2+ member group,
which means "destroy the newer memory." The conflict pipeline itself deliberately picks no winner
for a confirmed contradiction — it escalates to a human — so this suggestion invents a decision the
system refuses to make, in the direction that loses corrections.

The fix is not to invert the direction. Recency alone is not authority either, and "newest wins"
would destroy the older memory just as blindly. The fix is to stop asserting a winner the system
has no grounds to pick, and to make the safe action the recommended one.

## Design stance

Three principles, in priority order:

1. **Never recommend a destructive action by default.** `keep_both` is non-destructive and
   reversible; `replace` is neither. Every automated path in the system already chooses
   `keep_both`. The operator surface should not be the sole exception.
2. **A recommendation must carry its reason.** If a suggestion cannot state why it prefers one
   member, it should not name one. Silence is better than an unfounded `remove_uuid`.
3. **Recency is a tie-break, never a justification.** It is available and cheap, which is exactly
   why it got used implicitly. Any future directional preference must rest on provenance, source
   confidence, or explicit supersession — not creation order.

## Scope

In scope:

- Replace the hardcoded `replace` suggestion in `mcp/tools/conflict/list_conflicts.py`.
- Repair the dead `resolved` / `auto-resolved` filters on `list_conflicts`.
- Regression tests for both.

Out of scope (deliberately):

- Tuning `SIMILARITY_CONFLICT_THRESHOLD`. The false positives in RCA §3 are real, but that
  threshold governs detection and needs its own evidence base; changing it here would conflate two
  fixes. Track separately.
- Any automated resolution of confirmed contradictions. The escalate-to-human stance stays.
- Backfilling or repairing conflict state already resolved before this lands.

## Phase 1 — stop recommending destruction

Change the suggestion to the non-destructive action and make its reasoning explicit.

```python
if len(members) >= 2:
    group_payload["suggested_resolution"] = {
        "tool": "resolve_conflict",
        "action": "keep_both",
        "group_id": group_id or None,
        "rationale": (
            "Non-destructive default. Menhir does not infer which member supersedes the other; "
            "confirmed contradictions are escalated for human judgement. Use action='replace' "
            "with an explicit keep_uuid/remove_uuid only if you have decided which is correct."
        ),
    }
```

Deliberate properties:

- **No `keep_uuid`, no `remove_uuid`.** Naming a member is the error; omitting them removes it. An
  operator who wants `replace` must choose the UUIDs themselves, which is the point — that choice
  is a human judgement, and it should read as one.
- **`keep_both` is safe on both branches.** For the ~3-of-7 false positives it is exactly right.
  For a genuine supersession it is conservative: both memories survive, retrieval ranking decides,
  and nothing is lost while a human decides.
- The `older` / `newer` member tags stay. They are accurate and are the operator's main signal.

Acceptance:

- No `suggested_resolution` in any `list_conflicts` response contains `remove_uuid`.
- `action` is `keep_both` for every group.
- Existing member ordering and `older`/`newer` tags unchanged.

## Phase 2 — repair the resolved-status read path

`list_conflicts(status="resolved")` and `status="auto-resolved"` can never return a row: all three
resolution branches null `conflict_group_id`, and the listing query requires it to be non-null.
This is why the RCA could not measure blast radius.

Two options; **prefer B.**

- **A — preserve the group id.** Keep `conflict_group_id` set on resolve and rely on
  `conflict_status` to distinguish. Smallest query change, but it changes graph-state semantics
  for every downstream reader of that property, and re-scan/dedup logic may treat a non-null group
  id as "still in a conflict group." Needs a blast-radius pass before it is safe.
- **B — read resolution history from telemetry instead.** The history already exists:
  `_record_suppression` → `record_conflict_resolution(uuid_a, uuid_b, status, action, reviewed_by)`
  per `.agent/conflict-resolution-history-proposal.md`. Route the `resolved` / `auto-resolved`
  filters at that store rather than the graph. No graph-semantics change, and it reports what
  actually happened — including who or what reviewed it — rather than reconstructing it from
  surviving nodes.

If neither is done promptly, the interim honest fix is to reject those status values with a message
saying resolution history is not exposed, rather than returning an empty list that reads as
"nothing has ever been resolved."

Acceptance:

- `status="resolved"` returns real historical resolutions, or fails with an explicit
  not-available error. It must not silently return `count: 0`.
- Each row identifies the action taken and whether it was `llm`, `auto`, or human.

## Phase 3 — tests

Currently nothing in `tests/` asserts anything about `suggested_resolution`. That absence is why a
formatting refactor could set a resolution policy unnoticed.

1. `suggested_resolution` never contains `remove_uuid`, for 2-member and N-member groups.
2. `action == "keep_both"` for every group returned.
3. Member ordering: given members with known `node_created_at`, `members[0]` is the oldest, and
   the member missing a timestamp sorts last. (Pins §1 of the RCA so the ordering stays correct.)
4. `status="resolved"` does not return an empty list when a resolution has been recorded — the
   test that would have caught the dead filter.

## Risks

- **An operator may have a workflow that reads `remove_uuid`.** Single-user local system, and the
  field is advisory; low risk, but it is an output-shape change to a tool contract and should be
  called out in the CHANGELOG rather than slipped in.
- **`keep_both` on a genuine supersession leaves both memories live**, so retrieval may surface a
  stale fact alongside the current one. This is the accepted trade: retention over deletion, with
  ranking and the existing floor doing the work. It is also strictly better than today, where the
  suggestion keeps the stale one and deletes the current one.
- **Phase 2 option A has unbounded reader impact.** Run `blast_radius` on the
  `conflict_group_id` property before choosing it.

## Follow-ups (not this plan)

- `SIMILARITY_CONFLICT_THRESHOLD` tuning, using the ~3-of-7 false-positive rate as the first data
  point.
- Whether a *reasoned* directional suggestion is worth building later, from source confidence,
  provenance, and explicit supersession. Only with evidence that it beats `keep_both`; principle 3
  above forbids reintroducing recency as the signal.
