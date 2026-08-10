# Change-Aware Documentation: OSS and Prior-Art Research

> Status: research note
> Related design: `docs/change-aware-documentation.md`
> Question: which open-source projects can Menhir reuse, integrate, or learn from?
> Research date: 2026-07-10

## Executive summary

No single open-source project currently provides the complete Menhir design:

```text
document header in the repository graph
-> bounded document graphlet
-> sections and explicit claims
-> claims linked to shared concepts and code symbols
-> symbol-aware change propagation
-> stale/review state
-> durable review receipts
-> reverse lookup across docs, concepts, and code
```

The closest direct prior art is **DOCER**, which detects outdated references to code elements in repository documentation. It is useful for heuristics, evaluation cases, and comparison, but it is not a suitable architectural foundation for Menhir.

The strongest reusable building blocks are:

```text
markdown-it-py
    deterministic Markdown structure and source positions

SCIP
    language-agnostic symbol identity and occurrence indexes

Menhir's existing structure graph + Hook Center
    code anchors, Git events, dirty/stale propagation, temporal state

DOCER
    broken/deleted code-reference heuristics and evaluation fixtures

GumTree or Tree-sitter
    optional syntax-aware change classification and rename/move matching

Doc Detective
    optional executable verification for procedures, commands, APIs, and UI docs
```

Recommended direction:

```text
Do not fork a full documentation platform.
Do not embed Kythe or GumTree in v0.
Do not make Doc Detective a core dependency.

Build a Menhir-native document graphlet layer using markdown-it-py,
use existing Menhir symbols initially,
adopt SCIP-compatible symbol IDs,
and add optional external adapters later.
```

## The Menhir boundary

The target architecture is broader than stale-link detection.

A document should be represented as a stable header node on the repository graph plus a bounded graphlet owned by that document:

```text
Repository
└─ Document
   ├─ PRIMARY_TOPIC -> Concept
   └─ OWNS_GRAPHLET
      ├─ Section
      ├─ DocumentationClaim
      ├─ ConceptMention
      ├─ CodeAnchor
      └─ ReviewReceipt
```

Shared concepts and code symbols live outside the document-owned graphlet:

```text
DocumentationClaim -[:ABOUT]-> Concept
DocumentationClaim -[:ANCHORED_TO]-> CodeSymbol
CodeSymbol -[:DEFINED_IN]-> File
ConceptMention -[:RESOLVES_TO]-> Concept
```

This boundary matters when assessing reuse. Existing tools generally solve one of four narrower problems:

1. Parse and render documents.
2. Index code symbols and references.
3. Detect code/document inconsistencies.
4. Test executable documentation.

Menhir's contribution is the lifecycle and graph composition across all four.

---

## 1. DOCER

Repository: `wesleytanws/DOCER`

License: MIT

Associated work: *Detecting Outdated Code Element References in Software Repository Documentation*

### What it does

DOCER scans repository documentation for references to code elements and attempts to identify references that have become outdated. The public repository contains the implementation and generated reports used by the associated research.

Its documented workflow is batch-oriented:

```text
list repositories
-> clone repositories
-> run normal or extended analysis scripts
-> generate reports
```

### Why it is close

DOCER directly addresses one important subset of change-aware documentation:

```text
document mentions code element
-> code element disappears or no longer resolves
-> documentation reference is probably outdated
```

This corresponds most closely to Menhir's proposed `broken_anchor` state.

### What Menhir can reuse

- Candidate heuristics for recognizing code-element references in prose.
- Evaluation repositories and reported failure modes.
- Test-fixture ideas for deleted or renamed symbols.
- Terminology around outdated code-element references.
- Baseline comparisons for Menhir's broken-anchor detector.

### What it does not provide

- Document graphlets.
- Section or claim identity.
- Shared canonical concepts.
- Stable temporal state across commits.
- Review receipts.
- A general code-change lifecycle.
- Claim-level stale propagation after a symbol body changes but still exists.
- Reverse graph traversal from symbols to claims, documents, and concepts.

### Recommendation

