# Promotion synthesis — from mutation spike to Menhir Core

**Status:** design synthesis from the isolated mutation-kernel research branch.  
**Production impact:** none. This document proposes boundaries only; it changes no `src/menhir` code.  
**Research branch:** `research/mutation-kernel-spike`  
**Research base:** `13143d8a7ef5bfb9198db48895d55c7147f43c42`

## Executive conclusion

The mutation experiments did **not** discover that Menhir needs a large new plugin kernel.

They discovered almost the opposite:

> Menhir's reusable semantic core is already small, and substantial pieces of it already exist in
> production. The promotion task is mostly to formalize boundaries, replace a few closed registries
> with injected/registered definitions, and generalize lifecycle guarantees that currently exist in
> domain-specific forms.

The semantic loop that survived personality, production scalar state, investigative ownership,
purpose-sensitive trust, graph relationships, and model-facing context remains:

```text
Evidence
  -> immutable Assertion
  -> current set / explicit supersession
  -> extension-owned Fold
  -> View | Abstention | Retirement
```

Everything complicated added after that loop was operational correctness around it:

```text
admission ceiling
semantic-definition version
atomic invalidation
work generation
stale-writer fence
derivation receipt
freshness certificate
retrieval/context freshness propagation
bounded model-facing serialization
```

Those are generic governance/lifecycle services. They are not new domain semantics.

The resulting recommendation is therefore **not** "rewrite Menhir around the spike". It is:

1. preserve existing production repositories and scalar behavior;
2. introduce the smallest stable contracts around the seams production already has;
3. adapt the current scalar path as the regression oracle;
4. make built-in coding behavior the default registered extension set;
5. move lifecycle/version/freshness guarantees underneath those registered definitions; and
6. postpone physical schema unification and a public plugin framework until the contracts survive
   production use.

## The key observation: production already contains a proto-extension system

The strongest production analogue is the existing View architecture.

`src/menhir/infrastructure/view_models.py` already describes one shared View node shape with per-kind
semantics supplied by `ViewKind`. A kind owns its discriminator, keying, signature, retrieval surface,
value properties, read projection, and parse behavior. The shared repository owns versioning,
recall stamping, supersession, provenance and persistence.

`src/menhir/infrastructure/view_write_repository.py` then implements a kind-agnostic `record()` and a
single `_write_version()` path. The primary closed seam is the static `KINDS` dictionary containing
built-ins such as counter, timeline, admission-audit and scalar views.

That is already very close to the boundary the mutation spike independently derived:

```text
shared projection lifecycle
          ↑
registered domain definition
```

Likewise, the production typed-scalar path already contains many of the assertion/lifecycle
properties the spike later rediscovered generically:

- source-grounded assertion identity;
- immutable interpretation versions;
- an explicit current head;
- strict supersession;
- same-version disagreement retained for audit rather than flipping current state;
- atomic grounding/binding;
- durable `projection_pending` crash-recovery state; and
- future activation scheduling.

So the problem is not that Menhir lacks the necessary ideas. The problem is that several of them are
currently named, persisted, registered or routed as if scalar/coding semantics were the universe.

## Smallest promotion-worthy core

The candidate core should be deliberately boring. The following is enough.

### 1. Evidence and assertion envelope

Core owns the durable facts about **where an interpretation came from** and **which interpretation it
is**.

A promotion-worthy assertion envelope needs only:

```text
assertion_id
source_key
assertion_type
subject_id
scope
key
value                     # extension-owned value
valid_at
learned_at
authority                 # sealed effective authority, not producer self-assertion
confidence
evidence[]
supersedes[]
interpreter/definition version
opaque dimensions[]
opaque metadata[]
```

Core responsibilities:

- source-grounded identity;
- immutable assertion interpretation;
- time envelopes;
- exact provenance references;
- explicit semantic supersession;
- canonical opaque dimensions;
- sealed authority result;
- replay/collision checks.

Core should **not** know whether a value represents a token balance, personality trait, ownership
claim, code dependency or anything else.

