---
artifact_schema: 1
artifact_type: plan
artifact_status: IMPLEMENTED
---

# From built to working: wiring remote sync end to end

Parent plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`
P3 design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`
P4 design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`
Tenancy position: issue #127
Status: **IMPLEMENTED — 2026-09-20.**

Implementation decisions: a published canonical snapshot selected by its durable project id wins
over local structure for that same identity; a display-name collision stays visible and resolves to
the local project unless the caller selects the snapshot id explicitly. No published view preserves
the legacy result shape; degraded views answer with a bounded warning; and explicit
`commit_project_snapshot` (not the final chunk) triggers mode-gated work.
Promotion records the authenticated MCP principal, durable intent is reconciled by the WRITE-only
maintenance job, and deployment-wide root cleanup selects one oldest candidate per project before
applying its bound. First sync receives a server-minted opaque project id; the CLI keeps a
server-specific local receipt under `.menhir/`, and later begins must present an id already
registered by that server. Display names never become graph identity.

## The problem this plan exists to fix

At proposal time, P0–P4 had proved each stage in isolation and **none of the stages after upload
were connected to anything.** This implementation closed those call-site gaps:

| Stage | Built | Reachable at runtime |
| --- | --- | --- |
| Bundle + upload (`menhir sync`) | yes | **yes** |
| Receive to `SEALED` + staged blob | yes | **yes** (operator surface, gated by mode) |
| Extract archive (`archive_plan`, `extraction_writer`, `extract_worker`) | yes | **yes**, on explicit commit in SHADOW/WRITE |
| Shadow scan (`shadow_scan`) | yes | **yes**, with materialized-root cleanup |
| Promote (`promotion`, `view_root`, `canonical_view`, `snapshot_structure`) | yes | **yes**, in WRITE mode |
| Read a published view | **yes** | **yes**, exact canonical identity precedes local structure |
| Reclaim abandoned roots (`sweep_view_roots`) | yes | **yes**, scheduled in WRITE mode |

At proposal time a user sync got only **bytes on a disk**: the upload reached `SEALED` and the
chain stopped there. Every module downstream was tested, including against a restored production
graph, but none of it ran.

Two consequences shape the ordering below:

1. **The read path is the only piece with no design at all**, and it determines what promotion must
   produce. It is therefore first, not last.
2. `SnapshotReceiveMode` (`OFF` / `RECEIVE` / `SHADOW` / `WRITE`) already exists and gates each
   stage. **Every step here is a step in making an existing mode actually do what its name says**,
   which is a useful constraint: no new configuration surface is needed.

## Step 1 — The read path (largest, least designed, blocks the rest)

Nothing reads a `CanonicalView`. This is why the pilot's "local structure reads are unaffected" was
so easy to satisfy: there is no read that could have been affected.

The natural choke point is `memory_graph_adapter.query_structure`, which already funnels every
structure read through one allowlist — the same reason `write_project_structure` is where the
identity fence lives. Guarding the 11 query types individually would mean 11 places to keep in
step and the next query added would silently miss it.

**Settled decisions:**

- An exact server-issued project id selects the canonical snapshot before local structure. A
  display name is only an alias: if an unrelated local project has the same name, the local project
  answers and both entries remain listed; the snapshot remains selectable by its id.
- Snapshot data travels in an internal envelope and the MCP renderer adds `[SNAPSHOT ...]`, keeping
  the legacy wire output byte-identical when no canonical view exists.
- A degraded view still answers but carries one bounded warning; new promotion into it is refused.
- Reads use the writer's `(view_root, path)` index and constrain both relationship endpoints to the
  current root and project id.

**Acceptance:** a structure query against a project with a published view returns that view's
content, carries its status, and a query against a project without one is byte-identical to today.
The pilot's read-comparison harness already exists and should be reused as the regression test.

## Step 2 — Extraction and shadow scan, wired to `SHADOW`

At proposal time `SHADOW` claimed to extract and scan without touching the graph but did neither,
because nothing called the extraction chain.

Wire `SEALED` upload → `archive_plan` → `extract_worker` → `shadow_scan` → report, with the
existing lease and the existing subprocess isolation. This is the lowest-risk step in the plan:
it is graph-inert by construction, the parent plan explicitly allows it to run indefinitely while
evidence accumulates, and every component has a hostile-corpus test behind it.

**Settled:** explicit `commit_project_snapshot` triggers extraction. The final chunk only seals;
it never opens attacker-supplied archive bytes as a side effect.

**Acceptance:** an upload sealed in `SHADOW` produces a shadow report, leaves no extraction root
behind, and writes nothing to the graph. Provable against the existing hostile corpus.

## Step 3 — Promotion, wired to `WRITE`

Wire `promote_snapshot` to a caller. Everything it needs exists and is tested.

**This step must land the principal from issue #127**, because the wiring is where authorization
has to live and retrofitting it afterwards means changing every caller. Per that issue, under a
single-company deployment the justification is attribution — "who promoted this and from what" —
rather than isolation. The check belongs in the same statement as the CAS, for the reason
`admit_structure_writer` documents: a build takes ~25s at real scale and ownership can change
inside that window.

Also implemented in this step:

- `mark_degraded` requires an authenticated actor; background recovery additionally uses a
  generation/root guard so stale work cannot wedge a newer shared view.
- A per-user bound under the shared `disk_budget_bytes`, so one large sync cannot starve the team.

**Acceptance:** an operator can promote a sealed, extracted snapshot; the promotion is attributed;
a second concurrent promotion loses cleanly; and the pilot harness re-run against a restored graph
still shows local structure unchanged.

## Step 4 — Schedule the sweep

`sweep_view_roots` is tested and has no scheduler, so abandoned roots accumulate for the life of a
deployment. `services/scheduler_tasks.py` is the established home: standalone functions returning a
result dict, wrapped by the orchestrator for timing and telemetry.

**Settled:** a deployment-wide pass chooses one oldest candidate per project before applying the
global limit. An explicitly project-scoped sweep retains oldest-first behavior within that project.

**Acceptance:** abandoned roots are reclaimed without operator action; current and previous roots
survive; the existing sweep counterexamples still pass.

## Step 5 — The residual: durable promotion intent

A process killed between the flip and the compensation leaves a view serving a root already judged
bad, with nothing recording that judgement — indistinguishable from success. Documented in
`promotion.py` and deliberately left open, because closing it needs a durable promotion-attempt
record plus a reconciler.

Implemented with `SnapshotPromotionAttempt`, written before the flip and transitioned through
publication/compensation to a terminal state. The WRITE-only maintenance cycle reconciles stale
nonterminal attempts by restoring the retained root or marking the unchanged view degraded. Both
operations are generation/root guarded so a concurrent valid promotion cannot be degraded. A
repeated explicit commit also repairs a `PROMOTING` upload receipt from the durable attempt after a
post-publication process crash; the disk-only inactivity sweep never guesses that state is failed.

## Step 6 — Make `menhir sync` tell the truth

`sync.py` now calls explicit commit after the server seals the upload and reports the terminal
`received`, `scanned`, or `published` stage together with the server-issued project and snapshot
ids. It persists the project receipt locally with atomic no-clobber publication for later syncs to
the same server. The begin response advertises explicit-commit support, and the client refuses an
older pre-commit server before sending source bytes.

## What is deliberately NOT here

- **`tenant_id`, per-tenant quotas, database-per-tenant, identity re-keying.** Per issue #127,
  none are justified by a single-company deployment. If hosted multi-customer becomes real, the
  multi-tenant design doc covers it and should be revisited then.
- **P7 scale work** — manifest-first negotiation, plain-directory sources, shared branch views.
- **Changes to the local scan path.** Unchanged throughout, as it has been all along.

## Suggested order

Steps 1 and 2 are independent and can proceed in parallel; 2 is much smaller. Step 3 depends on
step 1 only in that the read path determines what promotion must produce, so if step 1's decisions
are settled, step 3 can start before step 1 is finished. Steps 4 and 6 are small and can land
alongside. Step 5 is last.

The critical path is **step 1's design decisions** — specifically "when both exist, which answers".
Nothing downstream is safe to freeze until that is settled.