Treat DOCER as **nearest direct prior art and an evaluation source**, not as Menhir's base implementation.

A useful benchmark lane would compare:

```text
DOCER-style detection:
    deleted or missing code reference

Menhir detection:
    deleted reference
    renamed/moved symbol
    signature change
    body change
    explicit review receipt
```

---

## 2. SCIP: Code Intelligence Protocol

Repository: `scip-code/scip`

License: Apache-2.0

### What it does

SCIP is a language-agnostic protocol for source-code indexes. It represents symbols, occurrences, definitions, references, and relationships used for code navigation such as:

```text
go to definition
find references
find implementations
```

The project provides:

- a Protobuf schema
- Go and Rust bindings
- generated bindings for additional languages
- a CLI
- indexers for Python, TypeScript/JavaScript, Java-family languages, Rust, C/C++, Ruby, .NET, PHP, Dart, and others

### Why it matters to Menhir

The hardest foundational problem in change-aware docs is not Markdown parsing. It is stable code identity.

Raw identity based on lines is fragile:

```text
src/foo.py:42-78
```

A more durable identity resembles:

```text
repository
package or module
namespace or containing symbol
symbol kind
symbol name
signature/disambiguator
```

SCIP already defines a cross-language vocabulary for this space.

### What Menhir can reuse

#### Direct integration

Support optional import of SCIP indexes into Menhir's code structure graph.

```text
SCIP index
-> CodeSymbol nodes
-> Definition/Reference occurrences
-> File anchors
```

#### Identity compatibility

Even before import support, design `stable_symbol_id` so that it can map cleanly to SCIP symbols later.

#### Language coverage

Use external SCIP indexers where Menhir's native structure extractor lacks coverage or precision.

### What SCIP does not provide

- Documentation parsing.
- Claim modeling.
- Document graphlets.
- Stale-document policy.
- Git-triggered review state.
- Review receipts.
- Canonical concept graphs.

### Recommendation

Adopt a **SCIP-compatible symbol identity policy**, but keep SCIP ingestion optional.

V0 should be able to work with Menhir's existing symbol graph. A later adapter can import SCIP without changing document claim identity.

Suggested abstraction:

```python
class SymbolResolver(Protocol):
    def resolve(self, reference: CodeReference) -> ResolvedSymbol | None: ...
```

Potential implementations:

```text
MenhirStructureResolver
ScipIndexResolver
TreeSitterResolver
LanguageServerResolver
```

---

## 3. Kythe

Repository: `kythe/kythe`

License: Apache-2.0, with bundled third-party notices

### What it does

Kythe is a language-agnostic semantic code-graph ecosystem. It models code entities and relationships such as definitions, references, containment, types, and cross-language links.

Kythe is highly relevant conceptually because it treats source knowledge as a graph rather than a collection of independent search records.

### Useful ideas for Menhir

- Separate semantic nodes from source anchors.
- Treat occurrences/ranges as evidence or anchors, not as symbol identity.
- Use typed edges for definitions, references, and containment.
- Preserve provenance for extracted graph facts.
- Prefer incomplete-but-correct indexing over false confident links.

That last principle matches Menhir's invariant:

```text
Wrong current-state view is worse than a miss.
```

### What Menhir could borrow

Vocabulary and schema ideas for:

```text
CodeSymbol
File
SourceAnchor
Defines
RefersTo
ChildOf
Overrides
Implements
```

### Why not adopt it wholesale

Kythe is a substantial indexing ecosystem. Menhir already has:

- a Neo4j/Graphiti-backed memory graph
- a structure graph
- Hook Center events
- Git-aware temporal state
- stale-anchor behavior

Adding Kythe as the mandatory structure backend would introduce a second major graph/indexing platform before the document lifecycle has been proven.

### Recommendation

Use Kythe as **schema prior art**, not a v0 runtime dependency.

Revisit a Kythe adapter only if Menhir later needs enterprise-scale, multi-language code indexing that exceeds the native structure layer and SCIP imports.

---

## 4. Markdown parsing: markdown-it-py