### 2. Projection slot and outcome contract

The generic projection identity proven by the spike is:

```text
(view_type, subject_id, scope, key, dimensions)
```

The current state of that slot is one of:

```text
View        current answer exists
Abstention  current evidence cannot safely support an answer
Retirement  a previous answer is known to have ended
```

That distinction matters operationally. "No View row" is insufficient because it cannot distinguish
never-materialized, contested/unsafe, expired/retired and accidentally stale states.

A minimal projection definition needs:

```python
class ProjectionDefinition(Protocol):
    definition_id: str
    version: int
    input_types: tuple[str, ...]
    view_type: str

    def targets_for(self, assertion: AssertionEnvelope) -> Sequence[ProjectionSlot]: ...
    def fold(self, assertions: Sequence[AssertionEnvelope]) -> ProjectionOutcome: ...
```

The exact Python shape is negotiable. The important boundary is not:

```text
core has a generic fold algebra every extension must use
```

It is:

```text
core can invoke a versioned deterministic fold and validate/persist its generic outcome
```

### 3. Semantic-definition lifecycle

This is the largest generic mechanism discovered by the spike, but its public contract can remain
small.

A semantic definition that affects durable derived state needs:

- stable definition ID;
- monotonically increasing semantic version;
- shared-current version publication;
- dependency registration / target invalidation;
- optimistic work generation;
- stale-version write fencing;
- deterministic derivation receipt; and
- freshness certification of the exact persisted projection hash.

The spike implemented separate coordinators while pressure-testing projection definitions, graph
definitions and trust resolvers. **Do not promote those separate coordinators.** They are evidence
that one underlying lifecycle primitive is needed.

Conceptually:

```text
publish semantic definition vN
        ↓
atomically invalidate affected durable targets
        ↓
worker captures (definition vN, work generation G)
        ↓
derive candidate outcome
        ↓
commit only if vN and G are still current
        ↓
record derivation + exact projection hash
        ↓
certify fresh
```

This is where most of the spike's complexity belongs.

### 4. Admission and authority ceiling

The spike separated two questions that production frequently conflates:

```text
May this evidence enter this domain?
```

and

```text
How authoritative may it become?
```

The promotion-worthy generic rule is:

```text
effective authority = weakest(
    producer request,
    trusted ingress/source grant,
    extension admission-policy ceiling
)
```

An extension may reject evidence or lower authority. It must not mint authority above the trusted
source mechanism.

This allows domain-specific admission without teaching core which sources count as deeds, user
preferences, test output, court filings, code files, anonymous tips, etc.

### 5. Purpose-sensitive trust as a sidecar

Authority is a hard admission ceiling. It is not the complete trust model.

The same admitted evidence may have different evidentiary relevance for different questions. The
investigation experiments demonstrated this with a deed that is strong evidence for recorded title
and weaker evidence for beneficial ownership.

Core only needs to preserve:

- assertion/profile binding;
- immutable trust-profile version;
- opaque extension facets;
- resolver ID/version;
- purpose-specific effective authority bounded by admission authority; and
- exact included/excluded/counterevidence trace for a derivation.

The extension owns the meaning of those facets and purposes.

This should remain an optional sidecar. Domains that need only the legacy total authority order do
not need a multidimensional trust model.

### 6. Freshness-aware serving envelope

Once derived state is versioned, freshness is part of correctness rather than presentation.

A projection is not "fresh" merely because:

- a current row exists; or
- the scheduler has no pending work.

The spike's stronger contract was:

```text
fresh iff
    semantic definition is current
    AND work generation is completely applied
    AND a derivation certificate exists
    AND the certificate names the exact current projection hash
```

That freshness result must survive retrieval, ranking, rendering and final context assembly without
becoming extension-editable metadata.

The generic candidate/context envelope therefore owns structural fields such as:

```text
projection identity/hash
semantic definition identity/version
work generation
fresh | stale
staleness reason
derivation ID
contributor IDs
counterevidence IDs
```

The renderer owns only presentation data.

