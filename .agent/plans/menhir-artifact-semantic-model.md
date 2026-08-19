# menhir — Artifact semantic model

Status: IMPLEMENTED (2026-08-03). Design approved 2026-08-02; shipped in five slices plus
**Last verified:** 2026-08-18 — CONSISTENT with IMPLEMENTED. `ArtifactLocation` 8 hits. Gap: `CurrentPlanView` is 0 hits — that piece is not in `src/`.

shape validation and the MCP surface, `37e4039..ea34c1d`. Sections below were reconciled
with the shipped code — where design and implementation diverged, the implementation is
described and the reason for the divergence is stated inline.

The MCP surface is shipped: `get_artifact`, `list_artifacts`, `list_artifact_questions`,
`get_artifact_relationships`, `link_artifacts`, `supersede_artifact` and
`transition_artifact`, wired through the full repository/adapter/protocol/client/tool
chain (see §6 and `endpoints.md`). **`CurrentPlanView` remains unbuilt** — that is the one
piece of §6 still outstanding.

Name settled: `:WorkArtifact`.
Date: 2026-08-02
Extends: `../archive/plans/menhir-todo-declared-links.md` (the architectural baseline)
Primitives inherited: `model.owned_record`, `model.operational_vs_semantic`,
`model.embodiment_invariant`

Git owns bytes. Menhir owns meaning. Artifact markdown stays the canonical document;
menhir stores only semantic structure. This is not a document-management system.

---

## 0. Two blockers found before designing

### 0.1 The name `Artifact` is already taken

`domain/artifacts.py` and `infrastructure/artifact_repository.py` implement the **L4
institutional artifact loop**: Decision / Failure / Incident nodes backed by first-class
`:Evidence`, with a trust policy (`TRUSTED` / `CANDIDATE` / `HISTORICAL`) mapped onto
`NodeScope`, and four hard invariants — an LLM-sourced artifact is never trusted on
create, a human artifact is trusted only with an evidence anchor, promotion without
evidence is refused, supersession marks historical and never deletes.

That is a **different class of object** from a plan or a review:

| | L4 Artifact | This proposal |
|---|---|---|
| Represents | a claim about what happened | an engineering document |
| Backed by | `:Evidence` anchors | a file in Git |
| Lifecycle | trust promotion | authoring and approval |
| Truth question | "should I believe this?" | "is this the current plan?" |

Reusing one name for both would entangle a trust policy with a document lifecycle and
make `is_artifact` ambiguous. **Proposal: this class is `:WorkArtifact`**, and the L4
concept keeps `Artifact`.

`:Document` was the obvious alternative and is **also taken**: `ingest_document` already
writes `structure_role='document'` entities carrying a `document_type` (7 live nodes:
`research`, `memory_note`). Naming this class `:Document` would produce a second collision
of exactly the kind we are avoiding. `:EngineeringArtifact` is accurate but verbose, and
`:Record` collides with the Owned Record pattern name.

So `:WorkArtifact` survives on evidence rather than preference: it is the only candidate
that collides with nothing already in the graph.

This is cheap to settle now and expensive later: the L4 loop currently has **zero live
nodes**, so neither name is entrenched. **Blocking — the naming decision precedes schema.**

### 0.2 An artifact's own location is not a reference

The handoff proposes `Artifact -[HAS_LOCATION]-> ArtifactLocation -[RESOLVES_TO]-> FileEntity`,
"exactly the same subordinate pattern proven by TodoLocation." The shape carries over; the
*meaning* does not, and conflating them would be a modeling error.

- A `:TodoLocation` says **where the code this todo is about lives**. The todo is not that file.
- A work artifact **is** a file. The file is its embodiment — not a thing it points at.

Stated precisely, because the loose version is misleading: **the file is the embodiment;
the path is only a locator for it.** A rename, move, or archive changes the locator and
leaves the embodiment untouched. That is the same identity-first discipline applied
everywhere else here — identity first, location second.

A plan can also reference code — and *that* is the true TodoLocation analogue. So two
distinct relations are needed, not one:

```
WorkArtifact -[EMBODIED_IN]->  ArtifactSource    // where this document itself lives
WorkArtifact -[HAS_LOCATION]-> ArtifactLocation  // code this document talks about
```

Both subordinates are Owned Records. Collapsing them would make "the plan's file" and
"a file the plan discusses" indistinguishable, which breaks the first question anyone
asks: *which document is this?*

---

## 1. Artifact semantic model

