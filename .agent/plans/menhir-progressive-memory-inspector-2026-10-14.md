---
artifact_schema: 1
artifact_uuid: 294fc49b-707b-450b-8910-1ae88db2b331
artifact_type: plan
artifact_status: PROPOSED
---

# Memory Atlas — Progressive Memory, View, and Scalar Exploration

Date: 2026-10-14
Repository: `Archolith/menhir`
Branch: `feat/progressive-memory-inspector`
Status: **PROPOSED — implementation has not started.**
Revision: implementation-ready refinement of the original Progressive Memory Inspector plan.

## 1. Why and product objective

Make Menhir memories explorable rather than merely searchable: read the short statement, expand its details, inspect source evidence, follow relevant relationships, and explain how derived Views and typed scalar values were produced. The first screen is a quiet, reading-first **Memory Atlas**, not the legacy graph-first Explorer. A click should distinguish **Expand** (more about this same claim) from **Follow** (open another record).

**Three first-class experiences:**

1. **Memory Reader:** Entity/Episodic content and source-linked summaries, expanding in place; optional Evidence, Timeline, and Graph contexts.
2. **View Studio:** View kind/class/audience/currentness and its Observe → Fold → Project pipeline, exact contributor edges, supersession, missing evidence, and a typed presentation of each kind.
3. **Scalar Explorer:** Subject+slot identity, canonical current scalar_state (if lawful), separate advisory scalar_history, operation-qualified assertion ledger, and explicit abstention/expiry/conflict outcomes.

**Not this feature:** a project-status dashboard or a `CurrentPlanView`/`ImplementationProgressView`. A future project-level briefing (latest status plus plans, reviews, implementation steps) requires a separate plan and authority model. Do not infer WorkArtifact relations from prose to make this Atlas look complete.

### Existing work and authority sources

