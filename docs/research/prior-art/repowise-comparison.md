# Repowise vs Menhir — Prior-Art / Positioning Comparison

**Date:** 2026-07-09  
**Status:** External comparison note; use for positioning, roadmap triage, and benchmark planning.  
**Compared project:** [`repowise-dev/repowise`](https://github.com/repowise-dev/repowise)  
**Compared project state:** not pinned to a commit/release at review time (curation audit,
2026-08-07) — re-verify claims below against the current repo before relying on them for a
decision; this is a known staleness risk for all comparisons in this cluster.  
**Trigger:** Reddit post in `r/ClaudeAI` claiming organic adoption / 50k+ pip installs.  
**Primary question:** Does Repowise collapse Menhir's intended lane, or does it validate a neighboring category?

---

## 1. Executive verdict

Repowise is a serious adjacent competitor. It is **more threatening to Menhir's public positioning than MemTrace**, because it does not stop at structural code search. It presents a full "codebase intelligence layer" for AI coding agents:

- dependency graph
- git history
- generated wiki/docs
- architectural decisions
- code health / defect risk
- change risk
- agent provenance
- graph-aware refactoring plans
- MCP tools
- dashboard / VS Code / hosted tier / PR bot

This means Menhir should **not** position around a broad claim like:

> graph + git + docs + MCP for agents

Repowise is already productizing that.

Menhir's safer and more durable lane is:

> evidence-backed cognitive memory for agents: what happened, why it happened, what was believed, what evidence supports it, what changed, what was superseded, and what should not be forgotten.

Repowise is primarily a **code intelligence and risk product**. Menhir should become the **agent cognitive infrastructure layer** that can consume code/risk signals but is not defined by them.

---

## 2. What Repowise is claiming

From the public README, Repowise describes itself as:

> "the codebase intelligence layer for your AI coding agent"

Its advertised shape:

- **Five layers:** graph, git, docs, decisions, code health.
- **Nine MCP tools:** task-shaped rather than primitive-shaped.
- **15 languages:** AST parsing across many languages, with deeper "Full" support for Python, TypeScript, JavaScript, Java, Kotlin, Go, Rust, C++, and C#.
- **Multi-repo workspaces.**
- **One `pip install`.**
- **Local dashboard.**
- **VS Code extension.**
- **Hosted/team product.**

Its strongest wedge is not memory. It is:

> measure risky code, locate why it is risky through graph/git signals, and give the agent a concrete refactoring plan.

That is a highly legible commercial story.

---

## 3. Where Repowise is ahead

### 3.1 Code health / defect prediction

Repowise's deepest differentiator is code health:

- 25 deterministic markers
- file score from 1-10
- three separate lenses:
  - defect risk
  - maintainability
  - performance
- calibrated against defect data
- zero LLM calls in the health scoring path
- concrete refactoring plans attached to the findings

This is not currently a Menhir strength. Menhir may eventually use code-health-like information as a retrieval or prioritization signal, but it should not try to compete directly with Repowise on defect prediction unless we have a separate benchmark-backed reason to do so.

### 3.2 Benchmark credibility

Repowise has a much stronger public benchmark story than Menhir today.

Notable claims from its benchmark repo/docs:

- code-health score evaluated at a historical T0 commit before a future 6-month defect window
- cross-project ROC AUC around 0.737 / 0.74 depending on corpus framing
- bootstrap confidence intervals
- file-size controls
- SZZ checks
- CodeScene head-to-head on same files / same commits / same labels
- paired significance tests

We have not independently reproduced these numbers, but the methodology write-up is much more credible than normal OSS marketing copy.

Menhir needs an equivalent benchmark story for its own differentiator, not theirs.

### 3.3 Workflow/product surface

Repowise meets users where they already work:

- `pip install repowise`
- `repowise init --index-only`
- `repowise mcp`
- local dashboard
- VS Code extension
- PR bot
- hosted MCP endpoint
- copy-to-agent refactoring cards
- risk comments on PRs

Menhir is still more backend/research substrate than product workflow. That is fine for now, but it means we need to stop measuring ourselves only by architecture depth. Repowise is winning on adoption surface.

### 3.4 Task-shaped MCP tools

Repowise's MCP surface is framed around user/agent tasks rather than internals:

- `get_overview()`
- `get_answer(question)`
- `get_context(targets, include?)`
- `get_symbol(...)`
- `search_codebase(...)`
- `get_risk(...)`
- `get_why(...)`
- `get_dead_code(...)`
- `get_health(...)`

This is a useful product lesson. Menhir's tools should become more task-shaped too.

Good Menhir-shaped tools would be things like:

- `recall_decision_context`
- `explain_why_changed`
- `audit_agent_belief`
- `replay_failure_context`
- `what_did_we_know_then`
- `show_superseded_beliefs`
- `find_prior_failed_attempts`
- `build_evidence_brief`

---

## 4. Where Menhir remains different

### 4.1 Menhir is memory-first, not code-health-first

Menhir's center is durable agent memory:

- raw episode ingestion
- entity/relationship extraction
- graph scoring
- structure-aware recall
- lifecycle state
- compression / decay
- conflict detection
- evidence and provenance policy

Repowise builds a strong code intelligence index. Menhir builds a system for managing what an agent knows over time.

That distinction matters.

### 4.2 Menhir has a stronger truth/provenance model

Menhir's artifact policy is a major differentiator:

- LLM-authored artifacts are not trusted on create.
- Human-authored artifacts require evidence to become trusted.
- Promotion fails closed without promotable evidence.
- LLM self-inference cannot justify trust by itself.
- Superseded artifacts become historical; they are not deleted.

Repowise has evidence-backed decisions and decision edges, but Menhir's trust model is closer to a general agent-belief governance layer.

This should be protected and moved toward the center of the product story.

### 4.3 Menhir can own belief drift and contradiction governance

Repowise can answer:

> Why does this part of the code exist?

Menhir should answer the harder questions:

> What did we believe when we made it?  
> What evidence supported that belief?  
> What later contradicted it?  
> Which conclusion superseded it?  
> Which agent action was based on the outdated belief?  
> What should future agents avoid repeating?

This is not just code documentation. It is agent cognition management.

### 4.4 Menhir can integrate non-code experience

Repowise is strongest when the source of truth is a repository. Menhir's future lane is broader:

- user corrections
- failed agent attempts
- tool failures
- local runtime friction
- test failures
- command outputs
- model/provider behavior
- session state
- operator decisions
- Git provenance
- code structure
- documents
- personal/project constraints

This is the basis for the Cognitive Infrastructure Platform framing.

---

## 5. Dangerous overlap

Repowise overlaps with several Menhir future ideas:

### 5.1 Architectural decisions

Repowise claims architectural decisions mined from multiple sources, evidence-backed, linked to graph nodes, and connected by edges like:

- `supersedes`
- `refines`
- `conflicts_with`

That overlaps with Menhir's artifact/evidence/supersession direction.

Important distinction:

- Repowise appears to treat decisions as one code-intelligence layer.
- Menhir should treat decisions as one kind of governed belief/artifact inside a broader memory and evidence system.

### 5.2 Agent provenance

Repowise claims agent provenance from git history: attributing commits to AI agents and surfacing agent-written low-health hotspots.

This overlaps with Menhir's agentic Git / construction narrative / episode attribution direction.

Menhir needs to move quickly from "future idea" to a minimal concrete slice:

- diff hunk attached to episode
- episode attached to prompt/user goal
- hunk attached to file/symbol/test evidence
- query: "which episode caused this region to change, and why?"

### 5.3 `get_why`

Repowise's `get_why` tool is a direct warning shot. It returns architectural decision records, evidence spans, and supersession lineage, and falls back to git archaeology when no ADRs exist.

Menhir should not cede the phrase "why" to a code-indexing product. Menhir's `why` should be deeper:

- why the agent acted
- why the user corrected it
- why a belief was trusted
- why a belief became historical
- why a test failure was linked to a prior decision
- why the system should suppress or resurface a memory

---

## 6. Borrow list

### 6.1 Add a lightweight health/risk signal as input, not identity

Do not clone Repowise. But Menhir should be able to store or ingest code-health-like signals as evidence/context:

- file health score
- hotspot flag
- ownership concentration
- churn/complexity hotspot
- co-change risk
- untested hotspot
- change-risk score

Menhir query example:

> "This memory is anchored to a low-health file that has caused three prior failed attempts and two test regressions."

That is Menhir-shaped.

### 6.2 Make Menhir's MCP tools task-shaped

Current Menhir tools are functional, but product tools should compress workflows:

- `explain_why_changed(file_or_symbol, time_range?)`
- `recall_prior_attempts(task_or_file)`
- `replay_agent_context(commit_or_episode)`
- `find_contradictions_about(target)`
- `build_evidence_brief(claim_or_decision)`
- `show_memory_lineage(target)`

These should return compact, cited, agent-usable packages rather than raw graph pieces.

### 6.3 Build a benchmark around Menhir's actual value

Do not benchmark Menhir against Repowise on code health. Benchmark Menhir on what it should own:

- repeated-mistake avoidance
- prior decision recall
- contradiction/supersession handling
- recovery of "why" after context loss
- file-context memory recall that vector search misses
- agent failure replay
- correct suppression of outdated beliefs
- evidence-backed trust ranking

Possible benchmark fixture:

1. Agent makes a decision from evidence A.
2. Later evidence B supersedes it.
3. A future coding task touches the related file.
4. Compare baseline agent vs Menhir-augmented agent:
   - does it recall the right current decision?
   - does it avoid the superseded one?
   - does it cite evidence?
   - does it avoid repeating a known failed attempt?

### 6.4 Add a "why ledger"

Repowise has a costs dashboard and code-health proof. Menhir should have a human-visible ledger for:

- decisions remembered
- contradictions caught
- outdated beliefs suppressed
- prior failed attempts avoided
- context recovered from a previous session
- evidence-backed claims promoted
- LLM-only claims held in candidate state

This turns abstract cognitive infrastructure into visible product value.

---

## 7. Do not copy

Avoid these traps:

### 7.1 Do not become a second Repowise

Repowise is already much more legible as:

> code health + risk + graph-aware refactoring for AI codebases

Menhir should not chase this center.

### 7.2 Do not over-index on static code intelligence

Static code graph, git history, docs, and risk are important inputs. They are not Menhir's identity.

Menhir's identity should be the memory/evidence layer that spans:

- code
- sessions
- users
- agents
- decisions
- beliefs
- failures
- tests
- git
- tools
- changing context

### 7.3 Do not leave "why" vague

Repowise has already productized a `get_why` concept. Menhir needs concrete `why` artifacts and queries, not just roadmap language.

---

## 8. Recommended Menhir positioning update

### Weak positioning

> Menhir is a graph memory system with code structure and Git awareness for agents.

This sounds too close to Repowise/MemTrace/DeepWiki-adjacent tools.

### Stronger positioning

> Menhir is cognitive infrastructure for agents: an evidence-backed memory substrate that records what happened, what was believed, why it was believed, what code/Git/test artifacts support it, how that belief changed, and what future agents should recall or avoid.

### Shorter version

> Menhir remembers the reasoning context behind agent work, not just the codebase shape.

### Even shorter

> Git remembers what changed. Menhir remembers why the agent thought it should.

---

## 9. Near-term roadmap implications

### Immediate

- Add this comparison to the external-eval watch list.
- Define Menhir's `why` surface explicitly.
- Create one benchmark fixture for superseded decision recall.
- Add one task-shaped tool around evidence-backed decision context.

### Soon

- Implement episode-to-diff-hunk attribution.
- Add query for "what prior attempts touched this file/symbol and failed?"
- Add candidate/trusted/historical artifact visibility in context builder output.
- Add freshness/evidence badges to recalled memories.

### Later

- Ingest external code-health/risk signals as evidence/context.
- Add agent provenance over Git history, but tie it to episode/prompt/evidence instead of only commit author metadata.
- Build a dashboard panel for memory value: contradictions caught, stale beliefs suppressed, prior failures avoided.

---

## 10. Bottom line

Repowise validates the agent-codebase-intelligence category and raises the bar for product polish, benchmarks, and workflow integration.

It also narrows Menhir's safe lane.

Menhir should not compete head-on with Repowise as a code-health/refactoring product. Menhir should absorb code intelligence as one signal inside a broader evidence-backed memory system.

The durable Menhir claim is:

> Agents need more than a code graph. They need governed memory of why actions were taken, what evidence supported them, how beliefs changed, and which lessons should survive into the next session.