A `:WorkArtifact` is an identity-bearing **semantic object**: it carries meaning,
participates in recall, and is referenced by other semantic objects.

Contrast with `:Todo`, which is *operational*. Both are first-class, but a todo is a unit
of work and an artifact is a unit of recorded thinking. The `model.operational_vs_semantic`
boundary still applies: an artifact may describe a todo; a todo never describes an artifact.

Initial types (`artifact_type`): `plan`, `review`, `investigation`, `implementation_report`.
**`handoff` was added after migration (2026-08-03).** It came off the deferred list because
the corpus answered the question: 14 handoff documents exist across two repos, and without a
type they were recorded as implementation reports and failed the wrapup contract for being
what they are. Its shape contract is derived, not invented — all 14 have an H1 title and no
section appears in even 4 of them, so a title is the only requirement and `Date` (9 of 14) is
a recommendation. A type earns its place by having instances.

Deliberately not generalized further. Deferred: `adr`, `migration`, `rfc`,
`orientation`.

### Identity

**Identity is a stable `artifact_uuid`, never the path.** The workspace-artifacts MCP
supports archive and restore, which move files between directories; filenames are also
edited. A path-keyed identity would break on the first archive, silently orphaning every
relationship pointing at it.

`ArtifactSource` records the current locator only — never a path history, which belongs to
Git (see §4). A move updates the locator in place; the artifact keeps its identity
across both. This is the same discipline as `code_ref` in Phase A — the raw declaration is
retained, and identity does not depend on resolution succeeding.

---

## 2. Graph schema

```
(:WorkArtifact)
  artifact_uuid        stable identity
  artifact_type        plan | review | investigation | implementation_report | handoff
  title                human label
  status               see §3
  namespace            non-null, same invariant as :Todo
  created_at, updated_at
  status_changed_at
  status_raw           the document's own Status: text, verbatim
  status_unresolved_reason  why it could not be mapped, if it could not
  shape_status         conforming | nonconforming | unchecked  (see §10)
  shape_violations     required-severity finding codes
  shape_checked_at

(:WorkArtifact)-[:EMBODIED_IN]->(:ArtifactSource)     // Owned Record
(:WorkArtifact)-[:HAS_LOCATION]->(:ArtifactLocation)  // Owned Record
(:WorkArtifact)-[:HAS_OPEN_QUESTION]->(:OpenQuestion) // Owned Record, addressable
(:WorkArtifact)-[:DECLARES]->(:ArtifactDeclaration)   // Owned Record, raw + resolution
(:ArtifactLocation)-[:RESOLVES_TO]->(:Entity)         // structural file entity
(:ArtifactSource)-[:RESOLVES_TO]->(:Entity)           // the document's own file entity
```

`handoff` was added on 2026-08-03, after migration; see §3 and the note in §1.

Both subordinate labels are distinct, never `:Entity` or `:Episodic`, so neither competes
in semantic recall — the `:TurnEvidence` / `:TodoLocation` containment.

`:WorkArtifact` **is** recallable; its subordinates are not. That split is the whole point
of the Owned Record pattern.

---

## 3. Lifecycle

Modeled as typed transitions, as `resolve_todo`/`reopen_todo` are — status changes and
their evidence edges move together in one statement, never separately.

```
plan:                   PROPOSED -> REVIEWED -> APPROVED -> IMPLEMENTING -> IMPLEMENTED
review:                 OPEN -> COMPLETE
investigation:          OPEN -> COMPLETE
handoff:                OPEN -> COMPLETE
implementation_report:  DRAFT -> READY_FOR_REVIEW -> REVIEWED -> COMPLETE
                        DRAFT -> COMPLETE            (written outside review)
                        READY_FOR_REVIEW -> COMPLETE (accepted without a review doc)

every type:             <any non-terminal> -> SUPERSEDED
                        <any non-terminal> -> DEFERRED
```

`SUPERSEDED` and `DEFERRED` are terminal and reachable from **any** non-terminal state of
**every** type — not attached per-type as the proposal drew them. They lead nowhere:
reopening a superseded plan means authoring a new one, which is what `SUPERSEDES` is for.

**`implementation_report` grew a review path (2026-08-03)** to match the workspace's
documented wrapup contract (`WRAPUP-TEMPLATE.md`: READY FOR REVIEW | PARTIAL | BLOCKED |
REVIEWED | REVIEWED WITH FINDINGS). A two-state lifecycle could not answer *"was this
reviewed?"*, which is the question the wrapup process exists to answer. `DRAFT -> COMPLETE`
was kept: a report written outside that process is finished when its author says so, and
routing it through a review it never had would record a review that did not happen.