- Visual prototypes: [five-screen Atlas](../../docs/prototypes/memory-atlas-v2/index.html), [View Studio + Scalar Explorer](../../docs/prototypes/memory-atlas-v2/views-scalars.html), [earlier single-page concept](../../docs/prototypes/progressive-memory-inspector.html). **All use synthetic data**; none is a connected Menhir implementation.
- Grounding: [Memory Atlas research and UX decisions](../../docs/design/memory-atlas-ux-research-2026-10-14.md), [prototype walkthrough](../../docs/prototypes/memory-atlas-v2/README.md), [cc-stretch](https://github.com/pro-vi/cc-stretch) as interaction inspiration only.
- Current implementation sources to reuse: `src/menhir/explorer/app.py` and `integration.py`; `infrastructure/memory_queries.py`, `view_models.py`, `view_query_repository.py`, `scalar_view_repository.py`; `domain/scalar_state_fold.py` and `scalar_history.py`; `services/scalar_state_service.py`; `explorer/recall_packet_prototype.py`.
- Read-side visibility and lifecycle policies remain owned by existing Menhir governance/authority services. Graphiti episode UUID, Menhir receipt UUID, TypedAssertion ID, TurnEvidence turn ID, and a materialized View UUID are distinct identities.

## 2. Scope and user journeys

| Surface | First paint | User action | What actually loads |
| --- | --- | --- | --- |
| Overview / discovery | Searchable, scoped memory summaries, type and currentness | Filter, select, search | Bounded summary records, not body/evidence/graph |
| Memory Reader | Exact or explicitly labeled derived statement; authority and caution visible | Expand Details; Evidence; History; Related | Requested layer only; compact source previews |
| View Studio | Registered View kind, ViewClass, ViewAudience, status and typed display | Observe → Fold → Project; inspect contributor; historical version | Kind-specific projection and paged evidence, not fuzzy reconstructed claims |
| Scalar Explorer | Canonical slot and lawful current value **or reason unavailable** | Open calculation, source assertion, advisory history | Existing fold verdict; paged contributing assertions and receipts |
| Evidence Trail | Derived assertion → contributor → source identities | Expand original quote, follow receipt | Authorized source excerpt and provenance links |
| Temporal History | Current vs superseded clearly separated | Switch learned/world clock | Recorded dates only; unknown remains unknown |
| Relationships | Selected memory and one-hop neighbors | Follow, filter edge types, request additional page/hop | Bounded, typed, directed graph reads |

The legacy three-column layout is **disposable UI**. The shared backend runtime, access control, operator workflows and regression contracts are **not disposable**. Graph/Timeline are optional alternate views, not the default home screen. On mobile prioritize a single reader column; side rails collapse into navigation.

### Required visual interactions

- Stable inline text expansion only when the expanded content contains the displayed source words in order. Otherwise add a separate explanation block below; **do not LCS-align unrelated source passages** to fabricate continuity.
- No model call on opening, revealing, following, changing a filter, or opening a View derivation. Complex explanations can be proposed later as an explicit, source-backed opt-in.
- Visible type/source/currentness warning even with all sections collapsed. Use semantic buttons, focus return, aria-expanded, keyboard navigation, 200% zoom, responsive layouts, and reduced-motion.
- Back/breadcrumb path preserves record focus and restores reasonable scroll; stale network responses cannot overwrite a newly selected record.

## 3. Data contracts and source-of-truth rules

### A. A small, versioned read projection

Before UI wiring, define a typed `atlas-inspection/v1` response contract with **discriminated** variants rather than one generic properties bag:

- Common envelope: `schema_version`, stable ID, namespace-safe identifier, `record_kind`, exact source/derived text, `visibility`, `authority_role`, `currentness`, available layers, typed time fields (`valid_at` = world time; `learned_at` = ingest/belief time), and source availability state.
- `entity` / `episode` variants: actual kind, source identity and safely exposed content; episodes are not silently described as derived Views.
- `view` variant: `view_kind`, `view_class` (FACT/METRIC), `view_audience` (RECALL/OPERATOR), `view_current`, key, actual display/payload, reducer/derivation descriptor, contributor identities with explicit edge roles, and supersession/version information.
- `scalar_slot` variant: canonical subject UUID + attribute, scope, value_kind, unit; optionally linked scalar_state and scalar_history View UUIDs; fold result (`current`, `no_anchor`, `ambiguous_anchor`, `delta_on_range`, `expired`, or other distinct failure/unavailable state); when current, value/display/as-of and exact contributors.
- Layer responses are small projections; source content and graph neighborhoods do not accompany summaries by default. `unknown`, `not_materialized`, `feature_disabled`, `not_authorized`, `redacted`, `erased`, `no_anchor`, `ambiguous`, `expired`, and `transport_error` must not be collapsed into a fictitious zero or “no data.”

The contract is **proposed**, not an already shipped public API. Ratify field names, aliases, error format, audience permissions, and namespace semantics in P0. Avoid claiming a source quote is available until its live provenance can be resolved.

### B. Internal Explorer routes (P0 design candidates)

Extend the **existing** `/explorer` FastAPI router and shared repo, not a second service. Candidate read-only endpoint family:

- `GET /explorer/api/atlas/items?query=&kind=&cursor=&limit=` — scoped discovery.
- `GET /explorer/api/atlas/items/{uuid}?layer=summary|detail|evidence|history` — one authorized item projection.
- `GET /explorer/api/atlas/items/{uuid}/relations?cursor=&limit=` — one-hop, typed/directed edges.
- `GET /explorer/api/atlas/views/{uuid}?layer=summary|derivation|contributors|versions` — View Studio.
- `GET /explorer/api/atlas/scalars/{subject_uuid}/slots` and `GET /explorer/api/atlas/scalars/{subject_uuid}/slots/{slot_id}?layer=state|history|contributors` — canonical slot inspection (slot_id is server-derived, namespace-bound; never constructed by joining untrusted strings).

These are **private Explorer read routes**, not new public MCP tools or persisted schemas. Require consistent errors, explicit query bounds, stable total ordering and continuation tokens bound to the effective identity/namespace/filter. Default page size 10, server cap 50. One hop per request, no recursive traversal or unbounded N+1 evidence loading; cap response bytes and constrain text snippets. Actual paths may be simplified during contract review if this avoids redundant handlers.

### C. View-kind mapping — not a “generic View = fact” assumption

| Kind or class | Authority/presentation | Grounding and special cases |
| --- | --- | --- |
| `scalar_state` FACT View | Candidate **current typed state** if authoritative fold, foundation, visibility and feature gates pass | `CURRENT_ANCHOR`, applied `CONTRIBUTED_TO`, excluded `SUPERSEDED_ANCHOR`; no inferred values |
| `scalar_history` FACT View | **Advisory change history** even if the history projection itself is current; OPERATOR by default unless explicitly recallable | Ordered `HISTORY_ENTRY` assertions; may have no absolute anchor; pagination must not change fold authority |
| `timeline` event lane | Dated **occurrences**, not present ownership/state | `EVENT_HISTORY_ENTRY` / typed event source, predicate/domain, source valid_at. Legacy subject-only timelines have different default audience |
| `counter` | Kind-specific current register, not a canonical scalar slot | Preserve existing counter semantics and suppression/supersession; do not equate with typed scalar_state |
| FACT/RECALL, FACT/OPERATOR, METRIC/OPERATOR | Distinct storage and audience lanes | Metrics are operator instrumentation, not semantic recall; operator-only rows are never surfaced to ordinary reader without policy authorization |
| Unknown/internal/stale/retired/unsupported | Explicit status or blocked inspection | No “generic current fact” fallback, no source fabrication; old Views require explicit audit authorization |

**Current projection is not current belief authority**. `view_current=true` on advisory history means this is the latest history projection, not an authoritative head. No summary, badge or graph styling may blur the distinction.

### D. Scalar correctness is delegated to Menhir

Use `fold_assertions` / `ScalarStateService` and source assertions; **the browser never folds values**. Display the official result (if safe for this caller) and explain exact contributors. The shipped domain fold selects the latest world-time absolute anchor, applies only strictly later eligible deltas, and records unused/superseded anchors. Equal-time conflicting absolutes without an explicit, valid correction abstain; a later expiry with no replacement retires current state; delta-only slots abstain. Keep an actual `as_of` or time-basis boundary and note future-effective assertions without making them current prematurely.

**Four minimum fixture journeys:**

1. Absolute 18 + later delta +7 → displayed 25 with both source records, current-state gate satisfied.
2. +7 and −2 with no absolute → *current value unknown* (reason `no_anchor`), advisory history still browseable.
3. Conflicting latest absolutes at identical valid_at → *unknown* (`ambiguous_anchor`) unless an authorized correction resolves it.
4. Latest expiry after previous absolute → *ended/expired; no current View*, with the prior value marked historical, not zero.

Also cover `delta_on_range`, malformed source time, ambiguous entity/slot resolution, weaker source foundation, disabled projection, absent history projection, and redacted numbers. A graph of assertion points is **not** a continuously observed value curve; no invented interpolation.

## 4. Security and trust — P0 gate before code reuse

The present Explorer contains raw UUID detail/graph functions. Specifically review `app.py` helpers `_node_detail`, `_graph_elements`, `_reveal`, `_require_explorer_readonly`, `_require_explorer_agent`, privacy toggle/cookies and existing routes **before** exposing deeper evidence. Existing route behavior is not an automatic authorization precedent.

- Every list, direct ID, deep link, item layer, contributor lookup, View version, edge target and cursor resolves a **server-validated caller and exact namespace**. Match graph identity using canonical namespace/group-ID translation, especially Episodic/TurnEvidence differences. No tenant-global default for an authenticated scoped caller.
- Build explicit allow/deny policy for ordinary readonly users versus operators: retired/history, candidate/orphan/internal Views, raw episode content, source excerpts and METRIC rows. Check both source and target visibility on edges. A missing unauthorized node cannot leak its existence through counts or labels.
- Apply `privacy_redact` to **all** content, including nested numbers and authority values, quote fields, inline long text, preview, graph labels, tooltips, HTML, JSON, browser URL, logs and error messages. The existing localhost-only reveal behavior is not an unconditional API bypass.
- Source validity must be checked at read time, including evidence erasure, stale View contributors and supersession. Never use cached receipt IDs as substitute for live existence/eligibility. Avoid persistent browser caches and leaking text to telemetry.
- Parameterize graph queries and enforce bounded records/pages, safe ordering, serializable request cancellation and no write/repair side effects; read requests must not refresh scores/last-access time.
- Fail closed on uncertain authorization and absent source evidence. Render clear internal diagnostics only if the caller is allowed to see them.

## 5. Execution phases and explicit gates

| Phase | Deliverable | Implementation references | Exit gate |
| --- | --- | --- | --- |
| **P0 — Contract & threat-model review** | Read-projection schema/DTOs, route matrix, tier/namespace/source permissions, source-ID map, fixture matrix, old-feature parity checklist and design decisions | `explorer/app.py`, `memory_queries.py`, `view_models.py`, `privacy.py` | Written decisions approved; positive/negative security cases and fixture shapes ratified. **No UI/API code until this gate.** |
| **P1 — Scoped read service** | Typed inspection service, bounded query projections, visibility predicates, paginated relation/source APIs, typed error/empty states | `memory_queries.py`, `view_query_repository.py`, `scalar_view_repository.py`, existing adapter/shared repo | Unit/contract and disposable Neo4j tests pass; wrong namespace, tier, UUID, cursor and erased source fail closed; no writes/LLM |
| **P2 — Atlas shell & Memory Reader** | Overview/search, memory header, progressive Details/Evidence/History, search, navigation, responsive layout, default redaction | Replaceable `explorer/templates` and `static`; `integration.py` stays | Entity/Episodic read journey, keyboard/mobile/reduced-motion, deep-link/back, stale-response tests; old operator controls preserved or separately reachable |
| **P3 — View Studio** | View-kind renderer registry, Observe/Fold/Project interaction, contributor roles, FACT/METRIC audience status, provenance/versions | `view_models.py`, `view_query_repository.py`, `view_evidence_lifecycle` | Current scalar View ≠ advisory history, event-only timeline, counter, operator Metric and stale/orphan/superseded tests pass; no authority inflation |
| **P4 — Scalar Explorer** | Canonical slot identity, authorized current-state panel, assertion ledger, history pagination, abstention/expiry screens and source navigation | `scalar_state_fold.py`, `scalar_history.py`, `scalar_state_service.py`, `scalar_view_repository.py` | Four fixture outcomes + correction, range, future-effective, feature-off, disabled, redacted and weak-foundation tests pass; browser performs **no arithmetic fold** |
| **P5 — Evidence, time, graph** | Source→episode→View trail, real time-basis labels, bounded optional graph, Follow/Back | Existing typed edge/receipt readers; `explorer/recall_packet_prototype.py` for shape reference, **not authority** | Claim/receipt/episode UUID separation, source erasure, history permissions, cycle/pagination/high-degree, keyboard navigation verified |
| **P6 — Cutover & release** | Feature-flagged Atlas, operator-function parity/fallback, documentation, instrumentation, rollout/rollback plan | Existing explorer router and tests | All release acceptance criteria below; required CI green on exact reviewed SHA; no migration or flag enablement without approval |

**Ordering:** P0 → P1 is strictly sequential. P2 may use offline fixtures while P1 is being tested but cannot connect to production reads before P1 security acceptance. P3/P4 can be independently developed after the common contract stabilizes. P5 integrates the shared navigation and provenance flows; P6 is deliberately separate. Each phase produces reviewable, focused commits or PRs and records deviations back in this plan. Do not treat mock navigation as a working backend.

### Phase test map

- Security/route parity: `tests/test_explorer_app.py`, `tests/test_explorer_candidates.py`, `tests/test_privacy.py`, `tests/test_view_audience_visibility.py`, `tests/test_view_namespace_fence.py`; add direct Atlas security/contract tests.
- Views/provenance: `tests/test_view_evidence_lifecycle.py`, `tests/test_view_class_metric.py`, `tests/test_view_repository_lww.py`, `tests/test_memory_queries_provenance.py`; new contributor/supersession/cursor tests.
- Scalar authority/history: `tests/test_scalar_state_fold.py`, `tests/test_scalar_ended_state.py`, `tests/test_scalar_history.py`, `tests/test_scalar_view_authority.py`, `tests/test_scalar_provenance_edges_live.py`; new typed-slot inspection/UI tests.
- Client/browser: proper HTML escaping of hostile excerpts, aria-expanded/state synchronization, text zoom, screenshots at 360/390/768/1280/1440 px, keyboard & focus, 200% zoom, reduced-motion, rapid requests/cancellation, missing/denied/deleted states. Focused browser tests should be automated if a supported runner exists; do not silently claim they ran from JS syntax checks.
- Graph-backed tests run only against **disposable** Neo4j with explicit opt-in. Follow `.agent/workflows/run_and_test.md` for focused tests/ruff and exact-SHA required CI.

## 6. Acceptance contract and measurable outcomes

A release is acceptable only if:

1. From a search result, the operator can inspect an actual authorized memory, open its original source, follow one typed relation, and return without losing orientation; *Expand* does not navigate away.
2. A View clearly identifies its kind, class, audience, currentness, supporting inputs and derivation; the three derivation stages do not introduce fabricated source text.
3. Scalar Explorer shows a canonical authoritative value **only** when the existing fold/authority service provides one; unanchored, ambiguous, expired and unsupported cases are explicitly explained with source records, not silently calculated.
4. User-only, operator-only, cross-namespace, candidate, retired/orphaned and erased cases are correctly fenced, including summary counts, graph labels, excerpts, value fields and deep links. No unauthorized existence hints.
5. Default summary does not load the full graph/evidence body; each expansion is paged, bounded and model-call-free. Measure per-layer query count, response size, latency p50/p95, and high-degree behavior on representative disposable fixtures **before** selecting concrete performance thresholds in P0. No memory-content telemetry.
6. All existing necessary Explorer operations (candidate moderation, privacy toggle, graph navigation, Recall Lab, system pages) remain functional or have explicit alternate entry points and regression tests.
7. The connected UI passes keyboard, responsive, focus, accessible labels/contrast, reduced-motion and rapid-navigation checks. The screenshots/prototypes are a design target, not proof of deployed behavior.
8. Docs, tests, rollout flag, fallback and operator instructions are complete; targeted verification is reported honestly and required CI is green on the exact reviewed SHA.

**Rollout:** default-off Atlas route or view toggle at first. Keep legacy Explorer reachable until feature parity and permission review are signed off. No new graph labels, edges, durable View kinds, public API/MCP tools, default-on scalar/event feature toggles or changed recall output in this feature. All code may be reverted without data migration.

## 7. Alternatives and risks

- **Fork cc-stretch terminal renderer:** rejected; its token LCS growth and 16 ms frame renderer solve terminal pointer/width constraints, not browser provenance. Keep stable-anchor progressive attention; use semantic DOM, actual source text and short optional animation.
- **Embed full graph as the default screen:** rejected; graph is an alternate tool, not how someone reads a memory. Keep bounded local graph available.
- **Generate a full long-form explanation for each memory up front:** rejected; wastes inference and can create false provenance. Layered deterministic fetch by default.
- **Recompute scalar/currentness/history in UI or from generic View prose:** rejected; weakens Menhir's canonical fold, correction, expiry, temporal and authority guarantees.
- **Introduce standalone backend/frontend service:** rejected; duplicate auth, namespace, repo connections and deployment. Redesign the frontend within existing runtime.
- Main risks: shared UUID/namespace semantics differ by node family; older Views may have missing evidence; history and Metric pages may cross a user/operator trust boundary; animations could change reading position; high-degree nodes can induce N+1 workloads. Address these at P0/P1 rather than masking them in the UI.

## 8. Invariants

1. No inference/model call, write, rebuild, repair, last-access mutation or ranking change from an ordinary Atlas inspect.
2. Namespace, tier, view audience/class, privacy/redaction, erasure and source-evidence validity are enforced at every read layer.
3. Derived View ≠ direct source; currently materialized history ≠ current-state authority; completed event ≠ current state; `METRIC` ≠ recallable semantic fact.
4. Deltas without an absolute anchor never yield an invented current total. Conflicts, expiry and corrections use the canonical fold, not frontend heuristics.
5. Time provenance is explicit; world time never silently falls back to learned time. Unknown and erased evidence cannot become supported.
6. Stable IDs/relations are typed and paged; graph hops remain bounded, no cross-tenant existence hints.
7. Replacement of legacy UI preserves necessary operational capabilities and is reversible.
8. The separate future project-status briefing is not implemented, approximated or renamed as a View in this feature.

## 9. Docs and open decisions

Update `.agent/architecture.md` (Atlas integration/security), `.agent/endpoints.md` (internal routes), `.agent/data_models.md` if a shared DTO is introduced, operator Explorer docs, `CHANGELOG.md`, and this plan as implementation phases land. Keep [UX research](../../docs/design/memory-atlas-ux-research-2026-10-14.md) and [prototype gallery](../../docs/prototypes/memory-atlas-v2/README.md) as design references; do not treat synthetic content as test evidence.

**P0 decisions requiring approval:** (a) exact read privilege for raw episodes/evidence and retired, historical, internal/METRIC Views; (b) selected namespace/deep-link semantics for a caller with multiple allowed namespaces; (c) whether View history/versions are a separate operator audit view or an explicitly restricted expansion. Default until decided: **fail closed**.

No implementation was performed when writing or refining this plan. This is the intended future execution contract, not a claim of completed endpoints, tests, authorization or deployments.
