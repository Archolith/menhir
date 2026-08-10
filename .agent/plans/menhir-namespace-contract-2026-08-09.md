# Namespace Contract and Contamination Cleanup

Status: **planned; implementation not started**

RCA: `.agent/reviews/rca-namespace-contamination-experimental-scalars-2026-08-09.md`

## Why

Namespace is a caller-supplied free-text label, validated at exactly one of several write
boundaries, with opt-in isolation so the default lands in shared space. Experimental scalar Views
now hold rank-1 authoritative current state in `plans`, `yawn.market`, and `IdeaProjects`, with no
provenance to trace them by.

The cleanup is the easy half and the least durable. The point of this plan is the contract: without
it the namespaces refill.

## Design stance

1. **Validate at a chokepoint, not at each door.** The 2026-08-07 fix was installed where the bug
   was seen. Repeating that per-tool guarantees the next new write path ships unguarded.
2. **A namespace that cannot hold an episode must not hold a View.** Views outliving their possible
   grounding is how `yawn.market` became permanently ungrounded by construction.
3. **Cleanup must be selective and reversible.** `delete_namespace` is the wrong instrument —
   these namespaces plausibly hold real episodes and entities beneath the View layer.
4. **Prefer a convention over a registry.** A hard allowlist means every new project needs
   registration before it can be remembered. Reserved prefixes plus a warning surface get most of
   the benefit at a fraction of the friction.

## Scope

In scope:

- Centralize namespace validation across all write paths.
- Close the View/episode namespace asymmetry.
- Add namespace inventory so this is observable.
- Remove the identified contaminating Views.
- A convention separating experimental from durable namespaces.

Out of scope:

- Requiring grounding for all scalar Views. Ungrounded admission is a real governance question
  (RCA §5) but belongs with the scalar/View authority work, not here.
- Changing opt-in isolation to opt-out. Tempting, but `namespace=None` currently means "search
  every group" and flipping that silently narrows every existing read. Needs its own plan.
- Migrating or renaming `IdeaProjects`, `plans`, or other in-use namespaces beyond the dotted-name
  decision in Phase 2.

## Phase 0 — inventory

There is no way to list namespaces or see what is in them; scoping the RCA required guessing names
from prior output and probing one at a time.

- Add a read-only `list_namespaces` returning, per namespace: entity count, episode count, View
  count, newest and oldest write, and whether the name is a legal graphiti `group_id`.

Acceptance: one call enumerates every namespace with counts, and flags illegal ones. Expect at
least `yawn.market` to be flagged.

## Phase 1 — one validation chokepoint

Move the check below the tool layer so every path inherits it: `add_memory`,
`add_memory_and_track`, `ingest_document`, `ingest_project`, REST routes, and the scalar/View
write path.

- Validate where the namespace is resolved to a `group_id` — `namespace_to_group_id` is the single
  function every write already passes through, which makes it the natural gate.
- `add_memory.py:79` keeps its early check for the better error message, but is no longer the only
  one.
- The View/scalar write path must reject an illegal namespace outright (principle 2). A View in a
  namespace no episode can occupy is unreachable-by-grounding forever.

**Risk — this will start rejecting a namespace already in use.** `yawn.market` is live and illegal.
Decide explicitly in Phase 2 before enforcement, or Phase 1 breaks writes that currently succeed.

Acceptance:

- Every write path rejects `"yawn.market"`-shaped input with the same actionable error.
- A test enumerates the write entry points and asserts each validates, so a new path fails the test
  rather than silently shipping unguarded.

## Phase 2 — decide the dotted-namespace question

`yawn.market` cannot be a graphiti `group_id`. Options:

- **A — rename to `yawn-market` (preferred).** Consistent with the character rule, keeps the
  project-name association readable. Requires rewriting `namespace` on affected nodes; in this case
  that is one contaminating View, so the migration is trivial *now* and grows with delay.
- **B — sanitize on the way in** (map `.` → `-` inside `namespace_to_group_id`). No caller changes,
  but it silently makes `yawn.market` and `yawn-market` the same silo, which is a surprising
  collision, and it hides the error rather than reporting it. Rejected unless A proves disruptive.

Check first whether other dotted namespaces exist — Phase 0 answers this. Project *names* in the
structure graph are dotted (`cth.mcp.delegate`, `yawn.rip`); if any became namespaces, the blast
radius is larger than one View.

Acceptance: no live namespace fails `_GROUP_ID_SAFE_PATTERN`.

## Phase 3 — remove the contaminating Views

Do this after Phase 0, so removal is driven by an inventory rather than by what happened to appear
in a probe.

Identified so far:

| Namespace | Views to remove |
|---|---|
| `plans` | `7f44b9aa…` (`coins: 30`), `3c6c4464…` (`coin count: 30`) |
| `yawn.market` | `32f1f685…` (`milk spend: 2`) |
| `IdeaProjects` | bike/movie scalars — **selective**; keep `perception abstained` self-telemetry |

Constraints:

- Delete **Views**, not namespaces. `plans` and `IdeaProjects` hold real content.
- `IdeaProjects` needs per-View judgement — it is genuinely mixed.
- Selection is by surface text, since there is no provenance to filter on (RCA §5). That is
  unsatisfying and should be recorded as the reason Phase 4 matters.
- Capture the removed rows first. These are the only concrete examples of the failure mode, and
  they are worth keeping as fixtures for a regression test.

Acceptance: `view_entropy` on `plans` and `yawn.market` returns zero Views; `IdeaProjects` retains
its self-telemetry counters and no benchmark scalars.

## Phase 4 — separate experiment from production

Cleanup without this just resets the clock.

- Reserve a prefix convention: `lme-*` already means benchmark; add `scratch-*` or `test-*` as
  explicitly disposable, documented in `.agent/memory-policy.md`.
- Have `list_namespaces` (Phase 0) flag namespaces that are neither reserved nor previously seen,
  so drift is visible rather than discovered months later.
- Consider pinning the `claude` client via `MENHIR_CLIENT_NAMESPACES` the way `tiny-agent` already
  is. This is the highest-leverage single change — it would have prevented all of this — but it
  constrains every interactive write, so decide deliberately rather than as a side effect.

Acceptance: writing to an unrecognized namespace is visible in inventory; the reserved prefixes are
documented.

## Risks

- **Phase 1 enforcement breaks live writes** if Phase 2 has not landed. Sequence them together or
  gate Phase 1 behind a warn-only mode first.
- **Over-tightening blocks legitimate new namespaces.** A new project should not need registration
  to be remembered; this is why Phase 4 warns rather than rejects.
- **Deleting the wrong View.** `IdeaProjects` mixes real self-telemetry with benchmark rows, and
  selection is by text. Capture before delete, and do it in one reviewable batch rather than
  incrementally.
- **Pinning the `claude` client** would silently redirect every interactive write, including ones
  intentionally targeting a project namespace. Do not fold this into another phase.

## Follow-ups (not this plan)

- Whether a scalar View should be admissible with zero grounding at all (RCA §5). This is the
  governance question underneath the symptom.
- Opt-out isolation: whether `namespace=None` should stop meaning "read every group".