**Reconcile with what already exists.** The corpus — estimated at ~98, actually 112 — carries an
informal vocabulary in `Status:` headers: `Proposal`, `OPEN`, `IMPLEMENTED`, `SUPERSEDED`,
`IN PROGRESS`, `DRAFT`, `DEFERRED`, `reviewer-approved`. Two of those have no home above:

- `DEFERRED` — a real state (decided-not-now), distinct from superseded. Recommend adding
  it to the plan lifecycle rather than forcing it into `SUPERSEDED`, which would lose the
  distinction between *replaced* and *postponed*.
- `reviewer-approved` — maps to `REVIEWED`; a spelling variant, not a new state.

Migration must record which header mapped to which state, and leave anything unmappable
as unresolved rather than guessing — the Phase A discipline.

---

## 4. Owned subordinate location model

`ArtifactLocation` reuses `TodoLocation`'s fields unchanged, which means the `parse_code_ref`
normalizer is reused rather than reimplemented:

```
project, path, kind, line_start, line_end, symbol,
ordinal, raw_segment, resolution_status, unresolved_reason, schema_version
```

Multiple locations allowed; **never a path array** — that was settled when multi-path
`code_ref` values forced positionally-aligned parallel arrays and lost per-record
resolution state.

`ArtifactSource` is an **embodiment**, not a path, and follows the medium-neutral contract
in `model.embodiment_invariant`. Git is one implementation, not the model:

```
medium              markdown | pdf | html | wiki | file
locator             medium-specific; mutable
version             current revision handle; ONE value, never a history
integrity           optional, medium-dependent
resolution_status, unresolved_reason
schema_version, first_seen_at, last_seen_at
```

| Leg | Git | Wiki | Filesystem |
|---|---|---|---|
| locator | repository, branch, path | page_id | uri |
| version | git_sha | revision_id | mtime |
| integrity | *(git_sha serves both)* | *(often none)* | content hash |

Storing these as a typed contract rather than Git columns means a wiki-backed or
filesystem-backed artifact needs no schema change — the semantic model survives beyond Git.

`medium` is the discriminator: one artifact may have several embodiments — canonical
markdown plus a generated PDF or exported HTML — each a separate `ArtifactSource`.
`src/oauth.py` is never an embodiment; it is an `ArtifactLocation`. That is the line the
two relations draw.

**`version` is provenance, not version history.** One value, meaning "the revision last
observed at this locator" — never a growing list. A list would be document version storage,
which is out of scope and which the versioning system already provides. If a trail is ever
wanted, it belongs in a Git query, not in these nodes.

Owned Record invariants apply to both: own label, no namespace copy, per-record
resolution, raw declaration retained, ordinal preserved, deleted with owner.

---

## 5. Relationship model

All author-declared. None inferred.

```
(:WorkArtifact)-[:REVIEWS]->(:WorkArtifact)        // review    -> plan | investigation | report
(:WorkArtifact)-[:IMPLEMENTS]->(:WorkArtifact)     // report    -> plan
(:WorkArtifact)-[:INFORMS]->(:WorkArtifact)        // investigation | handoff -> plan
(:WorkArtifact)-[:SUPERSEDES]->(:WorkArtifact)     // same type only
(:WorkArtifact)-[:ABOUT]->(:Entity)                // artifact  -> semantic object
(:WorkArtifact)-[:REFERENCES_TODO]->(:Todo)        // artifact  -> operational object
```

**The edge to a semantic object is `ABOUT`, not `REFERENCES`.** The proposal used
`REFERENCES`; §9.2 settled on `ABOUT` and that is what shipped. Do not implement or declare
`REFERENCES` — it exists nowhere in the code.

`REFERENCES_TODO` is named distinctly for the reason `ADDRESSES_TODO` was: a graph
accumulating several referent classes should not overload one edge type, and traversals are
usually type-specific.

`INFORMS` accepts a `handoff` source as of 2026-08-03, alongside `investigation`. No other
relation was opened up for handoffs: the corpus shows none reviewing or implementing
anything, and a relation without instances is speculation.

