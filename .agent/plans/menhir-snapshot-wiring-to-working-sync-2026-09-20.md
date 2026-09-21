---
artifact_schema: 1
artifact_type: plan
artifact_status: PROPOSED
---

# From built to working: wiring remote sync end to end

Parent plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`
P3 design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`
P4 design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`
Tenancy position: issue #127
Status: **PROPOSED — no implementation.**

## The problem this plan exists to fix

P0–P4 built and proved each stage of remote sync in isolation. **None of the stages after upload
are connected to anything.** Verified by call-site search, not by reading docs:

| Stage | Built | Reachable at runtime |
| --- | --- | --- |
| Bundle + upload (`menhir sync`) | yes | **yes** |
| Receive to `SEALED` + staged blob | yes | **yes** (operator surface, gated by mode) |
| Extract archive (`archive_plan`, `extraction_writer`, `extract_worker`) | yes | **no caller** |
| Shadow scan (`shadow_scan`) | yes | **no caller** |
| Promote (`promotion`, `view_root`, `canonical_view`, `snapshot_structure`) | yes | **no caller** |
| Read a published view | **not built** | — |
| Reclaim abandoned roots (`sweep_view_roots`) | yes | **no scheduler** |

So today a user syncs and gets **bytes on a disk**. The upload reaches `SEALED` and the chain
stops there. Every module downstream is tested, including against a restored production graph, and
none of it runs.

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

**Design questions to settle first — this step should not start until they are answered:**

- When a project has both a published view and local structure, **which answers?** Plausible
  positions: local always wins (snapshot is advisory), view wins when present, or the caller
  chooses. This is the single most consequential decision in the whole feature and it is not
  currently written down anywhere.
- How does `[SNAPSHOT ...]` status reach the caller — a field, a prefix on results, or a separate
  status call? The parent plan asks for it without specifying the shape.
- What does a **degraded** view return? The parent plan (line 738) says reads carry a warning. The
  wire shape of that warning is undecided.
- Does a read of a view need the same `(root, path)` index the writer uses, or different access
  patterns? Worth answering before promotion's node shape is frozen by real data.

**Acceptance:** a structure query against a project with a published view returns that view's
content, carries its status, and a query against a project without one is byte-identical to today.
The pilot's read-comparison harness already exists and should be reused as the regression test.

## Step 2 — Extraction and shadow scan, wired to `SHADOW`

`SHADOW` mode claims to extract and scan without touching the graph. It currently does neither,
because nothing calls the extraction chain.

Wire `SEALED` upload → `archive_plan` → `extract_worker` → `shadow_scan` → report, with the
existing lease and the existing subprocess isolation. This is the lowest-risk step in the plan:
it is graph-inert by construction, the parent plan explicitly allows it to run indefinitely while
evidence accumulates, and every component has a hostile-corpus test behind it.

**Open:** what triggers extraction — the sealing call itself, an operator command, or a worker
picking up sealed uploads? This is the same "is promotion queued" question from issue #127 in an
earlier form, and answering it once covers both.

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

Also in this step, because they are cheap now and not later:

- A guard on `mark_degraded`, which currently lets any caller wedge a shared project until a human
  intervenes.
- A per-user bound under the shared `disk_budget_bytes`, so one large sync cannot starve the team.

**Acceptance:** an operator can promote a sealed, extracted snapshot; the promotion is attributed;
a second concurrent promotion loses cleanly; and the pilot harness re-run against a restored graph
still shows local structure unchanged.

## Step 4 — Schedule the sweep

`sweep_view_roots` is tested and has no scheduler, so abandoned roots accumulate for the life of a
deployment. `services/scheduler_tasks.py` is the established home: standalone functions returning a
result dict, wrapped by the orchestrator for timing and telemetry.

**Open:** fairness. The current sweep is oldest-first with a global limit of 50, which under many
projects lets the noisiest starve the others. For a single-company deployment this is a small
concern and a round-robin over projects is probably sufficient — but it should be decided
deliberately rather than inherited from the single-operator default.

**Acceptance:** abandoned roots are reclaimed without operator action; current and previous roots
survive; the existing sweep counterexamples still pass.

## Step 5 — The residual: durable promotion intent

A process killed between the flip and the compensation leaves a view serving a root already judged
bad, with nothing recording that judgement — indistinguishable from success. Documented in
`promotion.py` and deliberately left open, because closing it needs a durable promotion-attempt
record plus a reconciler.

Left last on purpose. It is the largest item, it does not get more expensive with time, and with a
small number of users the window is genuinely rare. It should be done before any deployment where
nobody is watching the graph.

## Step 6 — Make `menhir sync` tell the truth

`sync.py` currently ends at `upload <state>`. Once steps 1–3 land, the command should report what
actually happened to the snapshot — extracted, scanned, promoted, or refused — rather than
reporting a successful upload of bytes that then go nowhere. Small, and worth doing in the same
release as step 3 so the client's story matches the server's behaviour.

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
