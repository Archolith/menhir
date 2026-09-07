# Utopia vs Menhir — Prior-Art / Category-Boundary Comparison

**Date:** 2026-09-03  
**Status:** Revision-pinned external comparison; use for positioning, roadmap triage,
architecture review, and benchmark design. It is not a benchmark result.  
**Compared project:** [`deeplethe/utopia`](https://github.com/deeplethe/utopia)  
**Analyzed Utopia revision:** [`80b6036d76993d7ba7d9ec9f2512a2d7ea84a424`](https://github.com/deeplethe/utopia/tree/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424)  
**Release context:** [`v0.1.0-rc3`](https://github.com/deeplethe/utopia/releases/tag/v0.1.0-rc3)  
**Menhir reference revision:** [`4f4969a9987c37343db071b766ce2499c66cde93`](https://github.com/Archolith/menhir/tree/4f4969a9987c37343db071b766ce2499c66cde93)  
**Primary question:** What does Utopia establish as prior art for governed,
bitemporal knowledge, and what should Menhir borrow without becoming a general
enterprise knowledge application?

---

## 1. Executive verdict

Utopia is the strongest adjacent prior art reviewed so far for a complete,
governed, bitemporal knowledge application.

It is not merely a vector store, a thin GraphRAG wrapper, or a prototype that
stops at extraction. At the analyzed revision it ships a document and source
ingestion pipeline, hybrid lexical/vector retrieval, entity resolution,
ontology management, bitemporal facts, review queues, symbolic derivation,
contradiction handling, a decision ledger, multi-user access control, an
application agent, a read-only MCP surface, and a substantial browser
application.

The clean category boundary is:

```text
Utopia
    constructs and governs an organization's changing model of the world

Menhir
    constructs and governs the evidence and context by which coding agents
    understand, change, and explain software
```

A second useful formulation is:

```text
Utopia asks:
    What did the organization know, when did it know it, and which sources
    and ontology rules support that knowledge?

Menhir asks:
    Why did the agent believe this, which repository objects does it concern,
    is the evidence still current, and what code and tests inherit the impact?
```

The overlap is material. Both systems reject overwrite-only memory, preserve
source provenance, distinguish current from historical knowledge, support
review and reversible correction, and expose graph-backed context to agents.
The principal difference is not whether either system has "memory" or a
"knowledge graph." It is the object around which each system makes correctness
claims:

```text
Utopia's center:
    ontology-constrained domain facts extracted from documents and sources

Menhir's center:
    evidence-grounded agent context joined to code structure, engineering
    artifacts, Git state, callers, dependents, and tests
```

Approximate comparison:

```text
conceptual overlap:            40–50%
ordinary user-task overlap:    20–30%
direct replacement risk now:   low
strategic category pressure:   medium
```

Utopia is broader and more productized. Menhir remains substantially deeper in
repository structure, code-linked provenance, change-impact analysis,
agent-facing authority boundaries, and deterministic assertion-to-View state.

The roadmap consequence is **narrowing, not convergence**:

> Menhir should become the strongest code-linked evidence and change-impact
> substrate for agents. It should not become a second enterprise document
> knowledge platform.

---

## 2. Scope, evidence, and limitations

This note compares public code and documentation at fixed revisions.

Primary Utopia sources:

- [`README.md`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/README.md)
- [`docs/pipeline.md`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/docs/pipeline.md)
- [`docs/decisions/0015-recording-a-sentence-is-not-asserting-a-fact.md`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/docs/decisions/0015-recording-a-sentence-is-not-asserting-a-fact.md)
- [`docs/decisions/0017-a-contradiction-points-upstream.md`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/docs/decisions/0017-a-contradiction-points-upstream.md)
- [`crates/utopia-server/src/retrieval.rs`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/crates/utopia-server/src/retrieval.rs)
- [`crates/utopia-server/src/github_issues.rs`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/crates/utopia-server/src/github_issues.rs)
- [`scripts/bench/README.md`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/scripts/bench/README.md)
- [`SECURITY.md`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/SECURITY.md)

Primary Menhir sources:

- [`README.md`](https://github.com/Archolith/menhir/blob/4f4969a9987c37343db071b766ce2499c66cde93/README.md)
- [`.agent/architecture.md`](https://github.com/Archolith/menhir/blob/4f4969a9987c37343db071b766ce2499c66cde93/.agent/architecture.md)
- [`.agent/data_models.md`](https://github.com/Archolith/menhir/blob/4f4969a9987c37343db071b766ce2499c66cde93/.agent/data_models.md)
- [`.agent/default-off-features.md`](https://github.com/Archolith/menhir/blob/4f4969a9987c37343db071b766ce2499c66cde93/.agent/default-off-features.md)
- [`docs/evaluation.md`](https://github.com/Archolith/menhir/blob/4f4969a9987c37343db071b766ce2499c66cde93/docs/evaluation.md)
- [`.agent/plans/menhir-projection-realization-coverage-implementation.md`](https://github.com/Archolith/menhir/blob/4f4969a9987c37343db071b766ce2499c66cde93/.agent/plans/menhir-projection-realization-coverage-implementation.md)

Two kinds of statement stay separate throughout:

1. **Shipped at the pinned revision.**
2. **Open or planned direction.**

In particular, Utopia pull request
[#271](https://github.com/deeplethe/utopia/pull/271) describes a recording-time
`as_of` read contract that was not merged into the analyzed revision. It is
useful design evidence, not a shipped capability. Menhir's open core-promotion
and admission-authority pull requests are treated the same way.

This comparison does not install both projects against a shared corpus, measure
latency, inspect private deployment configuration, or claim a quality winner.
Utopia publishes no directly comparable Menhir benchmark at this revision, and
Menhir has no public enterprise-document benchmark that would make a broad
head-to-head score meaningful.

---

## 3. What Utopia actually is

Utopia describes itself as an enterprise world model. The implementation is
more concrete than that phrase suggests:

```text
sources and uploaded documents
-> parse and chunk
-> embed
-> make searchable
-> extract entities and facts
-> resolve identity
-> type against an ontology
-> grow or review missing ontology terms
-> maintain bitemporal fact history
-> check ontology constraints
-> optionally derive facts
-> expose search, chat, graph, review, and data-query surfaces
```

Its product surface includes:

```text
document library
scheduled source synchronization
Tantivy full-text search
pgvector similarity search
RRF fusion
chat with passage citations
entity and fact graph
ontology workbench
duplicate and conflict review
symbolic derivation
decision/audit ledger
database mapping and read-only querying
multi-user knowledge-base roles
personal access tokens
read-only MCP tools
```

Its source coverage is substantially broader than Menhir's. The pinned README
names office files and text formats, web pages, RSS, GitHub, Jira, object
storage, and API ingestion. Mounted databases can be queried through Postgres,
Trino, Databricks, and Snowflake adapters.

The deployment shape is also product-oriented:

```text
one Rust application
one Postgres database with pgvector
embedded Tantivy index
table-backed job queue
React browser application
Docker quick start
```

Menhir is deliberately a different shape:

```text
Python/FastAPI service
Neo4j + Graphiti semantic graph
MCP, REST, CLI, and local explorer
repository and artifact indexing
operator-controlled deployment
single-operator trust model
external coding agents as the primary clients
```

Utopia is therefore not "Menhir in Rust." It is a full knowledge application
whose agent interfaces sit on top of its domain graph. Menhir is an evidence
and code-intelligence service whose user experience is usually supplied by an
external agent client.

---

## 4. High-level architecture mapping

| Concern | Utopia | Menhir | Similarity |
|---|---|---|---|
| Durable source | Document, synchronized source item, conversation memory | Episode, TurnEvidence, Git diff, file event, WorkArtifact, structural scan | Medium |
| Primary semantic object | Ontology-constrained entity fact | Governed memory, typed assertion, artifact, or rebuildable View | Medium |
| Raw source retention | Documents, chunks, evidence quotes, source clocks | Episodes/evidence, exact source spans, artifact bytes remain in Git | High in intent |
| Current state | Live fact intervals and graph queries | Deterministic current Views plus governed memory lifecycle | Medium |
| Historical state | Valid-time history; recording-time fields stored | Assertions/events plus current/history Views and provenance | Medium–high |
| Identity | Entity rows, aliases, merge/revert, type-aware resolution | Canonical entities, merge/unmerge, binding-pending assertions, structural identities | High |
| Governance | Review queues, human decisions, roles, append-only ledger | Candidate/persistent/promoted state, source authority, conflicts, provenance, operator tiers | High |
| Retrieval | Tantivy BM25 + pgvector + RRF; graph/data tools in agent | Graphiti hybrid candidates plus structural, file-linked, temporal, artifact, and authority lanes | Medium–high |
| Reasoning | Ontology axioms and forward-chained derivations | Deterministic folds, policy evaluation, structural traversal; no general domain reasoner | Low–medium |
| Code structure | GitHub/Jira as content sources | Files, symbols, imports, calls, tests, endpoints, dependencies, cross-project references | Low |
| Change impact | Knowledge and decision changes | Coverage-aware blast radius, callers, dependents, affected tests, related evidence | Low |
| Product UI | Complete multi-user application | Developer/operator explorer; agents usually provide the UX | Low |
| Agent surface | Built-in chat agent; external MCP read-only at the pin | External MCP/REST agent service with write and operator surfaces | Medium |
| Deployment goal | Organizational knowledge deployment | Local/operator-controlled agent infrastructure | Medium |
| Benchmark posture | Focused extraction/resolution corpora; no public headline system score found | Diagnostic LongMemEval path plus mechanism-specific acceptance and activation gates | Medium |

The table explains why broad feature counting is misleading. Utopia wins
categories that Menhir does not intend to own; Menhir wins categories that
Utopia does not currently model.

---

## 5. The category boundary: world knowledge versus software understanding

### 5.1 Utopia's center of gravity

Utopia's ontology gives domain facts a contract:

```text
class
relationship or attribute
domain
range
cardinality
symmetry / asymmetry
transitivity
inverse
disjointness
temporal interpretation
```

Extraction, validation, review, and derivation all use this contract. The graph
is intended to describe organizations, people, events, products, policies,
research, finance, law, and other world domains. GitHub issues and Jira tickets
become another source of domain statements.

### 5.2 Menhir's center of gravity

Menhir's distinguishing join is:

```text
source episode or engineering artifact
-> governed claim or memory
-> canonical identity and lifecycle state
-> repository path and Git evidence
-> files, symbols, imports, calls, endpoints, and dependencies
-> affected tests and transitive blast radius
```

A coding agent does not merely need to know that an architecture decision
exists. It needs to know:

```text
which files and symbols implement it
which previous failure changed the decision
whether the referenced code has drifted
which callers and tests inherit the change
whether an incomplete index makes a negative answer unsafe
which exact evidence should be shown in the context packet
```

That is not a generic ontology problem. It requires a deterministic software
structure graph and explicit coverage semantics.

### 5.3 Strategic implication

If Menhir is positioned as:

```text
long-term AI memory
bitemporal knowledge graph
governed GraphRAG
world model for agents
```

then Utopia creates severe category ambiguity because it visibly ships a
broader application in each of those frames.

If Menhir is positioned as:

```text
code-linked evidence and governed context for coding agents
software-understanding infrastructure
agent change provenance and blast-radius analysis
```

then Utopia is complementary prior art rather than a substitute.

The strongest one-line distinction is:

> Utopia knows the organization. Menhir knows why the agent changed the code
> and what that change affects.

---

## 6. Units of truth and authority

### 6.1 Utopia: fact ledger under an ontology

Utopia stores entities and facts with source evidence and temporal fields. A
new fact may:

```text
remain live beside an older compatible fact
close an older conflicting fact's valid interval
be rejected
be held for review
support or block a derivation
```

Derived facts are stored separately from asserted facts. An asserted fact wins
when a derivation contradicts it, and the blocked derivation can remain visible
with its proof chain.

This is materially stronger than a graph in which all edges are equivalent or
where derived relations are indistinguishable from extracted assertions.

### 6.2 Menhir: evidence, assertion, and projection are separate authorities

Menhir's write-side direction is:

```text
immutable source evidence
-> grounded typed assertion or event
-> admission and canonical binding
-> deterministic fold or reconciliation
-> disposable, rebuildable current/history View
-> recall or another authority lane
```

The View is not the evidence. It is not the interpretation event. It is not a
mutable memory row pretending to be all three. It is a query-sufficient
projection that can be audited and rebuilt from durable contributors.

Menhir also keeps several dimensions separate that a single fact-confidence
field can blur:

```text
source authority
interpretation confidence
review state
currentness
conflict state
projection authority
scope and client authority
```

### 6.3 Relative consequence

Utopia's model is especially expressive when the knowledge domain has an
explicit ontology and when forward derivation is valuable.

Menhir's model is especially expressive when:

```text
one source statement contains several claims
identity binding may change later
a current value must be rebuilt after policy changes
an assertion can exist without earning recall authority
multiple evidence records contribute to one current state
code or artifact locators move while semantic identity survives
projection correctness must be certified independently of retrieval
```

Neither architecture subsumes the other without losing its center.

---

## 7. Ingestion, failure accounting, and raw-evidence fallback

Utopia's pipeline documentation is unusually valuable because it maps not only
the happy path but the points where information disappears.

The sequence is intentionally two-phase:

```text
parse -> chunk -> embed -> searchable/askable
                         -> asynchronous extraction -> graph
```

A document becomes useful for search before graph extraction finishes. Long
documents can therefore remain available even when entity/fact processing is
slow or partially fails.

The pipeline records concrete extraction outcomes such as:

```text
truncated model reply
malformed item
non-entity-shaped name
low confidence
missing subject
attribute domain mismatch
missing or invalid literal value
missing relation object
direction corrected
unrecoverable domain mismatch
```

The distinction between a trace and a drop is explicit. For example,
`direction_corrected` records a deterministic repair rather than pretending the
model returned the correct direction.

Menhir already stores source episodes before enrichment and can recall raw
evidence through several paths, but its most rigorous coverage work currently
starts further downstream:

```text
typed assertion lifecycle
binding status
fold role
projection status
View parity
realization agreement or disagreement
```

The synthesis worth borrowing is an end-to-end accounting invariant:

```text
Every accepted source record is:

1. durably and directly retrievable as source evidence, and
2. either represented by admitted claims/projections,
   or accompanied by an explicit durable reason why not.
```

A Menhir audit should eventually be able to walk:

```text
source recorded
-> perception attempted
-> interpretation proposed / abstained / failed
-> admission accepted / rejected
-> identity bound / pending
-> projection materialized / not required / failed
-> recall reachable / intentionally withheld
```

This does **not** require another competing ledger. It should extend Projection
Coverage and Realization Coverage using their existing assertion, observation,
and View identities.

Useful counters:

```text
accepted evidence with no perception attempt
perception attempt with no durable interpretation receipt
interpretation rejected or abstained, by stable reason
admitted assertion with no certified projection
current View not reachable by any intended recall lane
source available only through raw-evidence fallback
```

The failure mode this prevents is:

> The system successfully stored what the user said but silently failed to make
> the important part available to future reasoning.

---

## 8. Interactive writes: recorded is not asserted

Utopia's decision record
[`0015`](https://github.com/deeplethe/utopia/blob/80b6036d76993d7ba7d9ec9f2512a2d7ea84a424/docs/decisions/0015-recording-a-sentence-is-not-asserting-a-fact.md)
starts from a concrete failure:

```text
user asks the system to remember a sentence
assistant claims it was recorded as intended
ontology cannot represent the requested predicate
graph receives a high-confidence but semantically empty edge
user has no way to see the mismatch
```

Its resolution separates two acts:

```text
record the original sentence as searchable source material
hold extracted facts outside the live graph until a human confirms them
```

The important lesson is broader than the exact confirmation UX:

```text
recorded != interpreted != admitted != projected != recallable
```

Menhir should preserve its more general source-bound admission model rather than
requiring a human click for every write. An external agent should be allowed to
propose knowledge, while durable source grants and domain policy determine the
maximum authority that proposal can earn.

What Menhir should copy is the **honesty of the write receipt**. A write
response should separately report:

```text
source status
enrichment status
interpretation status
admission status
requested and effective authority
binding status
projection status
recall eligibility
```

Illustrative shape:

```json
{
  "source": {
    "status": "recorded",
    "source_id": "..."
  },
  "enrichment": {
    "status": "queued"
  },
  "interpretations": {
    "status": "not_yet_available"
  },
  "authority": {
    "requested": "persistent",
    "effective": null,
    "status": "pending"
  },
  "projection": {
    "status": "not_yet_evaluated"
  }
}
```

A synchronous response must not claim a semantic outcome that depends on later
asynchronous extraction.

This is a strong candidate for near-term implementation because the underlying
Menhir concepts already exist. The work is largely a transport-neutral result
contract and truthful formatting, not a new memory architecture.

---

## 9. Identity resolution and reversible correction

Utopia resolves duplicates in multiple stages and favors splitting over an
unsafe merge. Suspicious pairs can be reviewed, merges can be reverted, aliases
participate in future resolution, and pending pairs are redirected when one of
their entities is merged.

The design record is useful because it documents a sequence of interacting
failures:

```text
same-type entities were classified as incompatible
successful merges removed alias bridges from later recall
closing pending pairs after a merge made some doubts permanently unreachable
```

Each fix exposed the next hidden layer. The benchmark corpus was valuable not
only because a score improved, but because it exercised repeated entity
mentions across documents and made identity continuity observable.

Menhir already has the more relevant identity contract for its domain:

```text
canonical semantic entities
merge and exact unmerge
binding-pending typed assertions
structural repository identities
artifact identity separate from source embodiment and locator
projection rebuild after binding or identity changes
```

The borrowing opportunity is primarily evaluative:

```text
alias introduced after earlier evidence
entity renamed across source history
same name, distinct entity
merge followed by unmerge
pending decisions redirected after identity changes
source or artifact locator changes without identity loss
```

These should be explicit cross-layer fixtures, not isolated entity-matcher unit
tests. The acceptance condition is not merely that the final entity count looks
reasonable; every dependent assertion, View, artifact relation, and structural
anchor must still resolve correctly.

---

## 10. Contradictions as upstream diagnostic signals

Utopia's contradiction design is one of the strongest parts of the project.

It rejects the idea that a conflict queue should ask a human to choose one of
two unexplained edges. A contradiction usually points to an upstream error:

```text
stale knowledge
misread extraction
wrong entity merge
over-strict or incorrect ontology
```

The review surface therefore includes:

```text
the derived and asserted claims
their source evidence and time intervals
the proof chain
computed diagnostic hints
repairs that act on the likely cause
```

A disputed assertion remains visibly disputed where it is consumed. Blocked
derivations can appear as ghost edges rather than disappearing silently.
Systemic rule conflicts are aggregated by cause rather than flooding the
reviewer with thousands of identical cards.

Menhir should generalize this into a transport-neutral diagnostic packet:

```text
DiagnosticPacket
    finding_kind
    affected object
    current assertion / View / artifact / structural record
    competing, missing, or stale evidence
    likely upstream layer
    diagnostic signals
    affected files, symbols, callers, tests, and artifacts
    repair options
    authority required for each repair
    reversibility and predicted consequences
```

Candidate finding kinds:

```text
assertion conflict
identity ambiguity
stale code anchor
projection parity failure
unprojected admitted assertion
source-authority denial
orphaned artifact source
incomplete structural index
```

The Menhir-specific improvement is downstream impact. A repair should be able
to state not merely:

```text
rebind this assertion
```

but:

```text
rebind this assertion; doing so will retire or rebuild these Views,
change these recalled memories, alter these artifact relationships,
and affect the blast-radius context for these files and tests
```

Scalability rule:

```text
bounded, independently actionable findings -> individual packets
systemic failure with one shared cause       -> aggregate + examples
```

That rule should apply to projection audits, registration completeness,
provider failures, and source-ingestion defects.

---

## 11. Time: stored clocks versus queryable clocks

Utopia stores valid-world time on facts and transaction/recording-time fields
that preserve when the system learned or invalidated them. Its public language
therefore justifiably describes a bitemporal ledger.

However, a separately reviewed open design PR outside the pinned revision,
[#271](https://github.com/deeplethe/utopia/pull/271), identifies an important
gap: storing the second clock does not guarantee that all reads can rewind it.
The PR proposes distinct `at` and `as_of` parameters rather than one ambiguous
time control:

```text
at
    when the claim held in the world

as_of
    when the system held the claim
```

The distinction matters because these are different questions:

```text
What do we currently believe was true in March?

What did we believe in April was true in March?
```

A single "time travel" slider can return a plausible but wrong interpretation.

Menhir already separates `valid_at` and `learned_at` in typed state. The Utopia
design should trigger a read-contract audit, not a new temporal architecture.

Audit questions:

```text
Can current assertion and View reads constrain both axes independently?
Can historical Views distinguish world time from knowledge time?
Do merge/unmerge and binding changes have queryable recording-time history?
Can artifacts be resolved as they were known at a prior point?
Does raw evidence retrieval preserve the source version available at the query's
knowledge-time boundary?
Do helper functions centralize current-only predicates, or can one forgotten
filter silently violate the contract?
```

Recommended invariant:

> No read may collapse world time and belief time into one ambiguous `as_of`.

Recommended API vocabulary:

```text
valid_at
known_as_of
```

The exact names may differ, but the axes should remain explicit through domain,
repository, service, and transport layers.

---

## 12. Ontology and reasoning: genuine prior art, mostly outside Menhir core

Utopia's ontology is not only a display taxonomy.

It can constrain and support:

```text
entity classification
predicate selection
relation direction
domain and range validation
class disjointness
functional and inverse-functional conflicts
symmetry, asymmetry, transitivity, and irreflexivity
inverse and sub-property derivation
ontology growth from repeated unknown terms
database-to-ontology mapping
```

Its derivation engine stores inferred facts separately and preserves proof
chains. It also distinguishes static ontology defects from conflicts that
emerge only when rules meet real data.

This establishes strong prior art for:

```text
ontology-backed LLM extraction
human-governed ontology growth
separate asserted and derived fact stores
proof-carrying symbolic derivation
ontology-aware conflict review
```

Menhir should **not** respond by adding a general OWL workbench to core.

Menhir's authoritative domains are narrower and better expressed through:

```text
registered evidence and assertion kinds
domain-specific admission policies
deterministic fold laws
repository structure
explicit artifact relations
source and client authority
```

A general ontology engine could become a downstream program built on Menhir,
but making it a core dependency would introduce a second semantic authority
beside the existing typed assertion and structural contracts.

What Menhir can safely borrow is a narrower principle:

> Unknown semantics should remain unknown while the original source wording and
> evidence remain durable.

An extractor that cannot bind a predicate or type should not silently degrade
to a generic `RELATES_TO` claim and then allow that edge to acquire authority
through repetition or retrieval.

---

## 13. Retrieval and agent interfaces

Utopia's basic retrieval function is intentionally simple:

```text
Tantivy BM25 candidates
+ optional pgvector candidates
-> reciprocal-rank fusion
-> ordered chunks
```

If embeddings are unavailable, it degrades to lexical retrieval rather than
making the entire knowledge base unusable.

The application agent can then combine:

```text
document search
whole-document reads
entity lookup
entity facts at a date
graph changes over a period
mounted database queries
```

Its external MCP surface is narrower at the pinned revision. It exposes
read-only tools and deliberately withholds interactive memory and production
database querying until their evidence and authorization contracts are
resolved.

Menhir's retrieval problem is different. It must combine:

```text
semantic and lexical memory candidates
source episodes
file-linked evidence
repository structure
imports and callers
tests and endpoints
current and historical typed Views
engineering artifacts
authority and conflict state
```

The lesson is not to simplify Menhir to BM25 + vectors. The useful lessons are:

1. **Graceful degradation.** Raw and lexical evidence should remain usable when
   model-backed enrichment or embedding infrastructure is unavailable.
2. **Whole-source continuation.** After a passage hit, an agent needs a bounded
   way to read the containing evidence object rather than relying on detached
   chunks.
3. **Capability honesty.** A transport should expose only the tools whose
   authority and evidence contracts are complete.
4. **One definition of a tool.** Schema, execution, authorization, and
   documentation should derive from a shared registration surface rather than
   drift independently.

Menhir already has strong registration and startup-validation direction. The
Utopia comparison reinforces the need for a completeness invariant:

```text
registered kind or tool
-> schema/index activation
-> writer
-> reader
-> reconciliation/repair
-> API or MCP exposure
-> authorization declaration
-> documentation
```

Registration should fail closed when one required leg is absent.

---

## 14. GitHub history as source evidence

Utopia's GitHub issues source makes one particularly good decision: current
state is not enough.

For each issue it attempts to preserve:

```text
issue body and current status
created and closed times
labels and assignees
comments
status, label, and assignment events
```

The implementation accepts an N+1 event-fetch cost because the cheaper
repository-level endpoint could omit older issue events in active repositories.
The authors explicitly choose a more expensive correct history over a cheaper
silently incomplete one.

This idea maps strongly to Menhir, but the implementation should differ.

Recommended separate adapter:

```text
archolith-github-history
```

The adapter should own GitHub-specific concerns:

```text
authentication
pagination
rate-limit recovery
webhook and polling reconciliation
issue, PR, review, and timeline endpoint behavior
payload versioning
```

Menhir core should own:

```text
source event identity
artifact identity
provenance
temporal ordering
fold and projection contracts
repository/code relationships
authority and recall
```

Unlike Utopia's rendered-document approach, Menhir should preserve immutable
GitHub event IDs and source payload provenance as the authority, then derive:

```text
issue or PR timeline View
current issue/PR state View
WorkArtifact identity
links to commits, changed files, symbols, tests, decisions, and agent evidence
```

High-value queries unlocked:

```text
Which PR discussion changed this architecture decision?
What issue state did the agent believe when it acted?
Why was this implementation chosen over the rejected approach?
Which closed issue was reopened after the related code changed?
What changed between the last passing test and this regression?
Who last changed the decision, and which evidence did they rely on?
```

This is the highest-value separate project suggested by Utopia because it
strengthens Menhir's existing software-understanding center rather than
broadening away from it.

---

## 15. Productization and deployment

Utopia is substantially ahead in end-user productization:

```text
single Docker-oriented application
complete browser UX
knowledge-base creation and roles
source setup
graph and ontology browsing
review queues
chat
settings
activity and audit surfaces
```

Menhir should not attempt to match this feature for feature.

Applicable lessons:

```text
one obvious operator start path
capability/readiness reporting
stable upgrade and backup guidance
clear default-off feature state
browser surfaces that explain findings and repairs
a truthful distinction between installed, configured, and active
```

Non-applicable lesson:

```text
rewrite Menhir as one Rust binary on Postgres
```

Menhir's Neo4j structural/semantic graph and Graphiti integration are central
to current behavior. A storage rewrite would consume the roadmap while proving
little about context correctness.

The appropriate productization target is a pinned, reproducible deployment
contract through `menhir-deploy`, plus focused operator and review interfaces.
The goal is not a second generic document library.

---

## 16. Security and maturity

Both projects are early and candid about important limits.

At the pinned revision, Utopia's security document says:

```text
LLM credentials and data-source connection strings are stored in clear text
inside Postgres
the default database password must be changed before exposing the database
least-privilege source roles remain necessary
the system should stay on a trusted network until encryption-at-rest work lands
```

Menhir's published security posture says:

```text
single-operator, not multi-tenant
loopback no-auth mode exists under explicit binding constraints
some stored content reaches LLM prompts unsanitized
operator maintenance scripts are privileged, not sandboxes
```

Utopia is at `0.1.0-rc3`; Menhir identifies its current package as `0.2.0`.
Version numbers are not a quality comparison, but they correctly signal that
both systems are still changing contracts and should not be treated as mature
enterprise infrastructure by default.

The competitive conclusion should therefore avoid two errors:

```text
Utopia has a polished UI, therefore all advertised semantics are complete
Menhir has deeper internal invariants, therefore it is operationally mature
```

Both projects have meaningful implemented architecture and meaningful
unfinished seams.

---

## 17. Benchmark and evaluation posture

Utopia's included benchmark harness focuses on extraction and entity/type
resolution. Its documentation records several strong methodological choices:

```text
fresh database for every arm
fixed corpora and truth sets
repeated entities across documents
human-review outcomes do not count as automatic hits
entities never extracted are separated from resolution misses
gold corrections are documented rather than silently conformed to output
cross-domain corpora are used to resist overfitting
```

The fresh-database rule arose after previous resolution runs contaminated later
arms. That is directly relevant to Menhir, where stale Views, prior merges,
review decisions, or benchmark-generated summaries can make later runs
incomparable.

Archolith Bench should make these explicit invariants:

```text
fresh graph or namespace per arm
fixture and truth-set digest recorded
no prior merge, View, review, or lifecycle state leaks between arms
capture failure separated from binding failure
binding failure separated from projection failure
projection failure separated from retrieval failure
gold revisions append-only with rationale
```

One Menhir-specific correction is essential:

```text
review_pending is not automatic task success
review_pending is not necessarily a safety failure
```

Report separately:

```text
automatic correctness
correct abstention/review rate
incorrect assertion rate
incorrect withholding rate
```

The strongest Utopia-shaped benchmark addition is not a broad document-QA
competition. It is a cross-layer accounting suite:

```text
source stored but perception missing
perception abstains with a valid reason
claim admitted but projection absent
projection exists with wrong contributors
identity merge redirects dependent state
source version changes and old evidence remains auditable
diagnostic packet points to the correct upstream repair
```

---

## 18. What Menhir should borrow

### 18.1 End-to-end evidence-to-context accounting

**Priority:** high, after the current projection/realization coverage stack.

Extend existing coverage so every accepted source can be classified from
durable evidence through recallability.

Do not create a parallel truth store. Reuse:

```text
source evidence IDs
realization observations
typed assertion/source keys
admission receipts
binding state
projection work and certification
View contributor receipts
retrieval traces
```

Exit condition:

> Given an accepted source record, Menhir can explain whether and how it became
> usable context, or identify the exact durable reason and repair path when it
> did not.

### 18.2 Honest memory-write receipts

**Priority:** highest near-term mechanism.

Return separate state for source recording, enrichment, interpretation,
admission, authority, binding, projection, and recall eligibility.

Exit condition:

> No caller can mistake queue acceptance or source persistence for successful
> semantic admission.

### 18.3 Generic diagnostic and repair packets

**Priority:** medium-high.

Represent findings with evidence, likely upstream cause, downstream impact,
bounded repair options, required authority, and reversibility.

Exit condition:

> A projection, identity, conflict, artifact, or structure failure can be
> rendered consistently in MCP, REST, CLI, and browser interfaces without each
> surface inventing its own diagnosis.

### 18.4 GitHub history adapter

**Priority:** highest separate project.

Preserve issue/PR/review event history as immutable source evidence and derive
current/timeline artifacts linked to repository structure.

Exit condition:

> Menhir can explain the issue and review history behind a code decision and
> connect that history to the affected files, symbols, and tests.

### 18.5 Dual-clock read audit

**Priority:** medium; perform before adding another temporal API.

Audit whether world-valid time and knowledge/recording time remain independent
through every authoritative read.

Exit condition:

> Menhir can answer both "what is believed now about T?" and "what was believed
> at S about T?" without an ambiguous parameter or scattered current-only
> filters.

### 18.6 Benchmark isolation and gold governance

**Priority:** high in archolith-bench.

Make run isolation, fixture digests, failure-layer attribution, and append-only
gold corrections part of the harness contract.

### 18.7 Registration completeness checks

**Priority:** ongoing.

A registered kind or tool should not be able to exist with an unwired writer,
reader, repair, authorization, or schema leg.

---

## 19. What Menhir should not copy directly

### 19.1 Do not add a general ontology product to core

Domain ontologies and symbolic derivation are Utopia's advantage and fit its
product. Menhir should retain typed domain registration and deterministic
policy/fold contracts.

### 19.2 Do not become an office-document knowledge base

Office parsing, generic enterprise connectors, multi-user knowledge workspaces,
and database chat would blur Menhir's strongest position and duplicate Utopia's
product center.

### 19.3 Do not promote vague semantic edges

A generic relationship may be useful for discovery, but it should not become an
authoritative assertion merely because it is frequently retrieved or repeated.
Unknown predicate identity must remain explicit.

### 19.4 Do not duplicate Git as a version store

Utopia stores document versions because its documents are primary source
objects. Menhir's engineering artifacts should retain stable semantic identity
while Git owns byte history. Menhir should store locators, version handles,
integrity, and observation times—not recreate Git history in Neo4j.

### 19.5 Do not adopt human confirmation as the universal write policy

Interactive confirmation is appropriate for some Utopia fact writes. Menhir
needs source-bound, client-bound, and domain-bound admission that can admit,
downgrade, stage, or reject without requiring a person for every event.

### 19.6 Do not treat application breadth as architecture quality

Utopia's UI and connector breadth are real strengths. They do not establish
that its semantics are superior for code change provenance. Menhir should
measure its own core claims rather than answer breadth with breadth.

---

## 20. Novelty and positioning impact

Utopia narrows several claims Menhir should make carefully.

Menhir should not claim standalone novelty for:

```text
bitemporal knowledge graphs
source-grounded temporal facts
non-destructive correction and supersession
human review of uncertain extracted knowledge
reversible entity merging
separate asserted and derived facts
proof-carrying derivation
contradiction review
proposal-gated interactive memory
append-only knowledge decision ledgers
self-hosted agent access to governed knowledge
```

Utopia publicly occupies these lanes in a broader application.

Menhir's defensible differentiators are:

```text
repository structure as first-class cognitive context
files, symbols, imports, calls, endpoints, dependencies, and tests in one graph
coverage-aware negative answers
transitive blast-radius and affected-test computation
code-linked memories and evidence
stale-anchor handling against current repository state
engineering WorkArtifacts with stable identity and Git-backed embodiment
source-bound admission authority for external agents
deterministic Event -> Fold -> View state
projection and realization coverage
agent action provenance from evidence through code impact
```

Recommended positioning adjustment:

```text
Adjacent category:
    enterprise world models / governed organizational knowledge platforms
    Example: Utopia

Menhir category center:
    code-linked evidence and governed context for coding agents
    software-understanding and change-impact infrastructure
```

The existing Cognitive Infrastructure Platform framing remains useful as a
long-term umbrella, but product-facing copy should lead with the specific
software-engineering job before the broader category claim.

Suggested external line:

> Menhir gives coding agents durable, governed context tied to the code,
> decisions, and tests a change will affect.

Suggested comparison line:

> Utopia governs what an organization knows. Menhir governs the evidence a
> coding agent uses and traces that evidence into software impact.

---

## 21. Recommended follow-up order

```text
Now
1. Add this revision-pinned comparison and update the prior-art index.
2. Amend canonical positioning with Utopia as the adjacent world-model category.
3. Define the transport-neutral honest write receipt.

After the current core-promotion / projection lifecycle work
4. Extend Projection + Realization Coverage into source-to-context accounting.
5. Define the generic DiagnosticPacket and one projection-failure renderer.
6. Run the dual-clock read-contract audit.

Separate project
7. Design archolith-github-history around immutable source events and
   WorkArtifact/structure linkage.

Archolith Bench
8. Enforce fresh-arm isolation and fixture/truth digests.
9. Add cross-layer accounting and correct-abstention metrics.

Explicitly defer
10. General OWL ontology workbench.
11. Broad enterprise document and database knowledge product.
12. Storage-stack rewrite for packaging aesthetics.
```

The first mechanism should be the write receipt because it uses concepts Menhir
already has and immediately prevents a dangerous false claim.

The first larger build should be the GitHub history adapter because it compounds
Menhir's unique code-and-artifact graph rather than pulling the project toward
generic enterprise search.

The source-to-context accounting invariant is the highest architectural value,
but it should extend the current coverage work after that stack settles rather
than competing with it.

---

## 22. Final position

Utopia is not a Menhir replacement at the analyzed revision.

It is a serious adjacent system that establishes substantial prior art for
governed, temporal, source-grounded organizational knowledge. It is already a
better fit than Menhir for document-heavy enterprise knowledge bases,
ontology-backed domain modeling, broad source ingestion, and end-user
knowledge-curation workflows.

Menhir remains differentiated where software itself is part of the evidence
model:

```text
which code carries a decision
which repository change made prior knowledge stale
which callers and tests inherit a proposed edit
which agent action followed from which evidence
whether the structural index is complete enough to trust a negative answer
whether current state can be rebuilt and certified from durable assertions
```

The strategic response is not to match Utopia's breadth.

It is to make Menhir's narrower promise undeniable:

> Menhir is the governed evidence and software-understanding substrate that lets
> coding agents act across sessions without losing provenance, currentness, or
> change impact.