Project: `markdown-it-py`

License: MIT

### What it does

`markdown-it-py` is a Python CommonMark parser that produces a token stream and supports plugins for additional syntax such as frontmatter and directives.

It can deterministically identify:

```text
headings
paragraphs
lists
code blocks
HTML comments
links
inline code
source line maps
```

### Why it fits Menhir

Menhir is primarily Python. A Python-native deterministic parser keeps v0 small and testable.

The parser can build a bounded document graphlet without LLM inference:

```text
Document
-> Section
-> explicit DocumentationClaim
-> CodeAnchor
```

Explicit claim markers can be stored in HTML comments, which Markdown parsers preserve as tokens:

```markdown
<!-- menhir:doc-claim id="sync-prices-retries" file="src/pricing/sync.py" symbol="sync_prices" priority="high" -->

`sync_prices()` retries failed API requests three times.

<!-- /menhir:doc-claim -->
```

### Alternative: remark / mdast

The JavaScript `remark` ecosystem exposes a rich Markdown AST and a mature plugin system. It is strong prior art for node types, directives, transformations, and source positions.

It is less attractive as Menhir's initial parser because it would add a Node runtime boundary to a Python service.

### Recommendation

Use `markdown-it-py` for v0.

Keep the internal parsed-document model independent of that parser so a future `remark`, MDX, reStructuredText, or AsciiDoc adapter can emit the same normalized graphlet events.

Suggested normalized events:

```text
DocumentStarted
SectionStarted
ClaimFound
CodeAnchorDeclared
ConceptDeclared
DocumentFinished
```

---

## 5. Tree-sitter

Project family: `tree-sitter` and language grammars

License: generally MIT for the core, with grammar-specific licenses

### What it does

Tree-sitter incrementally parses source code into concrete syntax trees and remains useful even while files contain syntax errors.

### Potential Menhir uses

- Resolve functions, classes, methods, and declarations.
- Compute current line ranges for stable symbols.
- Detect which symbol contains a changed Git hunk.
- Produce normalized body/signature hashes.
- Support languages not covered by a stronger semantic index.

### Limits

Tree-sitter provides syntax, not complete semantic identity. It may not resolve overloaded methods, imported names, dynamic dispatch, or cross-file semantics without additional logic.

### Recommendation

Use Tree-sitter as a **fallback structural resolver** and hunk-to-symbol mapper, not the only identity system.

A practical hierarchy:

```text
SCIP or language-semantic index available
    -> use semantic symbol

otherwise Tree-sitter symbol available
    -> use syntax-derived stable ID

otherwise file anchor only
    -> conservative file-level stale marking
```

---

## 6. GumTree

Repository family: `GumTreeDiff/gumtree`

License: LGPL-3.0

### What it does

GumTree performs syntax-aware code differencing. It maps syntax-tree nodes across revisions and can identify moves, updates, insertions, and deletions more accurately than line diff alone.

### Why it matters

The difficult change cases are:

```text
symbol moved to a different file
symbol renamed
method extracted
body reordered without semantic change
signature changed
implementation replaced
```

A raw Git diff can tell Menhir where text changed. A syntax-aware differ can better characterize what structural element changed.

### What Menhir could use

- Rename/move candidate generation.
- Mapping old symbol locations to new symbol locations.
- Structural change summaries attached to stale-document warnings.
- Better review prioritization.

### Why not v0

- Adds a separate tool/runtime boundary.
- LGPL needs deliberate dependency/distribution handling.
- Menhir can prove the core lifecycle using existing symbol snapshots and Git diffs.
- Semantic-change classification remains unresolved even with an AST differ.

### Recommendation

Defer GumTree to an optional **change-classification adapter**.

First record enough benchmark fixtures to know whether native symbol snapshots fail on rename/move cases often enough to justify it.

---

## 7. Doc Detective

Repository: `doc-detective/doc-detective`

License: AGPL-3.0

### What it does

Doc Detective treats documentation as executable tests. It parses test specifications or testable actions from documents and can execute browser, API, shell, and code actions.

