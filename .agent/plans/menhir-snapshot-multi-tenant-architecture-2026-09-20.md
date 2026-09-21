---
artifact_schema: 1
artifact_type: plan
artifact_status: PROPOSED
---

# Designing snapshot ingest for wide deployment, not for one operator

Parent plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`
P4 design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`
P4 pilot: `.agent/reports/menhir-p4-pilot-restored-graph-2026-09-20.md`
Status: **PROPOSED — no implementation. This document changes no code.**

## The reframe, and what it actually costs

P0–P4 were built for a deployment with one operator, and the parent plan says so: P6 keeps
"hosted multi-tenant instances disabled until structure read/write ownership is complete". That
was a reasonable sequencing decision and it is not what this document disputes.

What it disputes is narrower and more expensive: **P4 added a graph WRITE path that has no owner
at all**, and several P4 shapes are now baked into durable data. Fixing an authorization gap in
code is cheap. Re-keying data that already exists in customer deployments is not. So the question
worth asking now is not "is P4 multi-tenant" — it plainly is not — but **which P4 decisions get
much more expensive if they are changed after the first customer**.

There is one genuinely good piece of news, established by the pilot rather than assumed: **a view
root is already a self-contained unit.** Every edge is matched inside its own root, no edge crosses
roots, and a purge reclaims a root whole. That is exactly the shape multi-tenancy needs, whichever
isolation model is chosen. The architecture is not wrong. It is **under-scoped**.

## The decision that determines every other one

**How are tenants isolated in the graph?** Everything below depends on this and it should be
settled before any of it is implemented.

| Option | What it means | Cost | Leak risk |
| --- | --- | --- | --- |
| **A. One database, tenancy key on every node and every query** | What P5 already proposes: map `(tenant_id, project_id, view_id)` to a namespace key. | Cheapest to build. Works on Community. | Every query ever written is a chance to leak. One missing `WHERE` is a cross-tenant read. |
| **B. Database per tenant** | Neo4j multi-database; a query physically cannot cross. | Enterprise licence; per-database overhead; connection routing; cross-tenant ops (metrics, sweeps, migrations) become fan-out. | Near zero by construction. |
| **C. Hybrid** | Shared database for canonical/public structure, database per tenant for private workspace views. | Most complex. Two models to reason about. | Low where it matters. |

The existing codebase is evidence for how A behaves in practice. `structure_queries` has 14 query
methods and the CF-164 finding was that two of them were reachable and undocumented; CF-126 was an
unscoped recall. Those are exactly the failure mode A invites, in a codebase with one tenant where
the blast radius was zero. **Under A the same class of bug becomes a customer data breach.**

That is an argument for B or C, not a proof. It is the owner's call, and it is the first call.

## What P4 assumes today

Each row is a real thing in the code, not a hypothetical. "Cost later" is what it takes to change
once customer data exists.

### 1. The promotion path has no principal — the one to fix first

`promote_snapshot(neo4j, *, project_id, view_key, snapshot_id, ...)` and
`publish_root(neo4j, *, project_id, view_key, root_id, expected_generation)` take the project as a
**caller-supplied parameter** and never derive or verify it against an authenticated identity. Any
caller that reaches promotion can publish into any project's canonical view.

This is not a theoretical gap and it is inconsistent with menhir's own upload path, which got it
right: `receive.py` carries a `principal` on every `UploadRecord` and re-derives ownership on every
call, with the module docstring stating "a token is not authorization". **P4 dropped a property P2
already had.**

The fix is not a parameter. The session's own repeated lesson applies: the ownership check must
happen in the **same statement** as the CAS, the way `admit_structure_writer` validates identity,
fence and registration together — because a build takes 25 seconds at real scale (measured), and
ownership can change inside that window.

*Cost now:* small. Nothing calls `promote_snapshot` yet, so signatures are free to change.
*Cost later:* every caller, plus an audit of what was promoted by whom with no record of it.

### 2. `(project_id, view_key)` has no tenant dimension

`CanonicalView` is unique on `(project_id, view_key)`; `ViewRoot` carries the same pair. If
`project_id` is a server-issued globally-unique opaque id, this is survivable. If it is ever
tenant-scoped, derived from a name, or caller-supplied, two tenants collide on one view.

**There is no `tenant_id` anywhere in the codebase.** The only existing scoping is
`core/tenancy.py`, which resolves a namespace from the caller's `client_name` header against
static server-side config — and its own docstring frames it as a convenience for callers that
cannot be trusted to scope their own writes, not as a security boundary. An unrecognised client
falls through to the default namespace.

*Cost now:* a property and a constraint.
*Cost later:* re-keying every `ViewRoot` and `CanonicalView` in every deployment, with live
pointers, under a migration that must not lose the `previous` root.

### 3. Identity binds to `(server hostname, filesystem path)`

`PROJECT_IDENTITY_ROOT_CONSTRAINT` enforces one active binding per `(host, normalized root)`, and
`binding_host()` is `socket.gethostname()` — **the server's own hostname**, identical for every
client on a hosted instance. Filesystem paths are not unique across tenants; `/home/user/app` is
not distinctive. Two customers with the same path on one hosted instance contend for a single
binding.