### 7. Context budgets and serialization

Final model-facing context is a core resource/governance boundary because an extension renderer must
not be able to consume unbounded context or overwrite structural provenance.

Promotion-worthy behavior:

- deterministic ranking input;
- stale-serving policy checked again at the final boundary;
- per-item rendered-content budget;
- aggregate rendered-content budget;
- hard complete-packet budget;
- complete provenance retained for included candidates;
- whole-candidate drop when structural provenance cannot fit; and
- structured separation of generic governance metadata from extension-rendered data.

The spike's character budgets are **not** the production API. Production should use the target model
or provider's tokenizer, or a deliberately conservative token estimator.

## What remains extension-owned

A domain extension should own everything that answers "what does this mean?":

- assertion vocabulary;
- entity kinds and identity resolution beyond generic source identity;
- relationship vocabulary and materialization rules;
- value types;
- value codec where storage needs one;
- source/admission policy;
- fold semantics and arithmetic;
- assertion -> projection-target mapping;
- projection retrieval surface;
- trust facets;
- purpose-sensitive trust resolver;
- renderer;
- domain-specific ranking features;
- optional semantic judges; and
- activation/business rules specific to the domain.

Examples that should **not** move into Menhir Core merely because the built-in coding extension needs
them:

```text
TEST_PASSED
SOURCE_IS_GIT
FILE_CHANGED
blast-radius semantics
symbol/file/test identity
Git commit anchoring rules
scalar absolute-vs-delta arithmetic
recorded-title vs beneficial-owner meaning
personality trait math
```

Core may provide common helper libraries for these. It should not require their vocabulary.

## Production mapping

The lowest-risk promotion path is adapter-first because most of the required behavior already exists.

| Candidate core concept | Existing production analogue | Recommended disposition |
|---|---|---|
| source-grounded immutable assertion | `domain/typed_assertion.py`, `infrastructure/typed_assertion_*` | Adapt scalar assertions first; do not replace their schema |
| explicit current/supersession | `TypedAssertionHead -> CURRENT -> TypedAssertion` | Preserve; expose through generic assertion-store protocol |
| projection slot/current outcome | production View key/current-version machinery | Adapt existing View writer |
| per-kind projection semantics | `infrastructure/view_models.py::ViewKind` | Keep concept; make registration injectable |
| shared projection persistence | `ViewWriteRepositoryMixin.record/_write_version` | Reuse; do not rebuild from spike Neo4j store |
| deterministic scalar fold | `domain/scalar_state_fold.py` | Register unchanged through scalar adapter |
| fold helper vocabulary | `domain/fold_algebra.py` | Keep as optional shared library, not mandatory core algebra |
| namespaces | `domain/namespace.py` | Reuse/core utility |
| temporal parsing/state | `domain/temporal.py` | Reuse/core utility with audit fixes as applicable |
| crash repair | scalar `projection_pending`, assertion reconciliation/repair repos | Generalize lifecycle sidecar; keep scalar behavior intact |
| semantic compatibility fence | scalar identity-version activation checks | Reuse fail-closed deployment pattern for generic definitions |
| recall candidate envelope | `domain/recall.py::CandidateData/ScoredMemory` | Extend/adapt with certified freshness/provenance; avoid rewrite |
| authority side layers | scalar/event authority verdicts in `RecallResult` | Preserve initially; later implement through registered authority providers |
| evidence-kind mapping | `domain/truth/kinds.py` | Replace fixed global vocabulary with default registered coding signals over time |
| Neo4j transport | `infrastructure/neo4j.py` | Keep; no semantic responsibility |
| graph mutation | `memory_graph_adapter.py`, Graphiti integration | Later adapter boundary; not first promotion phase |
| View embeddings | `view_embedder.py` + shared View retrieval surface | Reuse |
| model-facing context | existing recall/context composition paths | Add structural freshness/budget layer rather than rewrite retrieval |

### Existing `ViewKind` is especially important

