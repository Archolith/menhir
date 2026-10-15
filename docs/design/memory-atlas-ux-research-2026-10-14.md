# Memory Atlas UX — research-backed design direction

Date: 2026-10-14
Status: design study / fixture prototype; not implementation, not live data.
Prototype: [Memory Atlas (standalone HTML)](../prototypes/progressive-memory-inspector.html)
Feature plan: [Progressive Memory Inspector](../../.agent/plans/menhir-progressive-memory-inspector-2026-10-14.md)

## Direction: greenfield UI, existing Menhir contracts

The existing Explorer's 360px sidebar / center full-graph / 400px inspector arrangement is an early prototype, **not an inherited layout contract**. We are free to replace its layout, styles, templates and JS. Preserve the runtime/repository integration, authorization, privacy semantics, canonical graph/provenance/temporal read paths, and relevant tests—not the screen architecture.

**Default view = reading-first Memory Atlas.** The readable memory statement is the primary surface. Explanatory layers, source evidence, event history and related memories are disclosed on demand. An optional local one-hop graph gives context; a full graph-centric mode remains available for users who want one. Make moving between **Expand** (same memory) and **Follow** (new memory) unmistakable.

### External design research

| Source | Verified interaction | Decision |
|---|---|---|
| [Nielsen Norman Group — Progressive Disclosure](https://www.nngroup.com/videos/progressive-disclosure/) | Secondary/advanced options are deferred so the primary task has focus. | Start with claim, authority, kind, freshness, and meaningful warning; postpone detail. |
| [Neo4j Bloom — Card list](https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/card-list/) | Selected cards and graph nodes stay linked; an inspector can display properties, relationships, neighbors and navigate between them. | Bidirectional memory selection and relationship navigation; don't imitate graph-first default. |
| [Obsidian — Graph view](https://obsidian.md/help/plugins/graph) | Local graph centers on current note, has configurable link direction and depth. | One-hop, focus-relative map with links labeled by semantics and direction; bound neighbors. |
| [Obsidian — Backlinks](https://obsidian.md/help/plugins/backlinks) | The reader can collapse mentions or reveal more paragraph context. | Evidence source rows reveal excerpts *in place*; source navigation is separate. |
| [Linear — Display options](https://linear.app/docs/display-options) | Visibility of metadata is separate from filtering; adjustable detail density. | Keep content visible but reduce metadata noise; no repeated ornamental labels. |
| [GOV.UK — Details](https://design-system.service.gov.uk/components/details/) | Details help scanning; important information needed by most users should not be hidden. | Authority, supersession, and source-availability warnings always remain visible. |
| [W3C — Disclosure pattern](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/) | Button uses aria-expanded; Space/Enter toggle. | Semantic disclosure controls, deterministic focus and keyboard behavior. |
| [W3C — Disclosure card](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/examples/disclosure-card/) | Guard against toggling while selecting text or pressing nested links; test assistive tech. | Don't make complex multi-action cards one giant click target. |
| [MDN — reduced motion](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/%40media/prefers-reduced-motion) | Users may prefer to suppress nonessential motion. | Transitions are purely optional and disabled in reduced-motion mode. |
| [Chrome — animating intrinsic sizes](https://developer.chrome.com/docs/css-ui/animate-to-height-auto) | Browser support exists for new intrinsic-size interpolation, but isn't universal. | Use progressive enhancement and robust instant-open fallback. |

### cc-stretch source review: what to reuse

- [README](https://github.com/pro-vi/cc-stretch/blob/main/README.md): a *skim* state followed by reader-controlled progressive attention; readers never lose their place.
- [guide.ts](https://github.com/pro-vi/cc-stretch/blob/main/hooks/guide.ts): authors a short phrase whose words persist in the longer version.
- [parse.ts](https://github.com/pro-vi/cc-stretch/blob/main/hooks/parse.ts): parses nested short/long alternatives while skipping fenced code and code spans.
- [grow.ts](https://github.com/pro-vi/cc-stretch/blob/main/hooks/grow.ts): tokenizes words and whitespace; uses longest common subsequence (LCS) to find stable anchors, then progressively reveals gaps on both sides. This takes O(short words × long words) time and memory and can align duplicate words ambiguously.
- [reply.tsx](https://github.com/pro-vi/cc-stretch/blob/main/hooks/reply.tsx): redraws each 16ms; cubic easing and content-length-paced ~0.45–1.4s animation. It is engineered for the terminal frame clock.
- [layout.ts](https://github.com/pro-vi/cc-stretch/blob/main/hooks/layout.ts): manually wraps terminal text and matches pointer coordinates to spans; none of that should be ported to the browser.

**Decision:** carry the orientation principle, not the component code. The browser's DOM should own text layout, semantics and hit targets. In Menhir, most expansion is an additional deterministic *projection* of actual source records, not a prewritten replacement sentence. Inline stretch around existing text is valid **only** when the expanded version faithfully contains the exact already-visible anchor in the same order. Otherwise insert more detail beneath the sentence; don't manufacture an artificial LLM expansion. Use 150–240ms opacity/height transitions (not 1.4s terminal-style text accretion) and no motion when requested.

### Visual direction

- **Mood:** thoughtful knowledge studio rather than operational dashboard: calm, high-contrast, text-forward, quiet chrome. Take typographic restraint from Linear, link navigation from Obsidian, relationship rigor from Bloom.
- **Hierarchy:** a stable 2–3 line claim, followed by type / currentness / source availability, then separate disclosure controls (Evidence, History, Related).
- **Desktop:** optional 230–270px memory/search rail, flexible 640–820px central reading surface, optional 280–340px local-context rail. These dimensions are a starting point, not binding. Graph mode can take full canvas width independently. Tablet hides context; mobile is one content column with a simple memory switcher.
- **Color tokens:** background #0A1018, surface #121D2A, border #283648, copy #EEF4F7, muted #93A7B8, interaction #8ED3DF. Mint means confirmed/current, amber means incomplete/warning, lavender means historical. Labels accompany color.
- **Typography:** system sans (avoid remote font dependency), ~16px reading text with 1.6–1.75 line height, 28–39px claim headline, compact monospace only for IDs; generous whitespace around the focal statement. No gratuitous gradient/card overload.
- **Focus:** visible outlines and large enough targets; graph is not the only path to relationships. Provide Back/breadcrumb preserving expanded state, selection and scroll where feasible.
- **Loading:** independent section loads, permission/redaction/error/empty results, cancel stale requests after selection changes, never hide original text or push it out of view unexpectedly.
- **Trust:** derived view ≠ primary evidence; historical ≠ current; world/valid time ≠ when Menhir learned. Explicitly say “No source available” when applicable.

### Prototype expectations and limitations

The standalone [Memory Atlas concept](../prototypes/progressive-memory-inspector.html) is a pure HTML/CSS/JS file with **synthetic** UUID/merge/receipt fixtures. It shows a fully redesigned reading-first layout, Expand/Collapse, Evidence excerpt disclosure, relationships/Back navigation, three modes (Read/Graph/Timeline), a contextual one-hop map, responsive behavior, keyboard-compatible buttons, and a reduced-motion CSS option. It intentionally does not call Menhir APIs, validate multi-tenant security, perform lazy server I/O, or present factual live memory content. Do not confuse the prototype with production functionality.

### Implementation consequences

1. Treat old `src/menhir/explorer/templates/base.html`, `_detail.html`, `static/explorer.js`, and legacy layout CSS as **replaceable UI**, not as the prescribed structure. Retain/port every required feature, route/handler contract, privacy toggle and moderation affordance when replacing.
2. Keep canonical backend app/router registration, Neo4j runtime, auth tier enforcement, namespace predicates and provenance adapters. Do not create a second graph service.
3. Start with a formal interaction / data-layer contract and a prototype usability review, before shipping any new routes or frontend integration.
4. Stage layout changes separately from graph/read-service changes where possible. Regression-test legacy operations, selection, candidate moderation and privacy even if markup changes completely.
5. Run keyboard/screen-reader and responsive review (360/390/768/1280/1440px, text zoom 200%), plus redaction, stale/erasure, source-less and high-degree cases.

## Open UX decisions

- Should the compact claim be the exact stored text or a marked derived summary? Do not implicitly summarize with an LLM.
- Should the context rail float, slide in on demand, or be persistent above desktop width?
- Should graph exploration become its own tab/route or retain an optional split-screen?
- Should “History” require a privileged audit mode when it includes superseded records? Decide with product authorization policy, not CSS.
