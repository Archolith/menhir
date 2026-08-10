# RCA: Experimental scalars hold authoritative state in real project namespaces

**Date:** 2026-08-09
**Severity:** Low-Medium. Nothing is lost or corrupted. The harm is fabricated authority: scalar
Views carry *current-state* authority in the recall packet, these Views are reachable at **rank 1**
(`view_entropy`, 2026-08-09), and they sit in namespaces holding real project knowledge. A question
about spend in the `yawn.market` context can surface `user's milk spend: 2` as authoritative
current state.
**Status: ROOT CAUSE CONFIRMED by code read + live namespace probe.** Origin of the specific rows
is **not** established — see §5, and it is itself a finding.

## Summary

Namespace is a caller-supplied free-text label with no registry, no allowlist, and validation at
exactly one of several write boundaries. Isolation is opt-in by design, so the default behaviour is
to write into shared space. Experimental scalar Views consequently landed in `plans`,
`yawn.market`, and `IdeaProjects` alongside real content, and they are ungrounded — no episode, no
evidence — so they cannot be attributed to a run or removed by provenance.

## 1. What is actually in there

`view_entropy` scoped per namespace (2026-08-09):

| Namespace | Views | Benchmark-shaped | Notes |
|---|---|---|---|
| `plans` | 2 | 2 (100%) | `user's coins: 30`, `user's coin count: 30` |
| `yawn.market` | 1 | 1 (100%) | `user's milk spend: 2` |
| `IdeaProjects` | mixed | several | bike/movie scalars **plus** legitimate `perception abstained` self-telemetry |

For `plans` and `yawn.market`, **every** View in the namespace is experimental. `IdeaProjects` is
genuinely mixed, so it cannot be cleaned wholesale.

Note this measures the **View layer only**. Those namespaces may hold real episodes and entities;
`delete_namespace` would be the wrong instrument.

## 2. Namespace is unvalidated free text

- `DEFAULT_NAMESPACE = "default"` (`domain/namespace.py:27`). There is no registry or allowlist of
  legal namespaces anywhere.
- `MENHIR_CLIENT_NAMESPACES` pins only `tiny-agent-rp=palworld-rp` and `tiny-agent=home-assistant`.
  The `claude` client is **not** pinned, so it writes whatever namespace it passes.
- Isolation is opt-in *by design* (`domain/namespace.py:14-16`): `namespace=None` writes to the
  default group and **reads are not filtered at all** — search spans every group. The safe-looking
  default is the shared one.

## 3. Validation exists at one boundary out of many

`namespace_group_id_error` — added 2026-08-07 to turn a deep enrichment-worker failure into an
immediate error — is called from exactly one place in the entire codebase:

```
src/menhir/mcp/tools/ingest/add_memory.py:79
```

Nothing else validates. `add_memory_and_track`, `ingest_document`, `ingest_project`, the REST
routes, and the scalar/View write path all accept any string.

## 4. Proof the View layer accepts what the episode layer cannot

`yawn.market` contains a dot. `_GROUP_ID_SAFE_PATTERN` is `^[a-zA-Z0-9_-]+$`
(`domain/namespace.py:33`), mirroring graphiti's `validate_group_id` exactly. So `yawn.market` is
**not a legal graphiti group_id** — an episode targeted at it would be rejected by graphiti in the
enrichment worker, and since 2026-08-07 `add_memory` rejects it outright.

Yet a scalar View is stamped `namespace="yawn.market"` and is reachable at rank 1.

That is direct evidence of an isolation asymmetry: **Views can occupy namespaces that
graphiti-backed content structurally cannot.** Such a namespace can never hold the episodes that
would give its Views context, so anything written there is permanently ungrounded by construction.

## 5. The rows are ungrounded, so origin is unrecoverable

`get_provenance` on the contaminating Views (2026-08-09):

| View | episode_count | evidence | anchor_paths |
|---|---|---|---|
| `user's coins: 30` (`7f44b9aa…`) | 0 | `[]` | `[]` |
| `user's bike spend: 125` (`8402b0b8…`) | 0 | `[]` | `[]` |

No source episode, no evidence, no anchors. **I cannot determine which run or session produced
them**, and neither can an operator. They cannot be selected for deletion by provenance, only by
inspecting their surface text and judging it synthetic.

This is a finding in its own right: a scalar View that asserts current state while carrying no
grounding is exactly the shape the governance model exists to prevent.

## 6. The disciplined path is not the source

The LongMemEval harness namespaces correctly. `bench-b7-lf/scripts/longmemeval/lib/ingest.py:2`
documents "STABLE namespaces (`lme-<question_id>`), no per-item reset on success", with a
namespace-window drain to keep enrichment complete per namespace. The in-flight KU78 run uses that
scheme and writes `lme-*` namespaces.

None of the contaminated namespaces are `lme-*` shaped. So this did **not** come from the bench
harness — it came from interactive or ad-hoc writes through a normal client, where nothing forces a
namespace choice.

That is the asymmetry worth naming: **the automated path is disciplined about namespaces; the
interactive path has no guard rails at all.**

## 7. Root cause

**Namespace is an unenforced convention rather than a checked contract, and the default is
shared rather than isolated.** An experiment run through the interactive MCP inherits whatever
namespace the caller happens to pass — including one holding real project knowledge — and nothing
in the write path objects.

Contributing factors:

1. **Opt-in isolation** (§2). Doing nothing puts data in shared space; you must actively choose a
   silo. The safe default is the unsafe one.
2. **Single-boundary validation** (§3). The 2026-08-07 fix was applied where the bug was observed
   rather than at a chokepoint, so the other write paths kept the old behaviour.
3. **No namespace inventory.** There is no tool to list namespaces or their contents. Scoping this
   required probing `view_entropy` namespace by namespace with names guessed from earlier output.
4. **Views admitted without grounding** (§5), so contamination is untraceable after the fact.
5. **No separation of experiment from production.** No reserved prefix, no scratch namespace, no
   convention that a test write must be disposable.

## 8. What is *not* the cause

- **Not the bench harness** (§6) — it is the one component doing this correctly.
- **Not the 2026-08-07 namespace validation being wrong.** It is correct; it is just installed at
  one door of several.
- **Not a graphiti isolation failure.** graphiti's `group_id` partition works as documented; the
  gap is that menhir's View layer writes outside it.

## Remediation

See `.agent/plans/menhir-namespace-contract-2026-08-09.md`.