The spike should **not** replace production `ViewKind` with its own `ProjectionDefinition` wholesale.
They solve adjacent halves of the same abstraction.

Production `ViewKind` already describes how a materialized View is:

- keyed;
- signed/idempotency-checked;
- surfaced for retrieval;
- encoded into node properties; and
- read back.

The spike's `ProjectionDefinition` describes how immutable assertions:

- route to output slots; and
- fold into current outcomes.

A production extension definition could compose them:

```text
ProjectionDefinition
  input types
  target mapper
  fold
        ↓
ViewKind / ProjectionPresentation
  storage/value encoding
  retrieval surface
  read projection
  rendering
```

Or those could remain separately registered contracts. There is no need to force them into one giant
interface.

### Existing typed-scalar path is the migration oracle

Do not begin by converting `:TypedAssertion` and scalar View nodes into a generic physical schema.

The scalar path already has substantial correctness machinery and is the best regression oracle we
have. The safe first target is:

```text
existing TypedAssertionRepository
        ↓ adapter
AssertionEnvelope protocol
        ↓
existing production scalar fold
        ↓ adapter
ProjectionOutcome contract
        ↓
existing ViewRepository / ScalarState View writer
```

The Experiment 2/Neo4j parity fixture should become the acceptance test for every promotion step.

If a proposed core contract requires changing scalar arithmetic, contributor identity, grounding,
current-state selection, replay behavior or write semantics before the adapter can fit, the contract
is probably too opinionated.

## Existing closed seams to open gradually

### 1. `ViewWriteRepositoryMixin.KINDS`

Today the writer is generic but registration is static.

First refactor should be conceptually as small as:

```python
ViewRepository(
    neo4j,
    kinds=default_builtin_view_kinds(),
)
```

with the default producing the exact current built-in set.

No behavior or schema change is required to prove that seam.

### 2. Evidence/source-kind SSOT

`domain/truth/kinds.py` correctly centralized overlapping source maps, but the SSOT remains a fixed
coding-oriented vocabulary:

```text
ANCHOR_KINDS
SELF_SOURCE_KINDS
SOURCE_LABEL_TO_KIND
KIND_TO_SIGNAL
DIVERSITY_FAMILY
```

The next abstraction is not "remove the SSOT". It is "make the SSOT a registry whose default
registration is today's exact coding set."

That lets an investigation extension register `deed` or `court_record` without editing core, while
preserving one authoritative lookup surface.

### 3. Scalar/event-specific authority lanes

`RecallResult` already separates structured authority verdicts from ranked memories. That is a good
shape.

The eventual abstraction is likely a registered authority provider producing typed structured
verdicts, not a return to one flat rank score and not an immediate replacement of the scalar/event
fields.

Backward-compatible evolution matters more than interface purity here.

### 4. Graph entity/relationship semantics

This is a later seam. Production graph/Graphiti integration is broad and operationally sensitive.
The spike proved that entity identity and edge semantics can be extension-owned, but it does **not**
justify replacing `MemoryGraphAdapter` during the first promotion phase.

Introduce graph contracts only after assertion/projection registration works in production.

## Spike implementation debt that should be discarded

Successful experiment code is not automatically production architecture.

Do **not** promote these literally:

### Spike physical schema

Discard as a production proposal:

```text
:MutationAssertion
:MutationProjection
:MutationProjectionWork
:MutationTrustProfile
:MutationFoldTrace
:MutationFreshnessCertificate
and the other Mutation* labels
```

They were useful because they isolated the experiment from production. They have not earned a schema
migration.

### Multiple deployment coordinators

The spike produced projection-, graph- and trust-specific deployment/fencing coordinators while
pressure-testing each surface.

Treat that duplication as a discovery result:

```text
all durable semantic definitions need one version/invalidation/fence primitive
```

not as three APIs to preserve.

### Global registry lock

The transaction-held registry guard proved correctness under publication races. It is not a
performance design.

Production should likely scope epochs/locks by semantic definition or another bounded dependency
unit rather than serializing unrelated extensions behind one global node.

### Direct private-driver access

