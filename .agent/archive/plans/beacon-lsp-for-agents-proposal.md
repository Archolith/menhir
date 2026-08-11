# Proposal: Beacon — an "LSP for Agents" (SEPARATE PROJECT)

> **ARCHIVED 2026-08-10.** This proposal was superseded by the accepted Menhir-native Beacon
> architecture described in its status note below. It is retained as rejected design history.

<!-- This is a NEW-PROJECT proposal, not a menhir backlog plan. Placed in .agent/plans/ (not
     backlog/) because it is deliberately decoupled from menhir. If greenlit it should graduate to its
     own repo; menhir becomes one (optional, "smart") provider behind it, never a dependency. -->

**Status:** SUPERSEDED — 2026-08-07 — this proposal's core bet (Beacon decoupled from Menhir,
Menhir as one optional backend) was superseded by the 2026-07-25 "Semantic Object and Beacon
Architecture Decisions" baseline (decision queue #56, accepted), which redefines Beacon as
Menhir-native: "the authorized adaptation and delivery boundary" generating `Orientation`
objects from Menhir's own Semantic Object graph. That decision doc no longer exists in the
working tree — its only remaining copy is commit `47bab08` on `main`
(`docs/architecture/semantic-object-architecture-decisions.md`). The open product decision
below was never answered directly; it was overtaken by the later accepted decision instead.
**Source:** `.agent/archive/reviews/menhir-beacon-architecture-review.md` (2026-06-29, reviewer "Antigravity")
**Decision owner:** ctharvey

---

## The gap (one line)

There is a reviewed architecture idea — **Beacon = the interface/contract layer, Menhir = the
implementation/engine** — with **no plan and no code** (`git grep -i beacon -- src/menhir` = empty). It
needs a product call, then (if yes) a proposal-to-plan.

## What Beacon is (and the core bet)

Beacon is a **dynamic, queryable contract** an agent handshakes with on entering a repo — an **LSP for
Agents / an MCP profile** — *not* a static `beacon.yaml`. The review's load-bearing claim: value lives
entirely in the **dynamic, queryable** aspect. A static manifest is just another `llms.txt`/`CLAUDE.md`
that rots; a live interface that answers questions about project state
(`beacon_get_architecture()`, `beacon_get_guardrails()`) is the leap.

Separation is the correct decision: a **dumb `beacon-serve` CLI** (serves a directory of markdown over
the Beacon MCP protocol) sets the low floor; **Menhir is the "smart" provider** developers graduate to
when their markdown rots. Tying Beacon to Menhir's complexity would kill Beacon's adoption.

## Current default (what exists today)

Nothing. Agents boot by "read the README and figure it out"; project intent lives in passive files
(`.agent/README.md`, `llms.txt`, `CLAUDE.md`) that drift from code. No standard handshake, no
guardrails contract, no provider abstraction.

## Promotion criteria (proposal → project)

Beacon earns a build only if the product decision below resolves **yes**, and then graduates by:

- **spike:** a **Beacon MCP profile** (the enumerated tools a server must implement to be
  "Beacon-compliant") + a **`beacon-serve`** dumb provider (markdown-over-protocol). An agent performs
  a boot handshake and gets structured answers instead of scanning raw docs.
- **adoption gate (the real risk):** a **killer client workflow that *requires* Beacon to succeed** —
  without native client support (Cursor / Copilot / Claude Code) no one authors Beacons (XKCD 927
  standardization fatigue). Success is measured by a client flow that is strictly better *because*
  Beacon exists, not by spec completeness.
- **provider ladder:** `Manifest → Docs → Git → Menhir` gradual-adoption path proven — the MCP layer
  is indifferent to where answers come from.

## Path (if greenlit)

1. **Product decision** (below) — do not build first.
2. Define the **Beacon MCP profile** (required tools/resources) as a spec, framed as a *profile*, not a
   file format.
3. Build **`beacon-serve`** — the stupid-simple markdown-over-Beacon-MCP provider (the "dumb" tier).
4. Prove a **killer client handshake** (agent boot = Beacon handshake, not README-scan).
5. Wire **Menhir as the "smart" provider** behind the same profile (optional graduation tier).
6. Optional later: CI check that fails when the Beacon contract severely contradicts Git reality.

## Open product decision (needs ctharvey)

- **Does Beacon earn a project at all**, or stay a research note? (The review endorses the *idea* but
  flags adoption as the killer risk.)
- If yes: **own repo now**, or prototype `beacon-serve` inside a scratch space first?

## Risks (from the review)

- **Standardization fatigue** (XKCD 927) — 14 competing ways to document AI instructions already.
- **The killer-client bootstrap** — no native client support → no authors.
- **Staleness in the dumb tier** — a rotting static provider breaks first-experience trust.
- **Ontology overreach** — a rigid concept ontology collapses under its own weight (semantic-web
  cautionary tale). Beacon should represent maintainer **intent** ("ideal state"), code is reality;
  when they diverge the agent aligns code to Beacon, not vice-versa.

## Non-goals

- Do not make Beacon a menhir feature or a menhir dependency — the whole point is decoupling.
- Do not lead with `beacon.yaml`; do not build a rigid ontology.

## Source

`.agent/archive/reviews/menhir-beacon-architecture-review.md`. Confirmed no `beacon` code in `src/menhir`
(2026-07-11).
