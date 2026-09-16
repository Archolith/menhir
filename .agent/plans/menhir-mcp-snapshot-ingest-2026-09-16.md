---
artifact_schema: 1
artifact_uuid: 174875fb-f36f-43a6-9595-cd30667087bc
artifact_type: plan
artifact_status: IMPLEMENTING
---

# MCP-bundled remote project snapshot ingest

## Execution status (2026-09-16)

**P0 code half: DONE. P1: DONE. P2A receiver BUILT, measurement NOT RUN. P2B durable receiver:
BLOCKED on those measurements.**

P2A's code half is in: `menhir.snapshot.receive` plus four operator-tier MCP tools registered
only while `MENHIR_SNAPSHOT_RECEIVE_MODE=staging`, so while off they are neither advertised nor
invocable, and each endpoint re-checks the mode at call time. The receiver reaches SEALED and
stops; a test reads the module AST and fails if it imports `zipfile` or anything graph-shaped.
Quotas, TTL, restart-resume, orphan reclamation, replay semantics and telemetry redaction are
tested (34 + 11 tests).

**What remains for P2A's gate is the measurement itself, which is a deployment act, not a code
one:** stand the staging instance up with the release ingress, probe 64 KiB / 256 KiB / 1 MiB /
2 MiB, record latency, memory, retries and the largest reliably accepted call, then set
`chunk_bytes` one rung below the largest repeatedly stable size. Until that runs, every
`SnapshotLimits` value stays PROVISIONAL and P2B stays blocked.

The env var is deliberate for P2A and temporary: P2B replaces it with the registered feature
flag (`off`/`receive`/`shadow`/`write`) and adds commit-through-SEALED.