Some spike coordinators use repository/private driver internals to hold Neo4j transactions across
fenced operations. Promote an explicit transaction capability if needed; never codify private access
as a core interface.

### Store inheritance/private helper reuse

The spike subclasses its generic Neo4j store and imports private hashing/serialization helpers across
modules. That was expedient experimental reuse. Production should expose deliberate store protocols
and stable identity helpers.

### JSON as universal domain storage

The spike JSON codec proved generic persistence can remain ignorant of a domain, not that all Menhir
values should become JSON blobs.

Keep typed/indexed production properties where their query behavior matters.

### Character budgets

Replace with token-aware budgeting before promotion to a real model-serving path.

### Personality/investigation folds

Keep them as reference extensions/fixtures. They are hostile tests of the boundary, not Menhir
features that must ship with core.

### Worker leases

Do not add them for correctness. The optimistic generation model rejected stale writes without them.
Add a lease later only if duplicate work becomes an observed performance problem.

### A large plugin framework

The spike has not justified plugin discovery, package marketplaces, runtime code loading or a complex
capability system.

Start with explicit in-process registration of trusted built-ins and adapters. Distribution can be
solved only when there is a real second deployment unit that needs it.

## Proposed production-facing contracts

The goal is to keep these contracts independent enough that existing production stores can implement
them without migration.

### Assertion read/write boundary

```python
class AssertionStore(Protocol):
    def record(self, assertion: AssertionEnvelope, *, targets: Sequence[ProjectionSlot]) -> RecordResult: ...
    def load_current(self, selection: AssertionSelection) -> Sequence[AssertionEnvelope]: ...
```

The production scalar adapter may internally use TypedAssertion heads and scalar-specific Cypher.
Nothing in the protocol requires generic physical nodes.

### Projection definition registry

```python
class ProjectionRegistry(Protocol):
    def definitions_for(self, assertion_type: str) -> Sequence[ProjectionDefinition]: ...
    def current_version(self, definition_id: str) -> int: ...
```

The implementation must eventually have a shared durable definition version, not merely an
in-process dictionary, once multiple independently upgraded workers exist.

### Projection lifecycle store

```python
class ProjectionLifecycleStore(Protocol):
    def pending(self, *, limit: int) -> Sequence[ProjectionWork]: ...
    def commit_outcome(self, work: ProjectionWork, outcome: ProjectionOutcome, derivation: Derivation) -> CommitResult: ...
    def read_certified(self, slot: ProjectionSlot) -> CertifiedProjection: ...
```

`commit_outcome` is where generation/version fencing and exact derivation certification belong.

### Admission

```python
class AdmissionPolicy(Protocol):
    policy_id: str
    version: int
    def evaluate(self, assertion, grants) -> ExtensionAdmission: ...
```

The generic engine owns grant binding and ceiling enforcement.

### Trust resolver

```python
class TrustResolver(Protocol):
    resolver_id: str
    version: int
    def resolve(self, profile: TrustProfile, *, purpose: str) -> TrustResolution: ...
```

Again, core clamps this under admission authority and versions any durable derived output that
changes when resolver semantics change.

### Presentation/context

```python
class ProjectionRenderer(Protocol):
    renderer_id: str
    version: int
    def render(self, projection: CertifiedProjection) -> RenderedProjection: ...
```

The renderer cannot populate reserved generic freshness/provenance fields.

## Staged integration plan

The ordering below intentionally delays risky schema and recall changes.

### Phase A — contracts and adapters only

Add generic domain-neutral contracts in a small package, with **no production routing changes**.

Candidate contents:

```text
evidence/assertion envelope
projection slot/outcomes
projection-definition protocol
admission contracts
semantic-definition version identity
certified projection read model
```

Build adapters around:

- production `TypedAssertion`;
- production scalar fold result; and
- production scalar View.

Acceptance condition:

> The existing scalar real-Neo4j parity fixture produces byte-/field-equivalent production state and
> the generic adapter sees the same value, contributors, authority and lifecycle result.