**`SUPERSEDES` is not a declarable relation.** It is absent from the relation whitelist and
is created *only* by `supersede_artifact`, which writes the edge and moves the superseded
artifact's status in one statement. A `supersedes:` frontmatter declaration is therefore
routed to that command rather than being materialized like an ordinary edge — see §9.4.
This is the one place where honoring a declaration changes another artifact's lifecycle,
and it is deliberate: the document declaring supersession is the authority for it.

Write rules, inherited from Phase B:
- Namespace compatibility validated on write.
- `MERGE`, so re-declaring is idempotent.
- Unsupported relation rejects the whole operation; the whitelist is also the injection
  guard, since Cypher cannot parameterize a relationship type.
- An ordinary relationship edge never mutates status. `supersede_artifact` is not an
  ordinary edge write — it is a lifecycle command that also writes an edge, and it does
  both in one statement.

### Explicitly rejected

Never infer: related code, reviewed plan, implemented plan, supersession, or references.
`CONCERNS` was removed precisely because inferred semantics produced durable noise —
1,139 edges, 29% of them artifacts of substring matching. Do not recreate it by parsing
markdown for plan titles.

---

## 6. Views

Artifacts feed Derived Semantic Objects. Views are computed, never written as markdown.

- **CurrentPlanView** — latest `APPROVED` plan for a subject, plus its latest review and
  implementation report.
- **ImplementationProgressView** — plan, implemented slices, remaining slices, blocking
  questions.
- **ReviewCoverageView** — findings by disposition: implemented, deferred, rejected.

`ReviewCoverageView` implies a finding is addressable individually, which points at a
third Owned Record — `Review -> ReviewFinding` — already listed as a candidate in
`model.owned_record`. **Out of scope here**; noted so the View is not designed as if
findings were free-text.

---

## 7. Migration — results (executed 2026-08-03)

Ordered so nothing was hidden before it was representable — the Phase A discipline, where
filtering before backfilling would have hidden 41 open todos.

**Outcome:** 112 artifacts (not the estimated ~98), 112 embodiments, 94 with a git sha,
0 duplicate locator paths, 0 fabricated relationships. Namespaces: 54 `menhir`,
58 `workspace`. Script: `scripts/migrate_work_artifacts.py`, idempotent by locator path,
dry-run by default.

| Step | Result |
|---|---|
| Settle the name | `:WorkArtifact`, done before any code |
| Create nodes | 112 created, identity minted fresh, path only in `ArtifactSource` |
| Map `Status:` headers | 46/112 mapped; 35 no header, 23 unrecognized, 8 invalid-for-type |
| Normalize code locations | **not run** — 945 backticked prose paths are volume, not signal; `--with-locations` measures without committing |
| Relationships | **not migrated**, by design — no declared source exists |
| Indexes | added for `:WorkArtifact`, `:ArtifactLocation`, `:ArtifactSource`, `:OpenQuestion`, `:ArtifactDeclaration` |

Header mapping started at 11/112 and reached 46 through two changes, both forced by the
corpus rather than guessed: the `implementation_report` review path above, and treating the
*leading token* of a header as the state with the remainder as commentary, so
`**SUPERSEDED** by X` and `APPROVED as the basis for Y` map correctly. Matching never scans
past the first word — a state named mid-sentence is being discussed, not declared.

The unmapped 66 are flagged, not guessed: `status_raw` and `status_unresolved_reason` are
stored on every node, so an artifact sitting in its initial state because nobody could read
its header stays distinguishable from one that genuinely is in that state. Leftover
vocabulary (`offline`, `research`, `root`, `findings`, `remediated`) is not lifecycle at
all and was deliberately left unmapped.

**Post-migration correction.** 11 documents in `.agent/for-review/` were HANDOFF, ISSUE and
SCALAR files, not wrapups. The directory-based type rule made them implementation reports
and they then failed a contract that was never theirs. They were refiled to
`.agent/handoffs/`, `relocate_source` followed the move (11 locators updated, identity and
relationships intact, 0 unreadable), and `handoff` became a type. This is the sequence the
embodiment invariant was written for, and it ran end to end.

### Original step list, retained for the reasoning

1. **Settle the name** (§0.1). Blocking.
2. Create `:WorkArtifact` nodes for the existing files. Identity minted fresh; path
   recorded in `ArtifactSource`, never as identity.
3. Map `Status:` headers to typed states. Emit a migration report: mapped, ambiguous,
   unmappable. Unmappable stays unresolved.
4. Normalize in-document code references into `ArtifactLocation` via the existing
   `parse_code_ref`. Expect a resolution rate below Phase A's 48/77 — these are prose
   documents, not `code_ref` fields.
