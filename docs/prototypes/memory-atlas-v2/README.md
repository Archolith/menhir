# Memory Atlas v0.2 — multi-page UX prototype

**Status:** interactive design study only; all memory content is deliberately synthetic. **No Menhir backend, Neo4j, LLM, tracking or persistence is connected.** The current production Explorer UI is an early prototype, not a layout constraint.

## Preview

Open [index.html](index.html) in a browser, or from this directory run the command *python -m http.server 8765* and visit http://localhost:8765. Everything is local with no build, dependency installation or network access. Fixtures and interactions live in [atlas.js](atlas.js), responsive styling in [atlas.css](atlas.css).

Each page can be opened from the left navigation or via the hash fragments below:

| Page | Route | What to evaluate |
| --- | --- | --- |
| Knowledge overview | index.html#browse | A useful default entry point: search, filters, memory previews, visibility of trust signals |
| Memory reader | index.html#inspect/identity | Quiet reading-first text, inline stable-anchor stretch, detail expansion and contextual follow navigation |
| Evidence trail | index.html#evidence | Claim → Menhir receipt → primary source; click each stage |
| Temporal history | index.html#timeline | Learned time vs world time, current vs superseded, avoiding inferred dates |
| Relationships | index.html#graph | Click local nodes, choose 1/2 hops, toggle historical links and open selected memory |

Examples: stable identity, canonical merge decision, ingestion receipt, superseded strategy, architecture document and governance policy. These are not statements about production Menhir.

## Interactions to try

- Type “merge” in the top search and switch Current/Historical filters on Overview.
- In Memory Reader click **stable UUIDs** inside the sentence. Previously visible words stay in order while an additional clause appears in place; no model call is needed.
- Expand **Supporting evidence**, **What changed?**, or **Related memories** and navigate to other views.
- In Evidence trail click different stages. The document, episode and derived claim have separate identities and source roles.
- In Timeline switch **When Menhir learned it** ↔ **When it happened**. Missing world time is explicit rather than fabricated.
- In Relationships enable **Show historical links**, switch 1 ↔ 2 hops and click nodes. The graph is intentionally bounded.
- Press / to focus search; navigate with Tab/Enter; inspect at mobile/tablet widths or with reduced-motion enabled.


## Specialized View Studio and Scalar Explorer

The [Views & Scalars concept](views-scalars.html) adds two purpose-built modes:

- [View Studio](views-scalars.html#views) lets readers inspect scalar state, scalar history, event-lane timelines, counters and an operator-only Metric. It distinguishes kind, class (FACT/METRIC), audience (RECALL/OPERATOR), world-time metadata, derived value, and contributor receipts.
- [Scalar Explorer](views-scalars.html#scalars) focuses on a canonical scalar slot (subject UUID, attribute, scope, value kind, unit), current folded state or abstention, source-time assertion history, and the evidence chain.
- Two sample scalar cases intentionally contrast anchored current state (absolute 18 followed by delta +7; example folded value 25) with delta-only history (+7 and −2; current absolute value UNKNOWN).
- Current scalar state and advisory scalar history are different authority surfaces. History never justifies fabricating a total; operator-only Views must not leak into general recall.

These are interactive synthetic examples, not evidence of production deployment or enabled feature flags. Offline, read-only, no model calls.

### Code contracts informing the specialized pages

- src/menhir/infrastructure/view_models.py — ViewClass, ViewAudience, ScalarStateKind, ScalarHistoryKind, CounterKind and TimelineKind.
- src/menhir/infrastructure/scalar_view_repository.py — canonical scalar slots, authority contributors, paginated HISTORY_ENTRY edges.
- src/menhir/explorer/recall_packet_prototype.py — current state versus advisory history, time and evidence.
- .agent/plans/menhir-view-evidence-lifecycle-2026-08-28.md — current, recallable FACT Views require valid supporting evidence.

## Design questions

1. Is a reading-first view more useful than the old center-graph/right-inspector layout?
2. Is phrase-level stretch discoverable, or should expansion be a simpler below-text disclosure?
3. Does a dedicated evidence trail clarify claim versus derived View versus Menhir receipt versus resolved episode versus source?
4. Should timeline be its own mode or another disclosure on the reader?
5. Should graph navigation change the focal memory or preserve the graph while updating only its detail rail?
6. Is the context nav too large on laptops? Should it collapse by default?
7. Can a reader distinguish historical from currently authoritative state at a glance?

## Scope boundaries

- Hard-coded fixtures only. “2 sources available” is sample UI data, not a computed receipt count.
- No authorization, namespace isolation, redaction, candidate moderation, user data, or temporal contracts have been exercised. Build/review them using Menhir's canonical service layer before integration.
- Graph layout is illustrative, not real graph rendering. Real edges need scoped typed queries and bounded navigation.
- Fixture content uses text nodes where generated dynamically. Real evidence still needs safe rendering, auth, scoped source access, latency limits, error/empty states, and accessibility testing.
- Do not copy cc-stretch's terminal renderer; preserve the stable-anchor progressive attention principle only when source text actually supports it.
- Respect reduced-motion and never require animation for meaning.

## Related work

- [First single-page prototype](../progressive-memory-inspector.html)
- [Grounded UX study](../../design/memory-atlas-ux-research-2026-10-14.md)
- [Proposed feature plan](../../../.agent/plans/menhir-progressive-memory-inspector-2026-10-14.md)
- [cc-stretch](https://github.com/pro-vi/cc-stretch) — interaction inspiration, not runtime dependency.