Its output is structured JSON containing pass/fail results and context.

### Why it complements Menhir

Menhir's change-aware docs answer:

```text
The code this claim describes changed.
This claim needs review.
```

Doc Detective can answer:

```text
The documented procedure, request, command, or UI action still works.
```

These are different evidence types.

Example:

```text
Claim:
“Run `menhir status --json` to retrieve stale counts.”

Menhir trigger:
CLI implementation changed.

Doc Detective verification:
Execute command and validate output shape.
```

### Integration strategy

Treat Doc Detective as an external verifier:

```text
DocReviewTask
-> run external Doc Detective test
-> ingest JSON result
-> create DocumentationReviewReceipt
```

Do not copy its implementation into Menhir by default.

### License consideration

AGPL-3.0 is materially different from Menhir's likely permissive dependency preferences. Calling an independently installed CLI and ingesting its output is a cleaner boundary than embedding or modifying its source.

### Recommendation

Optional integration only, after claim graphlets and review receipts exist.

---

## 8. Sphinx Domains and inventories

Project: Sphinx

License: BSD-2-Clause

### What it does

Sphinx domains assign typed identities to documented programming objects and support cross-references such as functions, methods, classes, and modules.

Sphinx can also produce inventories used for resolving cross-project references.

### Why it matters

Projects using Sphinx may already contain explicit code-object references. Menhir should not require authors to repeat those references in a second annotation format.

### Potential adapter

```text
Sphinx source + domain roles + inventory
-> DocumentationClaim or DocumentationReference
-> CodeAnchor
```

Examples that may be ingestible:

```text
:py:func:
:py:meth:
:py:class:
:js:func:
:cpp:class:
```

### Recommendation

Add a Sphinx adapter after the Markdown explicit-marker lane proves the normalized claim/anchor model.

The core should consume normalized graphlet records, not be tied to Markdown comments.

---

## 9. Backstage TechDocs

Project: Backstage TechDocs

License: Apache-2.0 as part of Backstage

### What it does

TechDocs provides docs-as-code connected to catalog entities, ownership metadata, repositories, and service discovery.

It is strong prior art for the **document header on the main graph** concept:

```text
Catalog entity
-> owned documentation
-> repository source
-> generated site
```

### What Menhir can borrow

- Repository/catalog ownership metadata.
- Document discoverability and service association.
- Source-of-truth-in-Git policy.
- Separation between source docs and rendered artifacts.

### What it does not provide

- Claim-level code anchors.
- Symbol-aware stale propagation.
- Document graphlets.
- Review receipts tied to code changes.
- Shared canonical concept traversal.

### Recommendation

Treat TechDocs as product and metadata prior art. A future integration could attach Menhir stale counts and review state to Backstage catalog entities without replacing TechDocs rendering.

---

## 10. Antora

Project: Antora

License: MPL-2.0

### What it does

Antora provides a structured documentation model centered on components, versions, modules, pages, resources, and cross-references.

### Why it matters

Antora is strong prior art for problems Menhir should postpone but eventually support:

- versioned API docs
- multiple maintained release lines
- component/module identity
- resource IDs independent of output paths
- cross-version links

### Recommendation

Borrow its version/resource identity ideas when Menhir designs version-aware documentation policies.

Do not add Antora as a core dependency unless Menhir is ingesting an Antora project.

---

## Comparison matrix

