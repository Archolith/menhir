# Beacon & Menhir — Architecture Review

> **Archived 2026-08-11.** This is a historical external architecture review with no current Menhir
> execution ownership; accepted Beacon decisions live with the current Beacon corpus.

**Date:** 2026-06-29
**Subject:** Project Handoff and Architecture Review for Beacon & Menhir
**Reviewer:** Antigravity

## Executive Summary of Review
The separation of Beacon (interface/contract) and Menhir (implementation/engine) is the most critical and correct architectural decision in the proposal. However, framing Beacon around a static manifest (`beacon.yaml`) risks making it just another standard in a saturated space of passive files (like `llms.txt` or `CLAUDE.md`). Beacon's true value lies in being a dynamic, queryable standard—essentially an **LSP (Language Server Protocol) for Agents**.

---

## Evaluation Questions

### 1. Does Beacon solve a real problem or simply rename existing concepts?
It attempts to solve a real problem: unstructured context stuffing vs. structured, dynamic, temporal querying of project state. However, if the baseline is a static `beacon.yaml`, it is dangerously close to simply renaming `.agent/README.md` or `llms.txt`. The value prop relies entirely on the **dynamic, queryable aspect**. If it's static, it's just another document that will rot. If it's dynamic (an interface that answers questions about state), it's a massive leap forward.

### 2. Is Beacon better viewed as a specification, a protocol, a file format, an MCP convention, or something else?
It is best viewed as an **MCP Convention (or Protocol)**.
If it is a file format, it competes with passive documentation. If it is an MCP convention, it defines a standard set of tools/resources for agents to query a project's state actively (e.g., `beacon_get_architecture()`, `beacon_get_guardrails()`). This makes it a live contract rather than dead text.

### 3. Is separating Beacon from Menhir the correct architectural decision?
**Yes, absolutely.** Interface vs. Implementation. Menhir is a heavy, opinionated, complex knowledge engine. Beacon is the lightweight contract. Tying them together kills Beacon's adoption because it would require buying into Menhir's complexity. A dumb `beacon-serve` CLI should exist alongside the highly intelligent Menhir daemon.

### 4. Is the proposed provider abstraction appropriate?
Yes. The progression from `Manifest -> Docs -> Git -> Menhir` allows gradual adoption. It’s the classic "start simple, scale to complex" model. It ensures the MCP layer doesn't care where the answers come from.

### 5. What important prior art is missing?
*   **LSP (Language Server Protocol):** It standardized IDE-to-compiler communication. Beacon is proposing to be the LSP for Agent-to-Project communication.
*   **LSIF (Language Server Index Format) / Sourcegraph:** Standardizes code intelligence and graph-based codebase representation.
*   **Semantic Web / RDF Ontologies:** A cautionary tale. If Beacon gets too obsessed with rigorously defining "concepts" and "relationships" in a rigid ontology, it will collapse under its own weight, much like the semantic web did. Software is messy.

### 6. What technical risks are underestimated?
*   **Staleness in the Dumb Tier:** A static `beacon.yaml` will rot immediately. If the first experience agents have with Beacon is outdated information, the trust is broken, and agents will revert to raw codebase scanning.
*   **Contradiction Resolution:** If Beacon exposes multiple sources (docs, git, manual manifest) in a dumb provider, what happens when they conflict? Menhir handles this, but a dumb Beacon provider won't, pushing the resolution burden back to the agent (which defeats the point).

### 7. What adoption risks are underestimated?
*   **Standardization Fatigue:** [Relevant XKCD (927)](https://xkcd.com/927/). There are already 14 competing ways to document AI instructions for a repo.
*   **The "Killer Client" Problem:** If Cursor, GitHub Copilot, or Claude Code don't natively look for and speak "Beacon", no one will bother writing Beacons. You have to build a compelling agent workflow that *requires* Beacon to succeed to bootstrap the network effect.

### 8. What would make you skeptical of this approach?
*   If Beacon tries to define a rigid, complex ontology for what a "project concept" is.
*   If the marketing and documentation emphasize `beacon.yaml` over the dynamic MCP interface.
*   If Beacon tries to describe reality *instead* of maintainer intent. Beacon should represent the "ideal state / intent", while the code represents the "reality". When they diverge, the agent's job is to align the code with the Beacon, not update the Beacon to match broken code.

### 9. If you were building this from scratch, what would you change?
I would frame Beacon entirely as an **MCP Profile**, not a file format. I would define the specific MCP tools a server must implement to be "Beacon-compliant".
Then, I'd build a stupid-simple CLI tool (`beacon-serve`) that takes a directory of markdown files and serves them over the Beacon MCP protocol. This acts as the "dumb" implementation, setting up Menhir as the "smart" implementation that developers graduate to when their `beacon-serve` markdown files rot.

### 10. If successful, what does the ecosystem around Beacon look like in five years?
*   IDEs and Agent platforms auto-detect a Beacon server when opening a repo.
*   Instead of prompting the agent with "Read the README and figure out what to do," the agent's first boot step is an automated handshake with the Beacon server.
*   CI/CD pipelines fail if the Beacon manifest severely contradicts the Git reality (e.g., a "Guardrails" test fails).
*   Menhir becomes a paid enterprise SAAS (or a heavy local orchestrator) that maintains this state automatically for large corporate monoliths, while open-source projects rely on lightweight static providers.