### Phase B — inject the existing View registry

Make built-in View kinds an injected/default registry instead of a class-level closed dictionary.

Default behavior must be exactly today's registration.

No new View kind is required yet. This phase proves the production write core really is open without
changing its semantics.

### Phase C — register scalar projection semantics without changing scalar persistence

Create a scalar `ProjectionDefinition` adapter:

```text
TypedAssertion input types
  -> existing scalar slot identity
  -> existing production fold_assertions
  -> generic outcome adapter
  -> existing scalar View writer
```

Keep existing scalar ingestion, repositories, repair and schema.

Initially run this as parity/shadow infrastructure, not as the authoritative replacement path.

### Phase D — introduce generic lifecycle sidecars

Add the minimum durable generic lifecycle state alongside existing schemas:

- semantic definition/version;
- target dependency/work generation;
- applied generation;
- derivation receipt/hash; and
- freshness certificate.

Do not migrate assertion or View payloads yet.

Bridge current scalar `projection_pending` repair into this model incrementally. The existing marker
is useful evidence and a compatibility input, not something that must disappear immediately.

### Phase E — certified read path behind a feature flag

Teach projection retrieval to optionally return certified freshness/provenance.

Then thread that metadata through recall without changing ranking behavior first.

Important compatibility rule:

```text
freshness metadata may suppress or label a candidate;
it must not silently become another relevance score.
```

Preserve existing scalar/event authority lanes while the generic provider abstraction is proven.

### Phase F — generic context envelope and budgets

Apply the Experiment 18/19 serving boundary to one opt-in context path:

```text
certified candidate
  -> renderer
  -> generic freshness-preserving envelope
  -> token budget
  -> model-facing structured packet
```

This phase directly addresses the broader architectural problem revealed by the audit: stored/recalled
content must not automatically become indistinguishable from trusted instructions merely because it
is memory.

It does not claim prompt-injection immunity.

### Phase G — move coding semantics behind default registration

Once the above contracts are stable, start converting central coding vocabularies into default
registrations rather than hard-coded universe definitions.

Good early candidates:

- View kinds;
- evidence/source kinds and their belief signals;
- retrieval diversity families;
- belief evidence providers; and
- projection definitions that already have clear deterministic folds.

Keep the default Menhir distribution behavior unchanged. "Coding becomes an extension" initially
means architectural ownership, not deleting or separately packaging coding features.

### Phase H — second real domain before public plugin packaging

Only after the built-in coding domain works through registration should Menhir run a second real
extension end-to-end.

The investigation platform is a better acceptance test than personality because it exercises:

- heterogeneous documentary provenance;
- competing claims;
- purpose-sensitive trust;
- entity relationships;
- conclusions/abstentions;
- source citations; and
- operator review.

If that can run without edits to Menhir Core, the extension boundary is real.

At that point packaging/distribution questions become concrete instead of speculative.

## What should *not* happen during promotion

Do not:

1. migrate all current graph nodes to `Mutation*` shapes;
2. replace Graphiti while proving the assertion/projection seam;
3. rewrite production scalar fold logic in generic arithmetic;
4. remove existing scalar/event authority APIs before a compatible replacement exists;
5. expose arbitrary runtime plugin loading before trusted registration is stable;
6. make every production module depend on one giant `Extension` object;
7. convert domain values to unindexed JSON merely for uniformity;
8. conflate trust with relevance scoring;
9. conflate stale with low-ranked;
10. make renderer text authoritative metadata; or
11. merge the research branch into `main` while the concurrent audit/remediation window is still
    changing production invariants.

## Evidence ledger: what the experiments actually earned