5. Relationships are **not** migrated. There is no declared source for them, and inferring
   them from markdown is the rejected path. They accrue as authors declare them going
   forward. Accept a sparse relationship graph initially rather than a fabricated one.
6. Indexes before any production read, with `EXPLAIN`/`PROFILE` evidence — `:Todo` had
   none and every read was a label scan.

**The archive/restore interaction must be settled in step 2.** Archiving moves a file, so
the `ArtifactSource` locator changes. Under the corrected model this is an *update* to
`path`, not a new embodiment and not a failure: identity, embodiment, and relationships all
survive. Only a locator that resolves to nothing — file genuinely gone — is
`resolution_status='unresolved'`.

---

## 8. Worked examples

```
(:WorkArtifact {type: 'plan', title: 'todo declared links', status: 'IMPLEMENTED'})
  -[:EMBODIED_IN]-> (:ArtifactSource {project: 'menhir',
                                      path: '.agent/archive/plans/../archive/plans/menhir-todo-declared-links.md'})
  -[:HAS_LOCATION]-> (:ArtifactLocation {project: 'menhir',
                                         path: 'src/menhir/infrastructure/todo_repository.py'})
  -[:REFERENCES_TODO]-> (:Todo {uuid: 'b65e906d...'})

(:WorkArtifact {type: 'review', status: 'COMPLETE'})
  -[:REVIEWS]-> (:WorkArtifact {type: 'plan', ...})

(:WorkArtifact {type: 'implementation_report', status: 'COMPLETE'})
  -[:IMPLEMENTS]-> (:WorkArtifact {type: 'plan', ...})
```

Answering *"what is the current approved OAuth plan, which review changed it, and which
implementation completed Phase A?"* becomes a typed traversal over declared edges — no
markdown read, no inference.

---

## Out of scope

**Semantic inference from free prose**, LLM summarization, document embeddings, document
version storage, document editing, automatic artifact extraction. This is the semantic
model only.

**The boundary is inference, not parsing** — the original wording said "markdown parsing"
and that was too broad. Reading *structured, human-authored* declarations is in scope and
shipped: `Status:` headers, frontmatter relations, open-question lists, and the headings
and metadata fields shape validation checks. Guessing intent from running text is out, and
stays out. That is the line `CONCERNS` crossed, and the distinction that keeps this from
recreating it: a declaration is transcribed, an inference is invented.

## 9. Resolved design decisions

### 9.1 `DEFERRED` is a first-class plan state

`SUPERSEDED` means *a better answer exists*. `DEFERRED` means *we intentionally chose not
to answer yet*. Collapsing them makes "what important design decisions are still
deliberately deferred?" unanswerable.

```
plan: PROPOSED -> REVIEWED -> APPROVED -> IMPLEMENTING -> IMPLEMENTED
      any -> SUPERSEDED
      any -> DEFERRED
```

### 9.2 Subject is a role, not a new class

A subject must not be a string — otherwise `CurrentPlanView` is string matching forever.
But it must also **not** be a new `:Subject` label, and the graph says why:

> `OAuth` already exists as `:Entity {type: 'SEMANTIC'}`. So do `recall` and `namespace`.

A `:Subject` label would mint a *second* "OAuth" identity competing with the
memory-extracted one — precisely the two-competing-representations failure that
`model.operational_vs_semantic` exists to prevent.

So subject is a **role an existing semantic object plays**, not a class:

```
(:WorkArtifact)-[:ABOUT]->(:Entity)
```

Plans, reviews, investigations and reports then cluster around one durable concept
identity, shared with every memory about that concept. Where no entity exists yet
(`scalar`, `blast radius`, `enrichment` are absent today), the declaration creates one of
the same class — one identity per concept, never a parallel taxonomy.

### 9.3 One population, differentiated by namespace

Workspace-root and project-local artifacts are one population. Namespace already exists
and already carries this distinction; two populations would duplicate it and make
cross-population supersession ill-defined.

### 9.4 Frontmatter is declaration, not inference

```yaml
implements: [oauth-plan]
supersedes: [oauth-v1]
about: [OAuth]
```

> **Menhir never infers artifact relationships from prose. It may materialize
> relationships explicitly declared in structured metadata authored by a human.**

Reading structured frontmatter a human wrote is *not* the CONCERNS mistake: CONCERNS
guessed intent from prose, this transcribes a declaration. The alternative — MCP-call-only
— creates a split brain where the graph knows a relationship the document does not
describe.