This predates P4 and P4 does not yet use it — but the moment an uploaded snapshot adopts a project
identity, it does.

*Cost now:* design-only; decide what a "root key" means for an upload that has no local directory.
*Cost later:* identity is the thing everything else hangs off; changing its key is the most
expensive migration on this list.

### 4. Quotas and budgets are instance-global

- `disk_budget_bytes` (4 GiB) is one number for the whole instance. **One tenant can exhaust
  staging for every other tenant** — a denial of service that needs no exploit, just a large repo.
- `max_receiving_per_project` is "across all principals for one project", so two principals in
  different tenants that share a project key interfere with each other by design.
- `max_receiving_per_principal` is the only limit that is actually per-caller.

*Cost now:* small; these are config values and a scoping key.
*Cost later:* moderate, but the incident happens before the fix.

### 5. The sweep is global and unfair

`sweep_view_roots(neo4j, project_id=None, limit=50)` walks all roots oldest-first. With many
tenants, one tenant generating garbage fastest consumes every pass, and another tenant's abandoned
roots are never collected. The bound that makes it safe for one operator makes it starve under
many.

Worse: **nothing schedules it at all today.** Whatever fairness model is chosen has to be decided
at the same time as the scheduling, not bolted on after.

### 6. `mark_degraded` is an unauthenticated denial-of-service primitive

It is deliberately unconditional on generation — correct, for its purpose. But a degraded view
**refuses all promotion until an operator intervenes**. With no ownership check, any caller that
reaches it can permanently wedge another tenant's project and require human action to recover.

Degradation needs to be authorized, and "repair is an explicit operator action" needs to mean a
*tenant's* operator, not the instance's.

### 7. Promotion is synchronous and unbounded

The pilot measured 24.6s for a first promotion of 20,183 nodes, and 27.2s to scan the repo before
it. A blocking call of that length is a single-operator shape. At wide deployment this is a work
queue with per-tenant fairness, backpressure and a resumable record — not a function call inside a
request.

That also interacts with finding 1: a queued promotion makes the "ownership can change during the
build" window *longer*, which makes checking ownership at the flip more important, not less.

### 8. Policy constants are global where they are arguably per-plan

Retention is exactly one previous root; the lease is 900s; the chunk default is 1 MiB. All were
right decisions for one operator. At least retention is plausibly a per-plan product decision.
Flagged, not argued.

### 9. The unclosed residual gets worse with scale

A process killed between the flip and the compensation leaves a view serving a root already judged
bad, with nothing recording it. For one operator this is rare enough to accept and is documented.
Across many tenants and many promotions per day, "rare" becomes routine, and nobody is watching
each tenant's graph the way an owner watches their own.

Wide deployment turns this from an accepted residual into a required piece of work: a durable
promotion-attempt record and a reconciler.

## What P4 got right and should survive

Worth stating, so the reframe does not become a rewrite:

- **Roots are self-contained.** No cross-root edges, verified against a real graph. This is what
  makes per-tenant isolation possible under any of options A/B/C.
- **Publishing is one atomic pointer move**, and rollback is another. Measured at 0.11s to roll
  back a 20,183-node promotion — it does not degrade with tenant count or snapshot size.
- **Promotion never prunes.** Deletion is absence from the next root. The entire class of
  cross-tenant prune bugs has no door, because there is no prune.
- **Separation is structural, not a filter.** `SnapshotEntity` versus the local label means no
  local query can reach snapshot data *including queries not yet written* — which is precisely the
  property option A struggles to maintain by discipline.
- **The lease and probe-write pattern** is already the right shape for concurrent writers who do
  not trust each other; it needs a tenant key, not a redesign.

## Owner decisions

1. **Isolation model: A, B or C.** Determines everything else. Recommend deciding before any
   further P4-adjacent implementation.
2. **Is `project_id` globally unique and server-issued, or tenant-scoped?** If the former, finding
   2 shrinks considerably.
3. **What is the authenticated tenant derived from?** It must not be `client_name` or any other
   caller-supplied label. Today nothing else exists.
4. **What is a "root key" for an uploaded project with no local directory?** Finding 3 cannot be
   designed without this.
5. **Per-tenant quota model**, including whether staging budget is reserved, fair-shared, or
   billed.
6. **Is promotion queued?** If yes, it should be designed as queued now rather than converted
   later.

## Suggested sequencing

Ordered by cost-of-delay rather than by difficulty:

1. **Thread an authenticated principal through promotion and check it inside the CAS.** Nothing
   calls it yet; this is the cheapest it will ever be, and it closes a real write-path hole.
2. **Decide the isolation model**, then add the tenancy key to `ViewRoot` and `CanonicalView`
   while there is no production data to migrate.
3. **Authorize `mark_degraded`** — small change, removes a wedging primitive.
4. **Scope quotas per tenant** before the first shared instance, not after the first incident.
5. **Schedule the sweep with a fairness rule**, decided together rather than sequentially.
6. **Then** the promotion-attempt record and reconciler, which is the largest item and the one
   that only becomes mandatory at scale.

Items 1–4 are small and get dramatically more expensive after the first customer. Item 6 is large
and does not.
