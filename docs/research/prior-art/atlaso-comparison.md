# Atlaso vs Menhir — Prior-Art / Closed-System Comparison

**Date:** 2026-08-06  
**Status:** External prior-art note; use for benchmark planning, architecture positioning, and product-interface lessons.  
**Compared product:** [Atlaso](https://www.atlaso.ai/)  
**Public implementation inspected:** [`atlaso-labs/claude-code`](https://github.com/atlaso-labs/claude-code), [`atlaso-labs/cursor`](https://github.com/atlaso-labs/cursor), [`atlaso-labs/codex`](https://github.com/atlaso-labs/codex), [`atlaso-labs/antigravity`](https://github.com/atlaso-labs/antigravity), [`atlaso-labs/mcp`](https://github.com/atlaso-labs/mcp), and [`atlaso-labs/cli`](https://github.com/atlaso-labs/cli).  
**Evidence boundary:** Atlaso's connectors are public; the hosted memory engine, reconciliation logic, ranking model, enrichment workers, and Ambient Memory composer are not open source.

---

## 1. Executive verdict

Atlaso is a **direct product competitor** to Menhir's agent-memory lane, but only partially inspectable architectural prior art.

The externally visible feature overlap is substantial:

- automatic capture after agent turns
- automatic recall before prompts or at session start
- personal and per-project scopes
- cross-tool memory
- duplicate suppression
- conflict/disagreement indicators
- claimed supersession and confidence verdicts
- freshness weighting
- background enrichment
- explicit remember/recall/recent/forget/status tools
- durable local delivery queues
- cloud synchronization

The verifiable implementation overlap is narrower. Public code confirms a robust thin-client pipeline around raw memory deposits, scope routing, secret scrubbing, durable delivery, server recall, and conflict metadata. It does **not** expose the core mechanisms that decide:

- what two memories are claims about the same thing
- whether they contradict
- which claim supersedes another
- how evidence changes confidence
- how freshness is calculated
- how entities are identified
- how current state is rebuilt
- how retrieval is ranked in production

The clearest classification is:

> Atlaso is strong prior art for automatic cross-tool memory capture, delivery, scope, conflict presentation, and hosted product integration. It is weakly inspectable prior art for Menhir's assertion, identity, temporal-fold, and View architecture.

Atlaso's published benchmark is also not currently a benchmark threat at Menhir's observed answer-quality level. Atlaso reports **56.4%–69.2% end-to-end QA on LongMemEval-S**, depending on judge. Menhir has recently reached roughly **90% on the official LongMemEval Oracle split**. These figures are not directly comparable because LME-S includes distractor sessions and LME-Oracle does not, but Atlaso's public numbers do not establish superiority over Menhir's semantic-state pipeline.

M3 Memory remains the more important published end-to-end benchmark competitor. Atlaso is more important as a product and operational-design competitor.

---

## 2. Evidence discipline for a closed system

Atlaso should be evaluated in three evidence tiers.

### 2.1 Verified from public client code

The public repositories establish that Atlaso:

- captures agent exchanges through lifecycle hooks
- performs a local commodity worth-keeping gate
- scrubs common secrets before network transmission
- assigns personal or project scope heuristically
- writes outbound memories to a durable local outbox before sending
- uses deterministic client IDs for idempotent retries
- synchronizes through a hosted API
- performs server-side recall with project filtering
- receives disagreement metadata with recalled memories
- injects memory into agent context
- exposes five MCP operations: recall, remember, recent, forget, and status
- uses separate per-tool credentials
- fails open so memory failures do not block the agent turn

### 2.2 Verified only as public service contracts

The MCP documentation and client types establish externally visible fields and behavior such as:

```text
memory id
content
scope
tags
has_disagreement
conflict_peers
polarity
evidence_grade
project key
```

They do not reveal how the hosted service computes those outputs.

### 2.3 Vendor claims without inspectable implementation

Atlaso publicly claims:

- verdicts of thin, settled, or contested
- contradiction detection
- automatic retirement of superseded facts
- evidence-sensitive confidence
- freshness decay
- background deduplication and structuring
- Ambient Memory session orientation

Those claims may be real—the public clients expose enough fields to show that at least conflict metadata exists—but the underlying algorithms cannot be audited from the public repositories.

Therefore this note uses the terms:

```text
verified
    demonstrated in public code or API contracts

published result
    reported by Atlaso's benchmark, not independently reproduced here

claimed
    described by Atlaso but not technically inspectable
```

---

## 3. Observable Atlaso architecture

The public implementation supports the following reconstruction.

```text
agent lifecycle event
-> extract user/assistant exchange
-> local chatter / signal gate
-> client-side secret scrub
-> heuristic polarity and scope hints
-> deterministic idempotency key
-> durable local outbox
-> hosted memory deposit API
-> server-side retention / dedup / enrichment
-> hosted recall API
-> project visibility filter
-> conflict metadata
-> context injection
```

### 3.1 Automatic capture

The public Cursor capture code uses a commodity local gate.

It drops:

- empty turns
- trivial acknowledgements
- explicit requests to use memory
- very short low-signal messages

It accepts:

- messages containing preference, decision, requirement, warning, or convention signals
- otherwise substantive messages above a small word threshold

The captured content is approximately:

```text
user message

(assistant: first 400 characters of the assistant response)
```

The client adds hints rather than final semantic conclusions:

```text
polarity: open
pol-hint: positive | cautionary | open
evidence_grade: anecdotal
scope: personal | project
source tool tag
project key when available
auto or manual tag
```

The public comments explicitly say the real worth-keeping gate runs server-side.

### 3.2 Scope routing

Atlaso distinguishes:

```text
personal memory
    follows the user across tools and projects

project memory
    visible only in the matching repository/workspace
```

The public client uses text heuristics and repository identity to route scope. Project identity is derived from the workspace/repository and normalized to avoid accidental duplication across clone forms.

This is operationally useful but semantically modest. It is closer to a namespace router than Menhir's richer scope, identity, authority, and structural-anchor model.

### 3.3 Secret scrubbing

The client scrubs common patterns before transmission, including:

- private keys
- OpenAI/Anthropic-style keys
- GitHub tokens
- AWS access keys
- Google API keys
- Slack tokens
- URI credentials
- JWTs
- bearer tokens
- secret-looking assignments
- high-entropy blobs

Later public fixes extended scrubbing to explicit `remember` calls and recall queries, not only automatic capture.

The server reportedly re-scrubs, so this is defense in depth rather than the only boundary.

### 3.4 Durable delivery

The public clients now use a write-ahead outbox:

```text
capture accepted
-> persist item locally
-> attempt upload
-> retry transient failures later
-> quarantine terminal failures with a reason
```

The idempotency key is derived from content, scope, and project rather than a random UUID, preventing retries from creating duplicates after ambiguous network failures.

This is one of Atlaso's strongest verifiable engineering choices.

### 3.5 Recall

The thin client sends a query and optional project key to the hosted service:

```text
GET /v1/recall?q=<scrubbed query>&limit=<n>&project=<project key>
```

A result can include:

```text
id
content
scope
tags
has_disagreement
conflict_peers
```

The public implementation deliberately has no local recall index. Reads go to the hosted brain so ranking and reconciliation remain server-side.

### 3.6 Conflict presentation

The public renderer adds a marker such as:

```text
[conflict] <memory content> (conflicts with N other notes)
```

This proves that the service exposes disagreement state. It does not prove the quality of the contradiction grouping or supersession decision.

### 3.7 Explicit MCP surface

Atlaso exposes:

```text
recall(query, limit)
remember(text, polarity)
recent(limit)
forget(id)
status()
```

This surface treats memory rows as the externally addressable unit. A user or agent remembers text, receives an ID, and may later delete that memory by ID.

### 3.8 Hosted boundary

The public MCP repository explicitly says:

- Atlaso is a managed service
- there is nothing to self-host
- no server code is published there
- clients connect to `https://mcp.atlaso.ai/mcp`

The public clients repeatedly describe themselves as thin and state that the engine remains on the server.

---

## 4. Atlaso's claimed knowledge model

Atlaso's public product material presents a more ambitious semantic model than the public client code alone reveals.

### 4.1 Confidence verdicts

Every recalled memory is claimed to carry one of three interpretations:

```text
thin
    one weakly supported note; treat as a lead

settled
    memories agree; safe to act on

contested
    memories disagree; inspect both sides
```

This resembles a user-facing projection over confidence and conflict state.

### 4.2 Supersession

Atlaso claims that when a fact changes:

```text
old fact
-> contradiction detected
-> newer fact selected
-> old fact marked superseded/retired
-> current recall favors the new fact
```

The website illustrates launch-date changes and a freshness curve. No public code reveals the semantic-slot, update-detection, or temporal decision logic.

### 4.3 Evidence accumulation

Atlaso claims confidence grows when work repeatedly confirms a memory. Public deposits include an `evidence_grade`, but automatic captures currently send `anecdotal`; the server-side promotion rules are not visible.

### 4.4 Background enrichment

Paid plans advertise a nightly worker that turns raw captures into cleaner, structured memories that are tagged, graded, and deduplicated.

This suggests at least two representations:

```text
raw capture/deposit
-> enriched memory
```

The relationship between these records—replacement, derivation, merge, or independent rows—is not public.

---

## 5. Architectural comparison

### 5.1 Surface overlap

| Capability | Atlaso | Menhir |
|---|---|---|
| Automatic capture | Yes, hook-driven | Yes, episode ingest / harness integration |
| Automatic recall | Yes, pre-prompt/session-start | Yes, recall/context surfaces |
| Personal/project scope | Yes | Namespace, project, structural anchors, provenance |
| Raw source preservation | Raw deposits appear to remain available | Episodes and source-grounded evidence are foundational |
| Conflict signal | `has_disagreement`, conflict peers | Assertions, conflict groups, review and fold semantics |
| Supersession | Claimed | Explicitly modeled and benchmarked in the assertion/View direction |
| Confidence | Claimed thin/settled/contested | Evidence, source confidence, belief state, review tiers |
| Current/historical state | Claimed current retirement | Deterministic current and historical Views are architectural primitives |
| Entity identity | Not publicly described | Canonical identity, aliases, merge/split continuity |
| Valid time vs learned time | Not publicly described | Explicit temporal distinction in the design |
| Exact evidence spans | Not publicly exposed | Typed assertion provenance includes source/evidence anchors |
| Rebuildable state | Not publicly described | State is a fold over durable assertions/events |
| Cross-tool sync | Strong product implementation | Possible through shared service, not current product center |
| Local/self-hosted core | No | Yes/local-first architecture |

### 5.2 Likely Atlaso unit of truth

The public contract suggests this external model:

```text
memory row/deposit
    id
    content
    scope
    tags
    polarity/evidence hints
    disagreement metadata
```

The row is recalled, listed, and deleted as one unit.

The server may internally use a richer claim graph, but there is no public evidence for it.

### 5.3 Menhir unit of truth

Menhir's architecture separates:

```text
source episode/evidence
-> typed assertion
-> canonical entity and semantic slot
-> deterministic reconciliation/fold
-> current or historical View
```

The assertion is an observation. The View is the reconstructed state.

This distinction enables:

- several source assertions supporting one current fact
- one assertion being superseded without deleting its evidence
- rebuilding after identity correction
- valid-time versus learned-time reasoning
- history queries independent of retrieval phrasing
- explicit uncertainty about the current state

### 5.4 Core unresolved question

The architectural question Atlaso cannot answer publicly is:

> Does the hosted engine maintain evidence-bearing assertions and rebuildable state, or does it attach confidence/conflict/supersession metadata to memory rows?

Those approaches can look identical in a product UI but behave differently under:

- buried corrections
- simultaneous conflicting sources
- entity aliases
- retroactive facts
- merge/split repairs
- temporal questions
- deletion of one supporting source
- reprocessing after an extractor improvement

Until the engine is published or independently probed, Menhir should not assume either implementation.

---

## 6. Benchmark comparison

### 6.1 Atlaso's published LongMemEval-S result

Atlaso reports a matched-condition study on all 500 LongMemEval-S questions with:

- one shared Qwen 3.5-9B answer reader
- four judges
- label-blind answer normalization
- Atlaso and mem0-default arms

Atlaso's reported end-to-end scores are:

| Judge | Atlaso |
|---|---:|
| Haiku 4.5 strict | 61.8% |
| mem0 verbatim | 56.4% |
| GPT-5 permissive | 57.8% |
| GPT-4o strict | 69.2% |

The homepage also advertises 92% evidence recall and approximately 67% full-haystack QA. These are not the same metric. The research paper's full 500-question retriever-reader table reports Atlaso R@5 of 64.9% on the 479 questions with gold session labels.

The correct reading is:

> Atlaso's headline retrieval percentage must not be compared to end-to-end QA accuracy.

### 6.2 Menhir's current result

Menhir has recently reached roughly 90% on the official **LongMemEval Oracle** split.

That result measures the semantic-memory path after the benchmark has removed distractor sessions. It is not an oracle-assisted modification; `longmemeval_oracle` is an official dataset variant.

### 6.3 What can and cannot be concluded

The two results are not directly comparable:

```text
Menhir ~90%
    LME-Oracle
    evidence sessions only
    strong measure of ingest/state/answer correctness

Atlaso 56.4–69.2%
    LME-S
    evidence mixed with distractors
    measures retrieval + substrate + answer reader
```

What can be said:

- Atlaso does not currently publish an end-to-end score near Menhir's Oracle-split answer accuracy.
- Menhir has not yet demonstrated equivalent full-haystack LME-S retrieval under the same reader/judge protocol.
- The remaining uncertainty is retrieval and distractor resistance, not whether Atlaso has a stronger public semantic-correctness score.

### 6.4 Atlaso's LoCoMo failure is especially relevant

Atlaso reports 24.0% on a 200-question adversarial LoCoMo subset versus mem0-default at 35.5%.

Its own analysis says contradiction-aware retrieval sometimes:

- over-abstained
- returned a planted distractor with high confidence

This is a load-bearing warning for Menhir:

> Conflict awareness is not automatically conflict correctness.

A system may become more cautious while still failing to establish the correct current state.

### 6.5 Benchmark reproducibility caveat

Atlaso's research page says the full study is reproducible from an open repository containing scripts, JSONL runs, prompts, and public protocol tags.

As of this review, those benchmark files and tags were not found in the public Atlaso GitHub organization, which exposes connector/documentation repositories rather than the engine or research harness. The artifacts may have moved or become private.

Until located, treat the benchmark as:

```text
vendor-published
methodologically described
not independently reproduced here
```

---

## 7. What Menhir should borrow

### 7.1 Durable local outbox

Memory capture should be durable before the network or enrichment path begins.

Recommended invariant:

> Once a capture is accepted locally, a transient process/network/server failure cannot silently erase it.

Menhir should ensure every harness integration has:

- write-before-send durability
- deterministic idempotency keys
- retry classification
- terminal quarantine with reason
- visibility into pending/failed deliveries

### 7.2 Idempotency as memory identity at the transport boundary

Atlaso derives stable client IDs so ambiguous retries cannot duplicate a memory.

Menhir's semantic identity is richer, but transport idempotency still needs a simple deterministic boundary before extraction.

### 7.3 Scrub both reads and writes

Atlaso fixed a subtle leak: explicit memory-search queries can contain secrets just as writes can.

Menhir privacy review should cover:

```text
ingest text
explicit remember calls
recall queries
context-generation prompts
telemetry
error logs
quarantine payloads
```

### 7.4 Per-tool credentials and revocation

Atlaso moved from one device-wide bearer to separate credentials for each connected tool. That allows one integration to be removed without disabling all others.

For a future hosted Menhir deployment, this is a useful capability boundary:

```text
user
  -> device
      -> tool-specific principal
          -> scoped rights and audit
```

### 7.5 Fail-open agent integration

Memory should not brick the host agent.

Atlaso bounds recall latency and treats failures as empty memory rather than a failed user turn. Menhir should retain the same host-safety rule while making omissions observable through diagnostics.

### 7.6 Conflict verdicts at the consumption boundary

Even when a consumer does not receive the entire belief graph, it should know whether context is:

```text
settled
contested
thin
historical
unknown
```

Menhir's View/context renderer should expose a compact verdict backed by evidence—not only internal confidence numbers.

### 7.7 Cross-tool memory as an integration target

Atlaso correctly treats memory as account/project infrastructure rather than a feature trapped inside one agent.

Menhir should keep its provider and protocol boundaries tool-neutral even while current development focuses on benchmark correctness.

### 7.8 Capture-quality telemetry

Atlaso records content-free counters for:

- capture attempts
- accepted items
- drop reasons
- time distribution

Menhir's ingest benchmark should similarly separate:

```text
turn observed
turn admitted
assertion proposed
assertion grounded
assertion folded
assertion recalled
answer correct
```

### 7.9 Matched-reader, multi-judge evaluation

Atlaso's benchmark methodology has a good shape even if its scores are moderate:

- fixed fixture
- fixed answer reader
- multiple judges
- label-blind normalization
- per-question outputs
- explicit losses

Menhir should borrow the evaluation discipline.

---

## 8. What Menhir should not copy

### 8.1 Do not make server-only reconciliation the trust boundary

A hosted black box can produce polished verdicts without making state repair, provenance, or failure analysis inspectable.

Menhir's core advantage should remain:

- locally inspectable state
- reproducible folds
- source-grounded assertions
- explicit identity decisions
- diagnosable current-state construction

### 8.2 Do not collapse an exchange into one durable fact unit

The public Atlaso capture sends the user message plus a short assistant excerpt as one deposit. That is useful raw evidence but too coarse as the final semantic unit.

One exchange can contain:

- several independent facts
- a correction
- a rejected proposal
- a future plan
- assistant speculation
- a question with no assertion

Menhir should retain raw turns while extracting independently governed assertions.

### 8.3 Do not let row-level conflict metadata substitute for semantic slots

Two semantically similar memories may not conflict. Two lexically dissimilar memories may update the same slot.

The benchmark target is not:

```text
find another similar row and mark disagreement
```

It is:

```text
bind claims to entity + semantic slot + time
then reconcile the state
```

### 8.4 Do not use automatic capture as automatic trust

Atlaso's default gate admits many substantive turns and sends `evidence_grade: anecdotal`.

Menhir must preserve the difference between:

```text
captured evidence
candidate assertion
admitted durable assertion
current View
```

### 8.5 Do not use confidence labels without receipts

A `settled` label should be explainable through:

- supporting assertions
- contradictory assertions
- authority and source type
- temporal ordering
- fold rule
- omitted evidence

Otherwise the label is product rhetoric rather than a trustworthy semantic object.

### 8.6 Do not equate high retrieval recall with knowledge correctness

Atlaso's own paper demonstrates that higher session R@5 can yield lower QA. Menhir should continue measuring every stage separately.

---

## 9. Recommended benchmark additions

### 9.1 Atlaso-shaped raw-deposit baseline

Create a deliberately simple baseline:

```text
raw user/assistant exchange deposits
personal/project scope
BM25 retrieval
top-k deposits
shared answer reader
```

This tests how much Menhir's semantic machinery adds beyond a strong scoped raw-turn substrate.

### 9.2 Conflict-verdict fixture

For each case, score both state correctness and verdict correctness:

```text
single weak statement
    expected: thin

repeated agreeing statements
    expected: settled

simultaneous disagreement
    expected: contested

explicit correction
    expected: new current state + old historical, not merely contested

retroactive correction
    expected: valid-time repair
```

### 9.3 Planted-distractor fixture

Use LoCoMo-shaped cases where a plausible distractor shares terms with the answer.

Measure:

- current-state accuracy
- false settled rate
- false abstention rate
- distractor confidence
- evidence trace correctness

### 9.4 Scope-isolation fixture

Store similar project facts in two repositories plus one personal preference.

Measure:

- cross-project leakage
- personal-memory under-recall
- project-key stability across clone URL forms
- unattributed-memory behavior

### 9.5 Delivery durability fixture

Fault-inject:

- process killed after local accept
- timeout after server commit
- 429/5xx
- malformed 2xx response
- duplicate lifecycle events

Expected result:

```text
no lost capture
no duplicate durable episode
visible quarantine for terminal failures
```

### 9.6 Evidence deletion fixture

Delete one supporting source memory.

A row-based system may simply delete the row. Menhir should recompute the View confidence and current state from remaining assertions.

### 9.7 Re-extraction fixture

Improve the extractor or identity resolver and replay the same evidence.

Menhir should demonstrate that current state can be rebuilt without destructive migration of raw sources.

---

## 10. Positioning implications

### Weak Menhir claim

> Menhir gives AI tools automatic long-term memory with contradiction detection and confidence.

Atlaso already markets exactly that.

### Stronger Menhir claim

> Menhir is an inspectable evidence-to-state engine. It preserves source episodes, extracts typed assertions, resolves identity and semantic slots, and deterministically rebuilds current and historical Views—so corrections, contradictions, and temporal questions can be audited rather than hidden behind a confidence label.

### Short contrast

> Atlaso productizes shared memory across tools. Menhir makes the knowledge state reconstructable.

### Benchmark contrast

> Atlaso has a stronger public integration story. Menhir currently has the stronger observed semantic-answer result on the official evidence-only split, but still owes an end-to-end full-haystack result.

---

## 11. Near-term implications

### Immediate

- Add Atlaso to the external-eval watch list.
- Preserve Atlaso's 56.4%–69.2% LME-S range as a no-oracle product baseline.
- Add a raw scoped-deposit/BM25 arm to archolith-bench.
- Keep Menhir's ~90% result labeled `LongMemEval Oracle`, not directly compared as an equivalent split.
- Add false-settled and false-abstention metrics to temporal/conflict fixtures.

### Soon

- Add a durable harness outbox and explicit delivery receipts where not already present.
- Expose compact View verdicts with evidence receipts.
- Add cross-project scope leakage fixtures.
- Add a multi-judge fixed-reader benchmark mode.

### Later

- Test a cross-tool Menhir service through the same tool-neutral protocol.
- Compare Menhir's deterministic View with Atlaso-style nightly enrichment.
- Revisit Atlaso if its server implementation, benchmark repository, or architecture paper becomes public.

---

## 12. Final classification

```text
Atlaso public clients
    robust thin integration layer
    automatic capture/recall
    scope, secret scrubbing, durable delivery, conflict rendering

Atlaso hosted engine
    claimed contradiction/confidence/supersession/enrichment system
    closed and not architecturally auditable

Menhir
    local-first evidence, assertion, identity, temporal fold, and View engine
    benchmark focus: knowledge correctness and reconstructability
```

The durable conclusion is:

> Atlaso validates the demand for one automatic memory across tools and offers several strong operational patterns. It does not currently collapse Menhir's architectural differentiation, and its published end-to-end QA is materially below Menhir's Oracle-split result while remaining more demanding on retrieval.

---

## Sources

- Atlaso product page: <https://www.atlaso.ai/>
- Atlaso LongMemEval-S study: <https://www.atlaso.ai/research/memory-benchmark>
- Atlaso vs mem0 summary: <https://www.atlaso.ai/vs/mem0>
- Hosted MCP documentation: <https://github.com/atlaso-labs/mcp>
- Claude Code connector: <https://github.com/atlaso-labs/claude-code>
- Cursor connector: <https://github.com/atlaso-labs/cursor>
- Cursor hosted-client boundary and API types: <https://github.com/atlaso-labs/cursor/blob/main/lib/atlaso.ts>
- Cursor capture heuristics and secret scrub: <https://github.com/atlaso-labs/cursor/blob/main/lib/capture.ts>
- Cursor session-start recall: <https://github.com/atlaso-labs/cursor/blob/main/hooks/recall.ts>
- Cursor conflict rendering: <https://github.com/atlaso-labs/cursor/blob/main/lib/render.ts>