| Project | Primary value | Directly reusable? | License | Menhir recommendation |
|---|---|---:|---|---|
| DOCER | Detect outdated code-element references | Partly | MIT | Use heuristics, fixtures, and baseline comparisons |
| SCIP | Stable language-agnostic code-symbol index | Yes, optional adapter | Apache-2.0 | Adopt compatible identity; add import later |
| Kythe | Semantic code-graph schema and ecosystem | Conceptually | Apache-2.0 | Borrow schema ideas; do not require in v0 |
| markdown-it-py | Deterministic Markdown parsing | Yes | MIT | Preferred v0 document graphlet parser |
| remark/mdast | Rich Markdown AST/plugin ecosystem | Via adapter | MIT ecosystem | Useful prior art; avoid Node dependency in v0 |
| Tree-sitter | Incremental syntax trees and ranges | Yes | MIT/core | Fallback symbol resolver and hunk mapper |
| GumTree | Syntax-aware structural diffs | Optional | LGPL-3.0 | Defer until rename/move benchmarks justify it |
| Doc Detective | Executable documentation tests | Via CLI/JSON | AGPL-3.0 | Optional external verifier only |
| Sphinx Domains | Explicit typed code-object references | Via adapter | BSD-2-Clause | Ingest existing references later |
| Backstage TechDocs | Catalog/document ownership and docs-as-code | Via integration | Apache-2.0 | Product/metadata prior art |
| Antora | Versioned component/resource model | Via adapter | MPL-2.0 | Borrow version identity later |

---

## Recommended Menhir stack

### V0: Document Graphlet Indexer

Use:

```text
markdown-it-py
Menhir existing code structure graph
explicit menhir:doc-claim markers
Git as canonical document storage
Neo4j/Graphiti for graphlet metadata and relationships
```

Build:

```text
Document
Section
DocumentationClaim
CodeAnchor
CodeSymbol/File connection
```

No automatic concept extraction.
No semantic-change classifier.
No external indexer required.

### V1: Change-aware stale propagation

Use:

```text
Hook Center file_changed events
Git changed hunks
Menhir symbol ranges/snapshots
claim -> symbol edges
```

Build:

```text
symbol body/signature changed
-> linked claim needs_review
-> warning includes commit and affected symbol
```

### V2: Review receipts and reverse lookup

Reuse Menhir's stale-anchor receipt pattern:

```text
still_valid
outdated
revised
dismissed
needs_review
broken_anchor
```

Add queries:

```text
Which docs describe this symbol?
Which claims became stale after this commit?
Which high-priority docs are awaiting review?
```

### V3: External code intelligence adapters

Add optional:

```text
SCIP index import
Tree-sitter fallback resolver
Sphinx domain/inventory ingestion
```

### V4: Structural change classification

Evaluate native symbol snapshots first.

If needed, add:

```text
GumTree adapter
rename/move correlation
signature/body/change-kind classification
```

### V5: Executable verification

Add external verifier interface:

```python
class DocumentationVerifier(Protocol):
    def verify(self, task: DocumentationReviewTask) -> VerificationResult: ...
```

Possible implementations:

```text
DocDetectiveVerifier
ShellCommandVerifier
HttpRequestVerifier
PythonDoctestVerifier
```

---

## Proposed adapter boundaries

Avoid importing external project models directly into domain services.

### Document parser

```python
class DocumentGraphletParser(Protocol):
    def parse(self, source: DocumentSource) -> ParsedDocumentGraphlet: ...
```

Implementations:

```text
MarkdownItGraphletParser
SphinxGraphletParser
AntoraGraphletParser
MdxGraphletParser
```

### Symbol resolver

```python
class SymbolResolver(Protocol):
    def resolve(self, reference: CodeReference) -> ResolvedSymbol | None: ...
```

Implementations:

```text
MenhirStructureResolver
ScipIndexResolver
TreeSitterResolver
```

### Change classifier

```python
class SymbolChangeClassifier(Protocol):
    def classify(self, before: SymbolSnapshot, after: SymbolSnapshot) -> SymbolChange: ...
```

Implementations:

```text
HashAndSignatureClassifier
TreeSitterChangeClassifier
GumTreeChangeClassifier
```

### Documentation verifier

```python
class DocumentationVerifier(Protocol):
    def verify(self, task: DocumentationReviewTask) -> VerificationResult: ...
```

Implementations:

```text
ManualReceiptVerifier
DocDetectiveVerifier
CommandVerifier
HttpVerifier
```

These seams let Menhir reuse OSS without coupling the document lifecycle to one parser or code-index format.

---

## Licensing notes

This is not legal advice; licenses should be reviewed before distributing integrations.

### Low-friction candidates