| Invariant | Experiment pressure | Production analogue | Disposition |
|---|---|---|---|
| domain-neutral assertion envelope | personality + scalar + investigation | `TypedAssertion` already source-grounded/versioned | promote contract, adapt schema |
| extension-owned folds | personality, scalar, investigation | scalar fold + ViewKind/fold algebra | promote protocol only |
| opaque slot dimensions | scalar | scalar value_kind/unit | promote |
| View/Abstention/Retirement distinct | scalar expiry + contested personality | production scalar has state/expiry semantics | promote outcome contract |
| immutable history / mutable current projection | Neo4j personality + scalar | typed assertions vs View current versions | promote |
| deterministic crash repair | lifecycle interruption tests | scalar projection_pending + repair repos | generalize lifecycle |
| metadata-driven dirty discovery | generation tests | partial scalar repair markers | promote sidecar |
| optimistic stale-worker rejection | concurrent-generation tests | several production fail-closed version guards | promote |
| extension registration/backfill | projection registry tests | ViewKind static registry | open registry gradually |
| atomic source authority ceiling | hostile admission tests | evidence tiers/source provenance exist but are domain-shaped | promote boundary |
| purpose-sensitive trust | deed/user/investigator evidence tests | no full generic analogue | optional sidecar |
| fold trace with included/excluded/counterevidence | investigative trust fold | scalar contributor provenance | promote derivation model |
| resolver semantic upgrades invalidate derived state | resolver deployment tests | scalar identity activation demonstrates fail-closed deployment pattern | generalize semantic definition lifecycle |
| freshness needs exact derivation certificate | freshness tests | stale-anchor metadata is narrower | promote certified read |
| renderer cannot rewrite freshness | context composition tests | no generic structural freshness envelope | promote serving envelope |
| context is bounded without clipping provenance | model-context tests | production recall/context limits are path-specific | promote generic budget layer |
| no exclusive worker lease required for correctness | interleaving tests | N/A | defer leases |
| generic physical Neo4j schema required | **not proved** | production schemas already encode useful indexes/compat | discard assumption |
| public plugin framework required | **not proved** | explicit built-ins today | defer |

## Why the core felt easy to abstract

Because much of Menhir was already built around the right conceptual split.

The production code already contains repeated versions of:

```text
typed durable evidence
        ↓
pure deterministic reduction
        ↓
one shared materialized View shape
        ↓
recall
```

The spike's hostile domains mostly forced us to remove accidental assumptions:

```text
"all slots have the same axes"              -> opaque dimensions
"absence is one state"                      -> abstention vs retirement
"one View has one input type"               -> multi-input definitions
"authority is one universal trust number"   -> admission ceiling + purpose trust
"definition code never changes under data"  -> version/invalidation/fencing
"current row means current semantics"        -> derivation certificate/freshness
"renderer is just presentation"             -> protected structural metadata
"context size is somebody else's problem"   -> generic budget boundary
```

None of those required core to learn personality or investigations.

That is the strongest result of the spike.

## Proposed architectural shape

Not separate applications pretending to share a database, and not a giant universal ontology.

```text
                         Menhir Core

    evidence identity / provenance / time / namespace
                         |
             immutable assertion contract
                         |
        admission + semantic-definition lifecycle
                         |
        projection work / fencing / certification
                         |
       freshness-aware retrieval/context envelope
                         |
      ---------------------------------------------
      |                    |                       |
  menhir-code       menhir-investigate      menhir-companion
  registrations      registrations           registrations
      |                    |                       |
  git/files/tests     people/orgs/docs        preferences/
  scalar state        claims/evidence         interactions
  blast radius        ownership/etc.          profile Views
```

The package boundaries can come later. The important point is ownership: code-specific meaning is a
registered consumer of the core lifecycle rather than a prerequisite for that lifecycle to exist.

## Decision

The mutation spike has provided enough evidence to stop expanding the experimental kernel.

The next implementation work, when the audit/remediation window permits production changes, should
be **promotion by adaptation**, beginning with:

1. stable core envelopes/protocols;
2. scalar adapters and parity tests;
3. injected `ViewKind` registration; and
4. a unified semantic-definition lifecycle sidecar.

Until then, keep this branch as an executable architectural proof and regression corpus.

The design target is no longer "can Menhir be abstracted?"

It is:

> **How little production code must change to expose the abstraction Menhir already mostly has?**