**A consequence that must be designed for, not discovered later.** Frontmatter refers to
targets by human-readable name; identity is a uuid. So every declaration needs resolution,
and resolution can fail — a renamed target, a typo, a plan not yet ingested. Declarations
therefore follow the Phase A discipline: the raw declaration is retained, resolution is
per-record, and an unresolved reference is **kept as unresolved rather than dropped**.
Silently discarding it would let a rename quietly delete a relationship, which is the exact
failure `model.embodiment_invariant` warns about.

### 9.5 Open questions become first-class

Every artifact in this corpus ends with an "Open Questions" list. That is structured
engineering state living as prose, and it is re-parsed by every reader.

```
(:WorkArtifact)-[:HAS_OPEN_QUESTION]->(:OpenQuestion)   // Owned Record
```

Fields: `ordinal`, `text`, `status` (`open` | `answered` | `deferred`), `raw_segment`,
`schema_version`.

This makes answerable, without reading markdown: *what design questions remain? which
plans are blocked? which questions were deferred? which review answered question 3?*

**One honest tension.** "Which review answered question 3?" means a semantic object points
*at* an `OpenQuestion`, so it needs a stable, addressable uuid. That is compatible with
Owned Record but worth stating precisely, because it looks like a violation:

> Being **referenceable** is not the same as having **semantic identity**. An OpenQuestion
> gets a uuid so it can be addressed, but it is still never recallable, never carries
> meaning independently, and still dies with its artifact.

This mirrors `:Todo` exactly: semantic objects point inward at it, and it never becomes a
semantic object itself.

## 10. Shape validation (added 2026-08-03)

Ingest and update both check a document against its type's contract and store the verdict
on the artifact. `src/menhir/domain/artifact_shape.py` — pure functions over document text,
no filesystem, no git, no graph.

**A failing document is recorded, never rejected.** Refusing the write would leave the graph
unaware of a document that exists on disk, which is worse than knowing about it and knowing
it is malformed. Same reasoning that retains an unresolved declaration rather than dropping
it.

Three states, because `UNCHECKED` must never read as `CONFORMING`: an artifact nobody
validated is a coverage gap, not a clean bill of health. Passing no document leaves
`shape_status` absent for the same reason.

The `implementation_report` contract is **not defined here**. It mirrors
`WRAPUP-TEMPLATE.md` as enforced by `cth.agentsmith/scripts/wrapup_validator`, which remains
the authority; `test_wrapup_contract_matches_the_authority` fails on drift rather than
letting the copies diverge. Menhir checks the same contract at a different moment — the
validator gates review, this gates what the graph believes — and two disagreeing definitions
of "valid wrapup" would be worse than one.

Contracts for other types are deliberately thin, and the `handoff` one is derived rather
than invented: across 14 real handoffs every one has an H1 title and no section appears in
even 4 of them, so a title is the only requirement and `Date` (9 of 14) is a recommendation.
Requiring sections nobody agreed to would manufacture violations rather than detect them.

Live result across the 112: 85 conforming, 27 nonconforming, 0 unreadable. Of the 27, 24 are
plans and reviews with no `Status:` line; 3 are genuinely malformed wrapups.

---

## Open questions

All four earlier questions are resolved in section 9. Three implementation questions were
open at design time; all three were settled during implementation:

1. **Does `ABOUT` allow more than one subject per artifact?** *Still open.* Nothing enforces
   a limit today. `CurrentPlanView` is unbuilt, so the grouping pressure that would decide
   this has not been felt yet.
2. **When frontmatter and a later MCP call disagree about a relationship, which wins?**
   *Settled: declarations only ever add.* Dropping a target from frontmatter does not
   retract an existing edge; removal is an explicit act. A document losing a line must not
   quietly rewrite the graph, and that is the only direction safe to automate.
3. **Should `answered` require an inbound edge naming what answered it?** *Settled: yes*,
   mirroring `RESOLVES_TODO`. An answered question with no answering artifact is a claim
   without evidence. Deferring requires no evidence, because deferring is a decision rather
   than an answer.

### Known divergences from this document's original design

- **`about:` does not mint a missing `:Entity`**, though §9.2 permits it. An entity created
  outside the embedding-stamping ingest path carries no name embedding and is silently
  unrecallable — worse than an honest unresolved declaration, because it looks like it
  worked. Entity creation belongs to ingest.
- **Code locations were not backfilled** during migration (see §7).
