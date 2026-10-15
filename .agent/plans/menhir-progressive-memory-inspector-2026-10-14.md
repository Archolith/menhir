---
artifact_schema: 1
artifact_uuid: 294fc49b-707b-450b-8910-1ae88db2b331
artifact_type: plan
artifact_status: PROPOSED
---

# Progressive Memory Inspector — on-demand memory drilldown

Date: 2026-10-14
Repository: `Archolith/menhir`
Planning branch: `feat/progressive-memory-inspector`
Status: proposed; no implementation authorized by this document.

## Why

The Explorer already shows node details, a small neighbor list, linked episodes, and a graph, but it does not provide a cohesive path from a compact memory statement through supporting evidence, belief history, and related memories. Users must switch context or accept a flat information dump. Borrow the **interaction idea** of [cc-stretch](https://github.com/pro-vi/cc-stretch): keep the initial answer compact; expand its supporting detail *in place*.

The Menhir-specific opportunity is to use existing structured evidence and graph edges rather than precompute long-form prose. **Expand** (inspect the same memory) and **Traverse** (follow another memory) are distinct actions. Both are read-only, lazy, and deterministic by default.


## Typed View and Scalar specialization

The general memory reader does not replace purpose-built [View Studio](../../docs/prototypes/memory-atlas-v2/views-scalars.html#views) and [Scalar Explorer](../../docs/prototypes/memory-atlas-v2/views-scalars.html#scalars) screens (synthetic prototypes only).

- **Views:** preserve view_kind, view_class, view_audience, view_current, view_key, version/supersession and live provenance. A current history View is not current-state authority; operator-only Metrics cannot become recallable claims.
- **Scalars:** show canonical slot identity (subject_uuid, attribute, scope, value_kind, unit), the current scalar_state if anchored, separate advisory scalar_history, typed operations, source-time/learned-time and actual contributor assertion/TurnEvidence/episode links.
- **Abstention:** delta-only histories must show unknown current value, never a fabricated absolute total. Distinguish disabled/unmaterialized, absent anchor, redacted evidence, expired source and genuine empty result.
- **Reuse:** draw on ViewKind and ScalarViewRepositoryMixin read contracts, and the existing Recall Lab projection, not a new durable View type, storage system, or read-time LLM. Do not enable default-off lanes for presentation.
- **UI acceptance cases:** anchored value, delta-only history, scalar-history operator audience, FACT View with live support, unsupported/orphaned View, superseded View, event occurrence-only semantics and operator METRIC.

The two new screens require an explicit backend security/data contract review before being connected to production.

## Experience prototype

The initial one-page reader prototype is now accompanied by the [five-page Memory Atlas experience study](../../docs/prototypes/memory-atlas-v2/index.html) ([walkthrough](../../docs/prototypes/memory-atlas-v2/README.md)): Overview, Memory Reader, Evidence Trail, Temporal History and Relationships. This is a proposed *greenfield layout*, with synthetic records and no runtime/backend connection. Review layout, state semantics, cross-view navigation and accessibility before treating any current Explorer screen as a constraint.

## Scope

**MVP: Menhir-backed Memory Atlas, with a greenfield visual frontend.** The existing Explorer was an early prototype: its three-column graph-first arrangement, templates, and styling are not binding. Only Menhir's canonical runtime/read contracts, governance, privacy, and essential operator capabilities are constraints. See the [grounded UX study](../../docs/design/memory-atlas-ux-research-2026-10-14.md) and [interactive concept](../../docs/prototypes/progressive-memory-inspector.html).
- A compact memory card with a stable identity and type/authority indicators; inline expansion to full details, chronology, source episodes/receipts, and evidence links when available.
- Clickable relationship previews with explicit direction, relationship type, and target identity; follow to a new card with breadcrumb/back navigation.
- Layer-level loading/error/empty/redacted states; collapse/re-expand without losing the current navigation path.
- Support Entity and Episodic nodes first; detect current/historical View nodes and label their authority correctly rather than treating a materialized View as original testimony.
- Stable deep links to authorized memory UUIDs; pointer and keyboard interaction; reduced-motion animation preference.
- No LLM invocation on opening or traversing a memory.

**Not MVP:** writing or fixing memories, new graph labels/edges/storage, public MCP/REST surface changes outside Explorer, unbounded graph visualization, cross-client UI protocols, auto-generated explanations, or retrieval-ranking changes. Extending Recall Lab and MCP clients is a later, separately reviewed phase.

## Proposed Design

### 1. Reuse existing runtime and provenance

- Extend `src/menhir/explorer/` and its existing `/explorer` router mounted by `explorer/integration.py`; reuse the shared runtime repository, auth, and privacy handling. Do **not** start a second graph pool or service.
- Add a small inspect projection/service over canonical infrastructure, not custom Cypher embedded in route handlers. Reuse or extend `MemoryGraphAdapter`, `memory_queries.py`, and existing `fetch_node_receipts` for source evidence. Verify the true shape of View contributors, temporal metadata, and Graphiti episode UUID versus Menhir receipt UUID before implementation.
- Explicitly distinguish original episode content, derived entity summary, View assertion, historical/superseded value, and source evidence. Unknown or missing evidence is `unavailable`, never silently reconstructed.
- Preserve original UUID, source kind, supporting UUIDs, timestamps with world-time vs learned-time meaning, and available reference/commit path. Link only to sources the caller may access.

### 2. Bounded read contract

An Explorer-internal versioned `memory-inspector/v1` response, with two distinct read shapes:
- `GET /explorer/api/inspect/{uuid}?layer=summary|detail|evidence`: one requested layer; type, authority/currentness, provenance and temporal metadata according to authorization. A summary must not fetch or serialize the full graph.
- `GET /explorer/api/inspect/{uuid}/relations?cursor=...&limit=10`: one page of related nodes with typed/directed edges and stable ordering; cursor is scoped to requester, namespace, source UUID, and query shape.

Contract details to finalize in the API design review: exact field names, handling of duplicate edges, edge types eligible for traversal, and authorized historical/audit inspection. Do not invent an alternative ownership or visibility model for the inspector.

Hard limits: max 10 relation/evidence items per default page (server cap 50), one-hop requests, no recursive fan-out, parameterized queries, index-backed UUID lookups, no retrieval-score or last-accessed mutation. Clicking a related memory begins a new one-hop query. Invalid/mismatched cursors fail closed. Limit payload bytes and prevent N+1 queries.

### 3. Security and correctness gates (before UI work)

- Apply existing Explorer readonly-tier auth and the canonical namespace/tenant predicates at **every** node and edge expansion; never trust a client-provided UUID to authorize graph access. Cross-namespace edges must not reveal target names, existence, counts, or content.
- Reconcile current Explorer `_node_detail`'s raw UUID lookup with canonical visibility rules before reusing it. Generic user-facing views exclude candidates, retired/internal/orphaned Views; any historical/audit reveal requires explicit higher privilege and a separate reviewed policy.
- Honor `privacy_redact` and loopback-only reveal behavior for JSON, HTML, previews, source excerpts, nested records, and deep links. A collapsed card must not leak a hidden long form in markup, logs, telemetry, hover text, or browser history.
- Explicit erasure/supersession must invalidate previously reachable details; a live read rechecks current evidence rather than treating stale receipt UUID strings as proof. Avoid persistent client-side caching in MVP.

### 4. UI integration

- **Redesign the full Explorer screen as a reading-first Memory Atlas**, rather than adapting the existing 400px detail sidebar. Treat `base.html`, `_detail.html`, `explorer.js`, and current CSS/layout as replaceable; the canonical backend router and privacy/authorization contracts remain. Central reading column displays a compact, authoritative memory; optional context rail previews its one-hop neighborhood, and Graph/Timeline are dedicated views. See the UX study and offline prototype.
- Separate **Expand** from **Follow**. Expand maintains the current card identity; Follow changes the focal memory and pushes breadcrumb history. Include visible source/authority badges and a concise “no supporting evidence found” state.
- Load a section only upon expansion (AbortController/cancel stale requests when switching nodes); render text safely, without injecting untrusted memory content as HTML. Offer loading, retry, permission-denied, deleted, and unavailable states.
- Preserve the *capabilities* of graph selection/navigation and necessary operations, but not the old graph-first default or DOM structure. Provide a clear optional full-graph mode, responsive single-column reading mode, focus and scroll restoration, keyboard access, and `prefers-reduced-motion`. Animation is an optional 150–240ms enhancement; textual anchors are preserved only when the actual expanded source content supports them.

### 5. Delivery slices

1. **Contract/security spike:** verify actual current View/evidence shapes and authorization predicates, examine `_node_detail` and `fetch_node_receipts` limits, define fixtures for Entity/Episodic/View, historical, erased and cross-tenant cases. Record unresolved policy choices.
2. **Read-only service + routes:** deterministic, bounded inspect and relation pages, schema tests, privacy/auth/tenant regression tests, no LLM or graph mutations.
3. **Greenfield Memory Atlas interaction:** review the prototype against concrete visual/interaction criteria, then build progressive sections, follow/back, optional graph view, keyboard/reduced-motion and request cancellation. Port the existing privacy controls, graph capabilities and candidate moderation intentionally; do not inherit the old layout.
4. **Validation and review:** focused tests, disposable Neo4j integration checks by explicit opt-in, browser walkthroughs using representative records, before/after timing and payload measurements; propose separate Recall Lab/MCP adaptation only after the Explorer contract stabilizes.

## Alternatives Considered

- **Pre-generate long-form text with an LLM, cc-stretch-style:** fast to prototype but costs inference even for unopened sections and weakens source traceability. Reject for default behavior.
- **Fork/use cc-stretch directly:** its TypeScript renderer hooks Claude Code's UI, not Menhir's browser. Source review: `grow.ts` uses LCS to preserve word anchors while revealing intervening tokens; `reply.tsx` uses a 16ms terminal frame clock and ~0.45–1.4s eased growth; `layout.ts` hand-wraps terminal markdown. Port none of this terminal rendering logic or fuzzy alignment over arbitrary evidence. Reuse only the reader-orientation principle with browser-native semantic text/disclosure. See the [source-level design study](../../docs/design/memory-atlas-ux-research-2026-10-14.md).
- **Build a new standalone front end or graph service:** duplicates existing runtime, auth and Explorer responsibilities. Reject.
- **Expose a generic new MCP tool immediately:** premature API support burden. Defer until the read projection is tested and cross-client demand is concrete.

## Risks

- Current Explorer node detail and graph queries can expose raw graph shape by UUID; blindly adding new layers magnifies a pre-existing security boundary. Authorization/namespace review is a prerequisite.
- Graphiti episodes, Menhir ingestion receipts, and derived Views are not interchangeable IDs or authorities.
- The same source can support several statements; cycles and large neighborhoods require cursors and explicit query budgets.
- Evidence deleted by privacy erasure may leave historical receipts; never represent a missing source as verified.
- Inline animation and partial updates can cause accessibility regressions or race conditions under rapid navigation.

## Invariants

1. All MVP reads are deterministic, read-only, and model-call-free; production recall, ingest and lifecycle output remain unchanged.
2. Same effective identity, tier, namespace, privacy and visibility restrictions as canonical Menhir surfaces; no cross-tenant hints.
3. A View's derived claim is never displayed as if it were direct source testimony; historical state is never labeled current.
4. Missing, invalidated or erased provenance is explicitly marked; no invented sources or times.
5. Read work is bounded by one-hop limits, stable ordering, pagination, and response budgets.
6. UI design is unconstrained by the historical inspector layout; all existing operational capabilities that remain required have a documented replacement and regression test. The initial memory statement and authority/status indicators remain readable without opening a panel.
6. Existing Explorer graph, candidate moderation, and privacy-toggle functionality do not regress.

## Validation

- Unit/contract tests for all four expansion levels and every memory shape, absent source and empty relation page, mixed/tied edge ordering, invalid cursor and stale navigation response.
- Auth, tier, redaction and namespace tests: direct UUID guess, cross-tenant link, anonymous/insufficient tier, masked nested evidence, non-loopback reveal, stale/erased and historical View.
- Neo4j integration tests (explicit opt-in) asserting bounded reads, no graph writes, correct Menhir receipt vs Graphiti UUID mapping and stable pagination.
- UI tests for expand/collapse/follow/back, keyboard/focus, reduced motion, rapid node switch, loading/error, and safe escaping of hostile memory text. Visual reviews at 360/390/768/1280/1440px, 200% text zoom, historical/redacted/source-less/high-degree records. Compare layouts to the [prototype](../../docs/prototypes/progressive-memory-inspector.html); verify semantic authority, not just aesthetic similarity.
- Track query count, p50/p95 latency and payload sizes per layer with no memory-text telemetry; require a bounded one-hop budget before release.
- Follow `.agent/workflows/run_and_test.md`: focused tests + matching lint locally, then required CI on exact published SHA; do not claim unrun tests passed.

## Docs To Update

`.agent/architecture.md` (Explorer integration), `.agent/endpoints.md` (internal route contract), `.agent/data_models.md` only if introducing a shared projection DTO, `docs/` operator Explorer usage, `CHANGELOG.md`, and this plan/index. The visual direction and research are in [`docs/design/memory-atlas-ux-research-2026-10-14.md`](../../docs/design/memory-atlas-ux-research-2026-10-14.md), with a fixture-only HTML concept in [`docs/prototypes/progressive-memory-inspector.html`](../../docs/prototypes/progressive-memory-inspector.html). No migration or public API contract is anticipated for MVP.

## Decisions Needed Before Implementation

- Decide whether this is a read-only operator debugging view alone, or whether an ordinary readonly-tier user may inspect raw episodes/source evidence; explicitly separate audited history from normal recall visibility.
- Finalize namespace context selection and permission handling for deep links.
- Decide whether to show derived View history in the same card or behind a separately labeled **Historical** expansion; preserve time-basis semantics either way.

Source inspiration: [pro-vi/cc-stretch](https://github.com/pro-vi/cc-stretch) (MIT-licensed). This plan borrows the interaction model only; no source code is copied.