**P0 provenance addendum: DONE (2026-09-16).** `source_head` is replaced by a `provenance` object
carrying `base_commit`, `commit_tree` (the commit's tree OID, so a server that ever holds the
commit's objects can compare them against what arrived), `branch` (None when detached), `dirty`,
and `quality`. `quality` is forced to `self_reported` on parse -- a client claiming
`trusted_automation` is exactly the claim the field exists to refuse. `tree_digest` is unchanged
and remains independent of every label; a test pins that clean and dirty provenance over the same
bytes digest identically.

`dirty` describes the SOURCE, not the bundle: it is true when tracked working-tree bytes differ
from `base_commit`, computed with `--untracked-files=no` because untracked files never enter the
bundle and counting them would report nearly every working repository as dirty for content it did
not send. Staged-but-uncommitted changes do count, since the bundler reads working-tree bytes.
**`dirty=false` still does not mean the bundle equals the commit** -- the selection policy drops
excluded and oversized paths and deletions appear separately, so a reader must consult `omissions`
and `deleted_count` too. That is stated in the dataclass rather than left for a server author to
work out.

**Multi-user design correction (2026-09-16):** sources are provenance, not ownership. The receive
protocol still produces immutable snapshots, but graph publication is now a separate promotion into
a server-owned view. This preserves the completed P0/P1 bundle work while removing the designated
uploader bottleneck before server-side receive or graph-write work begins.

Shipped in this pass:

- `src/menhir/snapshot/protocol.py` -- frozen wire contract: canonical manifest, `tree_digest`,
  path rules, limit arithmetic, stable error codes.
- `src/menhir/snapshot/policy.py` -- selection policy (include / declared omission / refusal).
- `src/menhir/snapshot/bundler.py` -- git enumeration, bundle plan, deterministic ZIP.
- `src/menhir/cli/sync.py` -- `menhir sync --check`, local and inspect-only.
- `tests/snapshot/` -- 118 tests: contract, policy, enumeration edge cases, archive determinism,
  and the P1 gate against a real repository.

The transport chicken-and-egg is resolved by splitting P2. P2A may implement the actual
begin/chunk/status/abort path in graph-inert, unadvertised staging mode and probe it through a staging
deployment with the same Streamable HTTP/proxy configuration. It may not extract archives or reach
the graph. P2B remains blocked until those measurements approve the operating envelope. An
`add_memory` padded-body probe may be used only as an early gross-envelope diagnostic; it cannot
pass the gate because its schema validation, decoding, telemetry, and handler allocation differ
from the chunk path. Every current `SnapshotLimits` value remains PROVISIONAL until P2A closes.

In parallel, land structure prune-key B2/#99 (not scheduler CF-99) and then #98's effective identity
generation/CAS. They remain hard prerequisites for P4, but do not depend on P2A/P2B.

`--allow-path` remains an explicit one-run approval and is never persisted per project. A
persistent path approval would silently re-authorize whatever that path contains later; a
hash-bound approval is the eventual answer, path-wide permission is not.

**The corpus ingest was attempted and is blocked -- by this plan's own premise.** Ingesting this
document so its `IMPLEMENTING` lifecycle is auditable requires `ingest_document`, which stats and
reads its path **in the server process**, exactly like `ingest_project`. The configured client is
`https://memory.ctharvey.me/mcp-http`, so a workstation path returns `not a file:
C:\Users\thron\...` and `transition_artifact` reports the UUID as not found because nothing was
ever registered. `artifact_status: IMPLEMENTING` therefore lives in this file's frontmatter and
nowhere else.

**Investigated 2026-09-16; an earlier version of this note overstated it.** The corpus is not
inoperable -- `list_artifacts` returns 12 registered plans and every graph-side read, transition,
supersede and link works normally on an artifact that is registered. What is broken is narrower
and, in one place, worse than a loud failure:

- **Registration fails loudly.** `mcp/tools/ingest/ingest_document.py:82` is
  `os.path.isfile(path)` in the server process -- the same shape as `ingest_project`'s `isdir`.
  A workstation path can never register, which is also why `transition_artifact` reports the UUID
  as unknown: the transition did not fail, the registration never happened.
- **The parity audit fails silently, which is the real finding.** `audit_artifact_corpus` against
  the same unreachable path returns no error: `entries 0 / sources 202`,
  `actions {MARK_SOURCE_UNRESOLVED: 190}`, commit `unknown`, `evidence valid False`. It scanned a
  directory it cannot see, found nothing, and concluded that 190 of 202 known sources are absent
  from the corpus. That is #104's shape exactly -- a completeness answer derived from a tree the
  server cannot observe -- and it is the second time this system has produced one.
- **There is a startup path that would write those marks.** `core/runtime.py:104-128` runs the
  audit and then `apply()` when the reconcile mode is `safe_apply`. The default is `audit`
  (`config/settings_model.py:197-198`), so this needs an operator to opt in. Two things stop it
  from doing damage today, and neither is that someone checked: apply refuses while the Git
  evidence base is invalid (`runtime.py:100-103`, and the live audit reports exactly that), and
  `MARK_SOURCE_UNRESOLVED` is retain-with-a-reason, never delete
  (`domain/artifact_reconciliation.py:1552-1570`). Both are worth keeping deliberately rather
  than by luck.

The corpus therefore has the **same two-sided break as the hook in #111**: the remote path cannot
see the files, and the local path (`menhir artifacts audit|reconcile`, which builds a
`Neo4jRepository` directly at `cli/artifacts.py:52-58`) cannot see the graph, because production
Bolt is sealed. Either half working would close it.

**Documents are deferred (owner, 2026-09-16).** Whether the artifact/document corpus eventually
rides this snapshot path is not a question this plan answers or waits on; it is listed under
Deferred below. Registering this plan stays manual until either half of the #111 break is closed.

What does carry forward is the failure mode, because it is about the snapshot path itself: the
audit's silent zero is exactly what P3's shadow-scan gate must exclude. "Scanned nothing, reported
everything missing" has to be an error there, not a clean parity report -- twice now this system
has produced a confident completeness answer from a tree it could not observe, and P3 is where a
third one would land.

One measured result worth recording: on menhir's own tree (1,738 files, 26.8 MB) a plan takes
0.67 s and the archive is 11.2 MB in 1.3 s.

## Decision

Ship remote structure ingest as a client-created, sanitized filesystem snapshot transferred entirely
through bounded MCP tool calls. The server reconstructs the bundle under a managed root and runs the
existing server-side `ProjectScanner`; callers never upload a `ProjectScanResult` and never address
graph rows by a caller-chosen project name.

The first product contract is deliberately narrow at the transport boundary: each upload creates an
immutable snapshot candidate; the complete bundle is uploaded each time; and publication advances a
server-owned view with compare-and-set semantics. Sources identify where bytes came from but do not
own or replace the project. The canonical view and each developer workspace are independent mutable
pointers to immutable snapshots, so concurrent users do not overwrite or prune one another.
Content-addressed delta transfer and local-scan/privacy mode remain later optimizations.

## Why

`ingest_project` currently stats and scans its path in the server process, so a hosted Menhir cannot
see code on a user's machine. Product users should need only:

```text
menhir connect https://memory.example.com/mcp-http
menhir sync .
```

They should not need SSH, a forge integration, a daemon, a Git remote, a published Bolt port, or a
second local Menhir. MCP is the sole public transport: the CLI packages bytes locally and invokes
the remote MCP server programmatically rather than asking a model to carry bundle content.

## Scope

In scope for v1:

- Git repositories, using the current tracked working-tree bytes (staged and unstaged changes,
  tracked additions and deletions), without `.git` or history.
- A deterministic ZIP bundle containing a canonical manifest and regular files only.
- Resumable, idempotent, bounded base64 chunks over MCP Streamable HTTP.
- Server-side validation, extraction, identity settlement, scanning, graph write, status, cleanup,
  and compensation after a failed write.
- Multiple isolated in-flight uploads per project and source, each producing an immutable candidate.
- A canonical project view plus private workspace views, with one serialized promotion at a time per
  view and no project-wide uploader lock.
- A direct MCP client inside `menhir sync`; no hidden `/api` upload route.

Deferred:

- Non-Git/plain directories until their include/secret policy has separate acceptance fixtures.
- Untracked files; later opt-in only, with a manifest preview and secret-risk refusal.
- Shared feature-branch views beyond the initial private-workspace model; private workspaces and the
  canonical view cover the low-friction team path first.
- Client-produced structure payloads, remote attestation, source-history upload, and forge pull.
- Content-addressed missing-blob negotiation; v1 always uploads one complete snapshot.
- Hosted multi-tenant structure graphs unless structure ownership is enforced on every read and
  write. Until then the feature is eligible only for single-tenant instances.
- Document and work-artifact corpus ingest over this path. `ingest_document` has the same
  server-side path assumption as `ingest_project`, so plans and reviews cannot be registered from
  a workstation either -- but code structure is the problem this plan is scoped to solve, and
  folding a second corpus into it would widen P3 before the first one has shipped. Revisit after
  P5, or when #111 closes the local half.

## Invariants

1. Bundle bytes cross only MCP tools authenticated with `menhir:write`; query-string auth is refused.
2. An upload is bound at creation to the authenticated caller, namespace, project id, and source id.
   Object-addressed follow-up tools re-check that ownership at load.
3. Project identity and prune keys are server-owned `project_id` values. B2/#99 must land before the
   first graph-writing rollout; no snapshot write may prune by display name.
4. The server derives `partial_index` by scanning extracted bytes. The client never supplies it.
5. No bundle content, base64 chunk, manifest path list, or extracted source text enters telemetry,
   logs, errors, traces, or background-warning headers. Chunk tools override `call_payload` with
   upload id, index, byte count, and digests only.
6. Limits are enforced before allocation and again after decoding/extraction: encoded call size,
   chunk bytes, compressed bytes, expanded bytes, file count, path length, and per-client/project
   concurrency and disk quotas.
7. Extraction accepts normalized relative POSIX paths and regular files only. It rejects absolute
   paths, `..`, drive/UNC paths, duplicate or case-colliding paths, links, devices, and archive
   entries not declared in the manifest.
8. A truncated, expired, hash-mismatched, or policy-refused bundle never reaches the scanner.
9. The managed root path is stable across snapshots. A project lease excludes the watcher and a
   second commit while directory swap, scan, write, or compensation is active.
10. The previous materialized snapshot remains available until graph write completion. If a write
    fails after changing graph state, the coordinator restores the previous directory and re-scans
    it; failed compensation marks the project degraded and blocks further commits.
11. An MCP or process restart can resume RECEIVING uploads and can deterministically recover or
    fail every later state; no state is inferred from a directory's mere existence.
12. A committed snapshot is immutable. Graph mutation occurs only by advancing a view from an
    expected snapshot to that committed snapshot; upload completion alone never changes another
    user's query context or the canonical project.
13. Every code-derived memory assertion is grounded to an immutable server-accepted `snapshot_id`
    and verified `tree_digest`. A mutable `view_id`, branch label, or caller-reported commit is never
    its sole historical anchor.
14. Snapshot provenance belongs to the episode/assertion/fact edge that observed it, not as one
    coalesced scalar on a mergeable semantic entity. Multiple observations may support the same
    entity from different snapshots without erasing one another.

## Product and bundle contract

`menhir sync` first resolves the repository root, reads the local project id/source receipt when
present, and builds a manifest without mutating the Git index. Git enumeration supplies tracked
paths; bytes come from the current working tree, so dirty tracked work is visible. Missing tracked
paths represent deletions. Submodules and nested repositories are omissions reported in the
manifest, not recursively copied.

The ZIP contains `snapshot.json` plus `content/<normalized-path>`. `snapshot.json` carries protocol
version, project/source identifiers when known, display name, bundle policy version, optional Git
base commit and commit-tree OID, branch label, clean/dirty state, file count and total bytes, and
per-file path, size, SHA-256, and executable bit. A canonical hash over the actual ordered file
records is `tree_digest`; a second SHA-256 covers the complete ZIP. Git and branch fields are
provenance claims until verified by trusted forge/CI evidence, while `tree_digest` is recomputed
from the uploaded bytes. ZIP timestamps do not decide change detection.

Always exclude `.git`, ignored files, build/cache outputs already excluded by the scanner, and local
Menhir identity/source receipts. Secret-risk paths such as real `.env` files and private keys block
the first sync with an actionable report; examples/templates remain eligible. Overrides are local,
explicit, and included in the policy receipt. Absolute workstation paths and usernames never enter
the bundle.

The server publishes its accepted protocol versions and negotiated limits. Phase 0 measures a safe
binary chunk size through the real MCP/Cloudflare path; 1 MiB is only the initial test point, not an
unreviewed constant. There is no one-call whole-bundle fallback.

### Source classes and project identity

Use one MCP snapshot protocol for all code, but distinguish lineage and provenance when resolving
the logical project and deciding who may advance canonical. The product categories are
provider-neutral rather than GitHub-specific:

| Source class | Identity evidence | Default behavior |
|---|---|---|
| Verified forge checkout | Stable provider repository id verified through an authenticated forge integration or trusted CI assertion | Join the authorized logical project; create a private checkout workspace; permit policy-driven canonical promotion from the verified default branch. |
| Local or unverified Git | Menhir project/source receipt plus Git tree/commit facts; any remote locator is only a self-reported hint | Create or explicitly join a Menhir project; create a private checkout workspace; require an owner/maintainer action or trusted local automation to initialize or advance canonical. |
| Plain local directory | Menhir project/source receipt only | Deferred until its independent inclusion and secret policy ships; then remain private by default and require explicit canonical publication. |

The client reports sanitized source facts; the server assigns the source class and provenance
quality from evidence. A Git remote URL, repository display name, branch name, or commit string is
never authorization and never sufficient to join an existing project. Remote user-info, embedded
credentials, query strings, and workstation paths are stripped before any locator metadata crosses
MCP.

A verified forge id identifies the logical repository, not a user's workspace. Every clone and Git
worktree still receives its own source receipt and private `view_id`. A dirty checkout advances only
that private view. A fork's distinct forge repository id creates a distinct logical project by
default; an upstream relationship may be recorded, but projects are joined only through an explicit,
authorized adoption flow.

This distinction changes onboarding and promotion policy, not bundle format, extraction, scanning,
or graph safety. A local-only project can later attach to a verified forge repository without
rewriting snapshot history: ownership settlement records the new verified lineage and explicitly
adopts the existing `project_id` after conflict checks.

## MCP wire protocol

All responses are compact JSON (`BaseJsonTool`) with stable machine-readable error codes.

| Tool | Scope and effect | Contract |
|---|---|---|
| `begin_project_snapshot` | NAMESPACED, agent write | Accept summary metadata, optional identity action, and an existing source receipt when available. Reuse its authorized private view or atomically mint a source and private view; return `upload_id`, bound IDs, a signed replacement receipt, negotiated chunk bytes, expiry, and identity-decision payload when needed. Allocate quota, not the advertised archive size. |
| `put_project_snapshot_chunk` | OBJECT, agent write | Accept `upload_id`, zero-based index, base64 data, decoded length, and chunk SHA-256. Strictly decode, write at the expected offset, and make exact replay a no-op; conflicting replay fails the upload. |
| `commit_project_snapshot` | OBJECT, agent write | Seal only when all chunks exist, verify ZIP digest, and enqueue durable extraction/validation. Return an immutable `snapshot_id`; do not move any view pointer or mutate a live graph. |
| `promote_project_snapshot` | OBJECT, agent write, `destructiveHint=true` | Advance one authorized view from `expected_snapshot_id` to a committed `snapshot_id`, then enqueue its graph materialization. The CLI may invoke this automatically for the caller's private workspace; canonical promotion follows project policy. |
| `get_project_snapshot_status` | OBJECT, readonly | Return state, received/missing chunks, bounded progress, result counts, partial-index status, and sanitized failure code. Long polling is out of scope. |
| `list_project_views` | NAMESPACED, readonly | Return canonical plus only the caller-owned or explicitly shared views, including kind, display label, active snapshot, provenance quality, and freshness. Never enumerate another user's private workspace. |
| `abort_project_snapshot` | OBJECT, agent write | Cancel an uncommitted upload and remove staged bytes idempotently. A WRITING job cannot be aborted; it must complete or compensate. |

Upload state is durable in a dedicated SQLite store under the Menhir state root; bytes live under a
separate bounded staging root. States are `RECEIVING`, `SEALED`, `EXTRACTING`, `VALIDATED`,
`SCANNING`, `WRITING`, `COMPENSATING`, `READY`, `FAILED`, `ABORTED`, and `EXPIRED`. Every transition
is compare-and-set and records only identifiers, counts, digests, timestamps, and sanitized errors.

The CLI uses an MCP client directly against `/mcp-http`, reuses the configured OAuth/Bearer identity,
queries status to resume missing chunks, and prints upload and server-job progress. Agent-visible
tool descriptions explicitly say that models should not synthesize chunks; normal use is through
`menhir sync`.

## Multiple users and concurrent pushes

Every object is addressed inside an authorization hierarchy, never by project name:

```text
authenticated tenant -> project_id -> view_id -> snapshot_id
                                      \-> source_id -> upload_id
```

`tenant_id` comes only from authenticated request context. `project_id`, `view_id`, `snapshot_id`,
`source_id`, and `upload_id` are server-issued opaque IDs. `source_id` records the checkout/device
that supplied bytes; it is not an authorization or graph-isolation boundary. Ownership is checked
on every upload, status, commit, promotion, and query.

The product exposes views rather than asking users to coordinate publishers:

- Every checkout automatically receives a private workspace view. `menhir sync .` uploads an
  immutable snapshot and advances only that workspace, so two users, branches, or dirty worktrees
  cannot prune one another.
- Each project also has a canonical view, normally advanced by trusted CI on the default branch.
  A project may instead allow maintainers to publish explicitly. Merely naming a branch `main` is
  self-reported provenance and never grants canonical-publish authority.
- Query calls resolve logical `project_id` to the caller's current workspace view by default. Users
  can explicitly request canonical context. Shared agents and background jobs default to canonical.
- Workspace labels such as branch names are display metadata. Durable isolation uses `view_id`, so
  renames, detached heads, duplicate branch names, and forks do not collide.

Concurrency rules:

- Upload and commit are append-only and may run concurrently across users and projects. Identical
  tree digests may reuse validated bytes, but each accepted snapshot retains its own actor/source
  provenance.
- Promotion is serialized per view, not per project. It uses
  `(view_id, expected_snapshot_id) -> snapshot_id` compare-and-set. A competing advance returns
  `VIEW_ADVANCED`; it never silently overwrites. Different workspace views proceed concurrently.
- Exact replay of commit or promotion is idempotent. A stale client can refresh its view and choose
  whether to retry; Menhir does not merge two filesystem snapshots into an incoherent union.
- Canonical promotion requires a project role or trusted automation policy and creates an audit
  record. Deleting a source or expiring a workspace never changes canonical state.

For initial storage, a view may materialize a complete structural namespace. This is intentionally
simpler and safer than overlay composition. Content-addressed bundles avoid duplicate transfer;
structural-node/overlay deduplication can follow only after query and prune equivalence is proven.

### View resolution and automatic workspace assignment

Users do not manually create a workspace in the normal flow. On the first `menhir sync` from a
checkout, the client has no valid source receipt. After authenticating and resolving the logical
project, `begin_project_snapshot` atomically creates:

1. a `source_id` bound to the authenticated principal and that checkout;
2. a private `view_id` owned by that principal and linked to the source; and
3. a signed opaque receipt containing `project_id`, `source_id`, `view_id`, and receipt generation.

The client stores the receipt in local Menhir state excluded from Git and from the bundle. Later
syncs from that checkout present it and reuse the same view. A different clone or Git worktree has
no receipt and therefore gets a different private view. Switching branches inside one checkout
advances that checkout's existing workspace; branch names are labels, not identities. A user who
wants two branches live simultaneously uses two worktrees, which naturally receive two views.

The server validates the receipt against tenant, principal, project, generation, and revocation on
every use. A copied receipt presented by another principal is refused and cannot reveal or advance
the original workspace. Receipt loss creates a new workspace after normal project authorization;
it never guesses by repository path, branch label, machine name, or display name.

Query context is resolved in this fixed order:

1. An explicit, authorized `view_id` or signed context handle supplied by a workspace-aware client.
2. Otherwise the project's canonical view and its last successfully promoted snapshot.
3. If no canonical snapshot exists, return `NO_PUBLISHED_SNAPSHOT` plus bounded onboarding guidance;
   never fall back to the newest upload or another user's workspace.

Generic MCP clients therefore see canonical unless they deliberately supply workspace context. A
local Menhir integration reads the checkout receipt and injects its opaque context handle, so local
agents see that checkout automatically. Authentication alone does not select a user's "most recent"
workspace because one user may have several devices and worktrees.

Every structure/query response includes bounded context metadata: logical project, resolved view
kind, active `snapshot_id`, tree digest, promotion/sync time, and whether provenance is self-reported
or trusted automation. This makes the selected version visible rather than an invisible server
default.

### Code-memory provenance and applicability

A workspace view is a mutable cursor used to select current code. It is not the historical identity
of a memory. Every episode, assertion, or fact derived from code records a provenance receipt with:

- authoritative snapshot evidence: `project_id`, `snapshot_id`, verified `tree_digest`, and
  observation time;
- context-only provenance: `view_id_at_ingest` and `source_id`;
- Git lineage when available: `base_commit_sha`, commit-tree OID, branch label, `dirty`, and
  provenance quality (`self_reported` or `trusted_automation`); and
- anchor evidence for referenced code: normalized path, manifest file SHA-256, durable `file_id`
  when settled, and symbol identity/body digest when available.

For a clean verified CI/forge snapshot, `base_commit_sha` may be a trusted commit mapping. For a
local clean clone it remains self-reported unless independently verified. For dirty code, the exact
identity is `base_commit_sha + tree_digest + dirty=true`; the commit names the base while the digest
names the bytes actually observed. For non-Git code, `snapshot_id + tree_digest` is sufficient.

The existing best-effort `belief_commit` property is retained only as a compatibility projection.
It cannot be authoritative for remote code because it flattens multiple observations onto a
mergeable entity, cannot represent dirty bytes, and may point to a rebased or vanished commit. New
code-memory currentness reads assertion-level snapshot provenance instead.

Recall compares a memory's immutable anchor receipt with the selected view's active manifest:

1. `EXACT_SNAPSHOT` — the selected snapshot is the memory's snapshot.
2. `UNCHANGED_ANCHORS` — the view advanced, but every anchored file/symbol digest is unchanged; the
   memory may carry forward as currently applicable.
3. `CHANGED_ANCHORS` — at least one anchored digest changed; return as stale/needs verification,
   not as unqualified current truth.
4. `REMOVED_ANCHORS` — referenced code disappeared; preserve for historical/postmortem recall but
   gate it from current-code answers.
5. `INDETERMINATE` — omission, partial indexing, missing receipt, or unresolved identity prevents a
   safe comparison; never infer currentness from absence.

Ephemeral-state memories therefore still have value: they preserve failed approaches, debugging
observations, test results, and decisions made against uncommitted code. They begin workspace/session
scoped. Promotion to persistent current-project knowledge requires exact/unchanged validation
against canonical or a deliberate human assertion. Expiring a workspace may delete materialized
bytes, but it retains the snapshot receipt, manifest file digests, and memory provenance needed to
explain historical applicability.

A code-specific memory submitted without an authorized snapshot context is marked
`UNVERSIONED_CODE_CONTEXT`. It may remain session evidence, but it cannot automatically become a
current persistent code belief. The local integration should sync at session boundaries and before
persisting a code conclusion after tracked bytes change; the server verifies that the supplied
snapshot receipt exists and is accessible rather than trusting raw IDs from the caller.

Hosted multi-tenant release has an additional hard gate: every structural entity, edge, query,
identity candidate, project listing, watcher action, and prune must enforce tenant ownership. Upload
isolation alone is insufficient because the current structure graph is shared. Until that gate
lands, snapshot ingest is enabled only on single-tenant Menhir instances regardless of how many
OAuth users those instances have.

## Server integration

Extend the canonical ingest path rather than create a raw graph writer:

1. Commit verifies and seals the archive before queuing work.
2. A bounded worker extracts into `<managed-root>/<project-id>/.staging/<upload-id>` and verifies
   every file against `snapshot.json`.
3. Commit records the immutable snapshot and leaves every view unchanged. Promotion takes a
   per-view lease, checks `expected_snapshot_id`, moves that view's existing `current` to `previous`,
   and materializes the candidate at its stable `current` path.
4. Invoke the existing project scan/write service against the promoted view. Refactor its background write
   so the snapshot coordinator receives durable completion instead of guessing from an early
   response.
5. On success, record tree/bundle digests, source/Git provenance, scanner version, counts,
   partial-index state, and `last_synced_at`; retain the immutable snapshot receipt and manifest
   file digests even when bundle/materialized bytes expire, then apply the byte-retention policy.
6. On failure after swap, restore `previous` and scan/write it as compensation. Block the project
   if compensation does not complete.

For managed snapshots, incremental structure diffing must use verified manifest file digests, not
archive extraction mtimes. The project node records the stable managed root plus source/snapshot
metadata; workstation paths are display-only and are not accepted from this protocol.

## Progressive delivery

### P0 — Freeze the protocol

- Add contract tests/fixtures for canonical manifests, ZIP determinism, base64 expansion, and limit
  failures.
- Before P2B, replace the ambiguous `source_head`-only provenance with base commit, commit-tree OID,
  branch label, clean/dirty state, and provenance-quality semantics. Keep `tree_digest` independent
  of these labels and authoritative for uploaded bytes.
- Define the 64 KiB, 256 KiB, 1 MiB, and 2 MiB decoded-chunk probe matrix and required measurements;
  execution moves to P2A because only the real chunk handler can produce acceptance evidence.

**Gate:** approved protocol v1 permits P1 and a graph-inert P2A staging receiver only; it does not
permit a production receive feature, extraction, or graph access.

### P1 — Local bundler, inspect-only

- Add `menhir sync --check` to produce the policy report, manifest summary, archive estimate, and
  refusal list without creating a durable archive or making a network call.
- Add deterministic bundler tests across Windows/Linux path rules, dirty tracked files, deletion,
  case collisions, ignored files, secrets, nested repos, and oversized files.
- Capture Git lineage without mutating the index; prove that a clean checkout and dirty checkout at
  the same HEAD have different `tree_digest` identities while sharing the same base commit.

**Gate:** repeated runs over identical bytes produce the same tree digest; repository/index status is
byte-identical before and after.

### P2A — Real-handler transport measurement, staging only

- Implement only begin/chunk/status/abort behind an unadvertised, operator-allowlisted staging
  mode. Use bounded temporary storage, the provisional hard ceiling, synthetic non-secret bytes,
  and the same MCP middleware, auth, proxy, telemetry, and request parser intended for release.
- Probe 64 KiB, 256 KiB, 1 MiB, and 2 MiB decoded chunks repeatedly through the staging
  Streamable HTTP ingress. Record encoded call size, success/retry rate, p50/p95 latency, peak
  process memory, staging growth, telemetry redaction, and failure behavior at the first rejected
  rung.
- Select the default chunk one rung below the largest repeatedly stable size, preserving at least
  2x envelope headroom. If 2 MiB is stable, use 1 MiB; if 1 MiB is the ceiling, use 256 KiB. Do not
  infer this result from a different MCP tool.
- Approve quotas separately from the ingress result: pilot candidates remain 64 MiB compressed,
  256 MiB expanded, and 20,000 files per upload, with at most two RECEIVING uploads per principal,
  eight per project, a 24-hour inactivity TTL, and one-hour retention of terminal staged bytes.
  Soak tests must prove the configured tenant disk budget refuses new begins before exhaustion.

**Gate:** measured chunk default and hard ceiling are recorded; quota arithmetic and TTL cleanup pass
restart/disk-pressure tests; no archive extraction or graph operation is reachable. Only then may
the provisional marker be removed and P2B start.

### P2B — Durable MCP receive substrate, feature disabled

- Implement begin/chunk/status/abort and commit-through-SEALED only, behind one registered feature
  mode (`off`, `receive`, `shadow`, `write`).
- Add durable upload records, staging quotas, expiry cleanup, restart recovery, exact-replay tests,
  conflicting-replay refusal, and telemetry/log redaction tests.
- Register the tools, metadata, OAuth scopes, tenancy declarations, allowlists, endpoint catalog,
  and backend protocol/client forwarding where supported. MVP CLI requires direct remote MCP and
  reports a clear error for an unsupported backend-first proxy path.

**Gate:** adversarial bundles can consume only configured resources; no archive is extracted and no
graph operation is reachable.

### P3 — Safe extraction and shadow scan

- Add the isolated extractor, manifest verification, managed-root containment, project lease, and
  bounded worker.
- In `shadow` mode, scan extracted content and return counts/fingerprint/partial status without
  identity adoption or graph writes; delete the materialized snapshot after the report.
- Compare shadow results with a direct local scan over a fixture corpus and representative real
  projects.

**Gate:** structural parity is explained for every fixture; malformed archives and interrupted jobs
leave no live root and are reclaimed after restart.

### P4 — Canonical-view graph write pilot

- Land B2/#99 and the effective identity-generation/CAS fix before enabling this phase.
- Add project/source enrollment and ownership-at-load; sources contribute immutable snapshots but
  no source is designated as project owner.
- Add `promote_project_snapshot`, canonical publish roles/policy, per-view CAS and leases, stable
  current/previous roots, digest-based incremental file comparison, write completion, compensation,
  degraded-view blocking, and `[SNAPSHOT ...]` status in structure queries.
- Pilot one new disposable project, then one adopted existing project, with operator-only access.

**Gate:** deletion, failed write, process kill at every state, and failed compensation have tested
outcomes; the old project graph can be restored from `previous`; no name-keyed prune remains.

### P5 — Private workspace views

- Add a structural namespace key for every structural entity, edge, read, watcher action, and prune.
  Map `(tenant_id, logical project_id, view_id)` to that namespace without trusting caller labels.
- Automatically create one private view per source/checkout and make it the default context for
  workspace-aware queries carrying its receipt. Keep canonical as the deterministic fallback for
  generic, shared, and background agents.
- Add view quotas, inactivity expiry, explicit reset/delete, source and branch relabeling, and tests
  proving that identical paths in two views cannot cross-read, cross-anchor, or cross-prune.
- Add receipt issue/rotation/revocation and context-resolution tests for zero, one, and many
  workspaces per principal. No-context queries must remain canonical in every case.
- Stamp code-derived assertion/fact provenance with immutable snapshot receipts; retain
  assertion-level multiplicity when semantic entities merge. Add manifest-digest comparison for
  `EXACT_SNAPSHOT`, `UNCHANGED_ANCHORS`, `CHANGED_ANCHORS`, `REMOVED_ANCHORS`, and `INDETERMINATE`.

**Gate:** two users can sync divergent complete trees concurrently, each reads their own structure,
canonical remains unchanged, deleting either workspace leaves the other two views intact, and a
memory from one workspace cannot appear as unqualified current truth in another after its anchors
change.

### P6 — Product release

- Enable agent-tier use on single-tenant instances; keep hosted multi-tenant instances disabled
  until structure read/write ownership is complete.
- Finish `menhir sync`, OAuth onboarding, resumable progress, `--check`, explicit identity decision,
  verified-forge versus local-Git classification, opt-in session-start sync, status/help, quotas,
  and operator cleanup tooling. GitHub may be the first verification adapter, but no core identity
  or snapshot contract is GitHub-specific.
- Roll out `write` mode by allowlisted client/project, then instance-wide after a clean observation
  window. Keep legacy local-path `ingest_project` for local deployments; deprecate remote use of it.
- Surface the resolved snapshot/commit and applicability verdict in code-memory recall. Require an
  authorized snapshot context before automatic promotion of a code-specific memory to persistent
  current-project knowledge.

**Gate:** install-to-first-sync succeeds from a clean PyPI environment; release documentation states
what is uploaded and retained; production metrics show bounded staging, scan latency, failure rate,
and zero bundle content in telemetry; clean, dirty, rebased/vanished-commit, changed-file,
renamed-file, removed-file, partial-scan, and non-Git memory fixtures all produce conservative,
explainable applicability verdicts.

### P7 — Scale and broader sources

- Add manifest-first MCP negotiation so the server requests only missing content-addressed blobs;
  retain the v1 whole-bundle path as fallback.
- Add plain-directory and opt-in untracked policies only with independent secret/omission fixtures.
- Add opt-in shared branch views only after role, retention, and concurrent-promotion behavior has
  production evidence from private workspaces. Do not infer shared scope from a branch label.

## Alternatives considered

- **One giant base64 MCP argument:** fewer tools but no bounded memory, resumability, or safe retry;
  rejected.
- **Separate HTTPS upload endpoint:** efficient binary transfer, but violates the requirement that
  MCP be the product transport and expands public ingress; rejected for v1.
- **Git bundle/smart HTTP:** good Git delta transport, but Git-only, carries more server machinery,
  and does not solve project authorization; optional future adapter, not the core contract.
- **Client-produced scan payload:** lower bandwidth and better source privacy, but makes destructive
  completeness caller-controlled; deferred until immutable/versioned observations exist.
- **Long-lived client daemon:** adds supervision, upgrade, and enrollment burden without improving
  payload trust; rejected.

## Validation and observability

- Unit: canonical manifest/tree digest, selection policy, path normalization, limit arithmetic,
  chunk idempotency, ownership, expiry, state transitions, and sanitized errors.
- Integration: real MCP Streamable HTTP upload with OAuth; restart/resume; duplicate/out-of-order
  chunks; ZIP corruption; extraction attacks; scan parity; identity decision; successful write and
  compensation against disposable Neo4j; first-sync workspace assignment; copied/revoked receipts;
  canonical fallback; concurrent divergent views for the same logical project; forged remote
  locators; verified forge joins; fork isolation; and local-to-forge project adoption.
- Deployment: managed/staging mounts and permissions, read-only root filesystem retained, no new
  server egress, disk-pressure refusal, cleanup after hard kill, and Cloudflare-path measurements.
- Metrics: active uploads, staged bytes, expiry/abort/failure counts, state age, extraction ratio,
  scan/write duration, compensation and degraded-project count. Labels never contain project paths,
  filenames, digests with user meaning, or source content.

## Rollback and stop conditions

- `off` removes the tools from advertised capability and refuses direct invocation; cleanup retains
  only bounded expired staging data.
- `receive` and `shadow` are graph-inert and may remain deployed indefinitely while evidence is
  collected.
- Disabling `write` stops new promotions but still permits bounded receive/validation when that mode
  remains enabled; it does not remove existing structure. Local-path ingest and structure queries
  remain available.
- A degraded view is fail-closed: reads carry a warning, uploads may still become immutable
  candidates but cannot be promoted into that view, and repair is an explicit operator action.
- Any evidence of content in logs/telemetry, cross-project mutation, quota bypass, or an
  uncompensated partial graph write returns the feature to `shadow` or `off`.

## Resolved owner decisions (2026-09-16)

1. Measure the real chunk handler in graph-inert staging via P2A; `add_memory` is diagnostic only.
2. Start P2A with the bounded pilot quota/TTL candidates in that phase and approve or revise them
   from soak and disk-pressure evidence, independently of the HTTP envelope result.
3. Land structure prune-key B2/#99 before #98; both may proceed in parallel with P2A and gate P4.
4. Keep `--allow-path` one-run only. Do not persist secret-risk upload approval per project.
5. Ingest this plan after the decision update so IMPLEMENTING status is visible to corpus audits.

## Open owner decisions

1. The measured chunk default and hard ceiling produced by P2A; this is an evidence result, not a
   pre-measurement preference.
2. Whether the current materialized view snapshot is retained until superseded (recommended for stable
   staleness and delta support) or deleted immediately at the cost of a new remote-staleness model.
3. Whether snapshot tools remain hidden from model-facing catalogs when only the CLI should call
   them, or are advertised with strong “use `menhir sync`” guidance.
4. Whether hosted Menhir is one tenant per instance or must block P6 on full structure-graph
   namespace ownership.
5. Canonical promotion policy: trusted CI only (recommended default), explicit maintainer publish,
   or both with separately auditable grants.
6. Private workspace quota, inactivity TTL, and whether users may pin selected workspaces.
7. Whether GitHub verification is required for the first hosted release or launches later while all
   Git checkouts initially use the safe local/unverified path.

## Docs to update

- `.agent/architecture.md`, `.agent/data_models.md`, `.agent/endpoints.md`, `.agent/mcp-tools.yaml`
- `.agent/tasks-ingest.md`, `.agent/tasks-mcp.md`, `.agent/edge-case-testing.md`
- `docs/post-install.md`, `docs/agent-usage.md`, README and security/privacy documentation
- deployment authority/compose docs for managed roots, quotas, cleanup, and feature mode
- `.agent/CHANGELOG.md` at each shipped phase
