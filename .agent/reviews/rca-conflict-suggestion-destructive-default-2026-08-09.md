# RCA: `list_conflicts` recommends destroying the newer of two contradictory memories

**Date:** 2026-08-09
**Severity:** Medium-High. The defect is advisory, not automatic — nothing is destroyed unless an
operator follows the suggestion through the separate `resolve_conflict` tool. But the advice is
wrong in the direction that loses data, it is presented as a system recommendation rather than a
guess, and **whether it has already been followed is currently unknowable from the operator
surface** (see §4). Nothing is on fire; nothing should be resolved by hand until this is fixed.
**Status: ROOT CAUSE CONFIRMED by direct code read.** No live repro was run — the confirming
evidence is that the code path is unconditional, so it cannot behave otherwise.

## Summary

`list_conflicts` attaches a `suggested_resolution` to every group with 2+ members. It is
hardcoded to `action: "replace"` — a destructive action — with `keep_uuid = members[0]` and
`remove_uuid = members[1]`. Members are sorted oldest-first, so the suggestion always means:
**keep the older memory, destroy the newer one.**

That is backwards for the case conflict detection exists to catch. When a fact genuinely changes,
the newer statement is the correction; the suggestion recommends deleting it and keeping the stale
claim.

The deeper problem is not the direction. It is that **the conflict pipeline deliberately declines
to pick a winner, and this suggestion manufactures one anyway.**

## 1. What the code does

`mcp/tools/conflict/list_conflicts.py:58-65`:

```python
if len(members) >= 2:
    group_payload["suggested_resolution"] = {
        "tool": "resolve_conflict",
        "action": "replace",
        "keep_uuid": members[0].get("uuid"),
        "remove_uuid": members[1].get("uuid"),
    }
```

Unconditional. No reference to content, source confidence, provenance, review state, or whether
the two statements actually contradict.

`members` is ordered by `_coerce_conflict_members` → `_node_sort_key`
(`mcp/formatters.py:109-120`), which sorts on `node_created_at` ascending, missing timestamps
last, UUID as tiebreak. So `members[0]` is genuinely the oldest and the `older`/`newer` labels in
the output are honest — **the ordering is not the bug.** I initially suspected the labels were
fabricated from list position because the Cypher `collect()` in
`consolidation_queries.py:601-608` has no ordering; that concern is void, because the Python side
sorts deterministically after collection.

`replace` is destructive. Per `consolidation_queries.py:721-727`, it absorbs the removed node's
content into the kept node, sets the removed node to `GONE`, and bridges its edges.

## 2. The pipeline deliberately declines to choose — and this overrides that

Every automated resolution path in `services/lifecycle_conflicts.py` uses `keep_both`:

| Path | Line | Action |
|---|---|---|
| Spurious single-member group | 146 | `keep_both`, `false_positive` |
| LLM review says *not* a contradiction | 211, 217 | `keep_both`, `false_positive`, + suppression row |
| Aging auto-resolve (>14d) | 276, 286 | `keep_both`, `reviewed_by="auto"` |

And when the LLM **confirms** a real contradiction (line 195-197), it does not resolve at all:

```python
if is_conflict:
    await asyncio.to_thread(self.graph_adapter.set_conflict_group_status, group_id, "unresolved")
```

It marks the group unresolved and escalates to a human, choosing no winner. That is a deliberate
design stance, consistent with menhir's documented fail-safe-toward-retention posture: no
automated path in the system ever destroys a member of a conflict pair.

`list_conflicts` then fills that intentional gap with an arbitrary destructive recommendation.
Two incompatible resolution policies coexist: the governed pipeline retains, the operator-facing
tool advises deleting.

It also never suggests `keep_both`, despite that being the only action every automated path uses,
and despite `keep_both` being correct for the false positives described in §3.

## 3. Live evidence (7 unresolved groups, pulled 2026-08-09)

**The direction is wrong on a real supersession.** Group `3f6310df`:

- older — *"The scripts/longmemeval/build_graph.sh sets no scalar flags at all."*
- newer — *"build_graph.sh exports the scalar gate flags that were previously absent."*

The newer statement explicitly describes correcting the older one. The tool suggests keeping the
older and removing the correction.

**Roughly 3 of 7 groups are not contradictions at all**, where `keep_both` is the right action and
`replace` would destroy a valid memory:

| Group | Members | Why it is not a contradiction |
|---|---|---|
| `f985d192` | Instrument Serif 400 / JetBrains Mono 400/700 | One feature can embed both fonts |
| `669bcd9a` | `index.html` / `index.html` | Different projects; pure name collision |
| `b6305d1a` | Piece C / Piece D | The content itself says they are separate efforts |

These are `SIMILARITY_CONFLICT_THRESHOLD = 0.85` catching topical similarity rather than
contradiction — a known tuning item in `post-v1-todo.md` ("Threshold tuning from operational
data"). This is that operational data.

All 7 groups were detected between 2026-07-10 and 2026-07-29; nothing newer. Detection is not
actively churning.

## 4. Blast radius is unquantified, and that is a second finding

I could not determine whether the suggestion has ever been acted on, because
**`list_conflicts(status="resolved")` and `status="auto-resolved"` are structurally incapable of
returning rows.**

All three resolution branches clear the group id (`consolidation_queries.py:755`, `:879`, `:899`
all set `n.conflict_group_id = null`), while `list_conflict_groups` filters
`WHERE n.conflict_group_id IS NOT NULL` (`:596-598`). Once resolved, a group is permanently
invisible to the listing. Both non-default status filters are dead.

The underlying history does exist — `_record_suppression` writes
`record_conflict_resolution(uuid_a, uuid_b, ...)` including `(keep_uuid, remove_uuid)` for manual
`replace`/`discard_new`, per `.agent/conflict-resolution-history-proposal.md`. It is the operator
read path that is missing. So: impact is unmeasured, not zero.

## 5. Root cause

**A presentation-layer affordance manufactured a resolution decision that the conflict pipeline
deliberately refuses to make.**

`suggested_resolution` was introduced in `e77d4b7` (2026-03-10), whose full commit message is
`refactor: compact mcp memory responses` — a response-formatting pass. There is no rationale in
the commit, no comment in the code, and no design doc describing a keep-oldest policy. It was
almost certainly added as a convenience for compact output, picking the two available UUIDs in
list order, without anyone deciding that "oldest wins" was the policy.

Contributing factors:

1. **The tool is `required_tier = "readonly"`.** The destructive consequence lands in a different
   tool, so the suggestion never trips a permission gate or a destructive-operation review.
2. **No test pins it.** Nothing in `tests/` asserts anything about `suggested_resolution`; the only
   `keep_uuid` references are the `conftest.py` stub.
3. **Recency is the only signal used**, and it is used implicitly — as a side effect of sort order,
   not as a stated tie-break rule.
4. **No feedback loop.** Because resolutions are invisible to the tool (§4), a bad suggestion
   produces no observable consequence for the operator who followed it.

## 6. What is *not* the cause

- Not the member ordering — verified correct (§1).
- Not `resolve_conflict` under-removing. The suggestion passes an explicit `remove_uuid`, and
  `resolve_conflict_group` only removes all non-keep members when `remove_uuid is None`
  (`:786-787`). Following the suggestion removes exactly the one member shown.
- Not conflict detection failing to run. It runs; ~3 of 7 results are simply false positives.

## Remediation

See `.agent/plans/menhir-conflict-suggestion-remediation-2026-08-09.md`.