```text
DOCER            MIT
markdown-it-py   MIT
SCIP              Apache-2.0
Kythe             Apache-2.0 plus notices
Tree-sitter       MIT/core, grammar-specific licenses vary
Sphinx            BSD-2-Clause
Backstage         Apache-2.0
```

### Require deliberate boundaries

```text
GumTree           LGPL-3.0
Doc Detective     AGPL-3.0
Antora            MPL-2.0
```

For AGPL and LGPL tools, external-process adapters may provide a cleaner architectural and licensing boundary than embedding or copying code. That still requires project-specific legal review.

---

## Novelty boundary

Menhir should not claim novelty for:

- parsing documents into structural nodes
- code-symbol indexing
- detecting deleted code references
- syntax-aware diffs
- executable documentation tests
- docs-as-code catalog ownership

The potentially distinct combination is:

```text
bounded document graphlets
+ shared canonical concept graph
+ stable code-symbol anchors
+ temporal change propagation
+ claim-level stale state
+ durable review receipts
+ reverse traversal across docs, concepts, code, commits, and memories
```

Even this should be presented as a systems integration and lifecycle design until a broader prior-art review confirms the nearest academic and commercial systems.

---

## Research questions to validate before implementation

1. Does Menhir's current structure graph provide stable enough symbol IDs across edits and moves?
2. Can existing symbol snapshots reliably map Git hunks to functions/classes?
3. Which Markdown parser exposes the source positions and HTML comments needed for explicit claim blocks most cleanly?
4. How many real docs require claim-level granularity versus section-level anchors?
5. How should one claim reference multiple symbols or an entire module?
6. How should renamed symbols preserve anchor continuity?
7. Can SCIP imports be incremental, or would every commit require a full index replacement?
8. Which Sphinx roles and inventories can be converted deterministically?
9. Do GumTree results materially outperform Menhir's native symbol hashes on the benchmark fixture set?
10. Should external verifier results automatically produce `still_valid`, or only proposed receipts requiring approval?

---

## Benchmark fixture ideas derived from prior art

Build fixtures covering:

```text
symbol deleted
symbol renamed in same file
symbol moved to another file
signature changed
body changed, signature stable
comments/formatting only
unrelated symbol changed in same file
line range shifted without symbol change
document reference already broken at indexing time
one claim anchored to multiple symbols
multiple claims anchored to one symbol
Sphinx explicit code reference
executable command remains valid after implementation refactor
executable command fails after CLI change
```

Expected outcomes should distinguish:

```text
current
needs_review
high_priority_review
broken_anchor
still_valid_after_review
outdated_after_review
```

DOCER provides the natural baseline for deleted/broken-reference cases. Menhir's value should be measured on the remaining temporal and lifecycle cases.

---

## Decision

Proceed with a Menhir-native implementation.

The first implementation slice should use:

```text
markdown-it-py
+ explicit Markdown claim markers
+ existing Menhir structure graph
+ SCIP-compatible symbol IDs
```

Do not add a mandatory external service.
Do not fork DOCER.
Do not embed Kythe.
Do not require GumTree.
Do not embed Doc Detective.

Design adapters now so those projects can be integrated later without changing the core document graphlet and review-lifecycle model.

## References

- DOCER repository: https://github.com/wesleytanws/DOCER
- DOCER paper: https://arxiv.org/abs/2212.01479
- SCIP: https://github.com/scip-code/scip
- Kythe: https://github.com/kythe/kythe
- Kythe overview: https://kythe.io/docs/kythe-overview.html
- markdown-it-py: https://github.com/executablebooks/markdown-it-py
- remark parse: https://github.com/remarkjs/remark
- Tree-sitter: https://github.com/tree-sitter/tree-sitter
- GumTree: https://github.com/GumTreeDiff/gumtree
- Doc Detective: https://github.com/doc-detective/doc-detective
- Sphinx domains: https://www.sphinx-doc.org/en/master/usage/domains/index.html
- Backstage TechDocs: https://backstage.io/docs/features/techdocs/
- Antora: https://antora.org/
