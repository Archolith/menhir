# Change-Aware Documentation

> Status: design + implementation plan
> Lane: documentation claims anchored to code
> Goal: make docs reviewable when the code they describe changes

## Summary

Documentation is memory about code. When the code changes, the documentation that describes it may become stale. Menhir already has the primitives for this pattern in the Hook Center stale-anchor lane:

```text
file/tool event
-> dirty/stale detection
-> anchored memory marked stale
-> recall/context warns the agent
-> verification receipt records the review outcome
```

Change-aware documentation applies the same lifecycle to docs:

```text
documentation claim
-> code anchor
-> code/symbol change
-> documentation claim marked needs_review
-> reviewer confirms, revises, or dismisses
-> review receipt records the outcome
```

The product value is straightforward:

- show which docs describe a function, class, script, or API
- show which docs may be stale after a commit or PR
- warn agents before they rely on outdated docs
- let reviewers confirm, revise, dismiss, or mark a doc claim outdated
- optionally fail CI for stale high-priority documentation

The v0 design should be explicit and deterministic. Do not start with fuzzy paragraph inference or semantic-change classification. Start with explicit doc-claim anchors, symbol-level code references, and conservative stale marking.

## Core principle

```text
Documentation claim -> CodeAnchor -> ChangeEvent -> ReviewState / ReviewReceipt
```

Important invariants:

```text
Wrong documentation presented as current is worse than no documentation.
```

```text
A code change should not silently invalidate an anchored documentation claim.
```

```text
A review receipt is audit evidence; it should not automatically rewrite docs or mark code clean unless a later lifecycle pack explicitly defines that behavior.
```

```text
Symbol-level anchors are preferred over raw line anchors because line numbers drift during refactors.
```

## Example

A Markdown file contains this claim:

```markdown
<!-- menhir:doc-claim id="sync-prices-retries" file="src/pricing/sync.py" symbol="sync_prices" priority="high" -->

`sync_prices()` retries failed API requests three times.

<!-- /menhir:doc-claim -->
```

Menhir indexes the claim and resolves it to the `sync_prices` function. If the function changes in commit `abc123`, Menhir marks the claim as needing review:

```text
This documentation may be outdated because sync_prices() changed in commit abc123.
```

A reviewer can then choose:

```text
still_valid
outdated
revised
dismissed
needs_review
broken_anchor
```

## Scope boundaries

### In scope

- explicit doc-claim anchors in Markdown
- symbol-level references to code
- optional line/range metadata for display
- stale marking when anchored symbols change
- reverse lookup from code symbol to docs
- read-only stale documentation reports
- review receipts for doc claims
- optional CI checks for high-priority stale docs after the base lifecycle works

### Out of scope for v0

- automatic doc rewriting
- semantic-change classification
- LLM-only inference of affected paragraphs
- CI blocking by default
- generated documentation workflows
- versioned API policy
- automatic supersession of documentation claims
- storing file contents or transcripts

## Data model

### DocumentationClaim

A documentation claim is the smallest reviewable unit. For v0, it should be an explicitly marked block in a Markdown document.

```json
{
  "claim_id": "sync-prices-retries",
  "doc_path": "docs/pricing.md",
  "section_heading": "Retry behavior",
  "claim_text": "sync_prices() retries failed API requests three times.",
  "priority": "high",
  "status": "current"
}
```

Suggested fields:

| Field | Meaning |
| --- | --- |
| `claim_id` | Stable ID from the doc marker |
| `doc_path` | Markdown file containing the claim |
| `section_heading` | Nearest heading, if available |
| `claim_text` | Text inside the claim block |
| `priority` | `low`, `normal`, `high`, `critical` |
| `owner` | Optional reviewer/team |
| `status` | `current`, `needs_review`, `reviewed`, `dismissed`, `broken_anchor` |

### CodeAnchor

A code anchor identifies the code described by a documentation claim.

```json
{
  "anchor_id": "anchor_456",
  "repo": "Archolith/menhir",
  "project": "menhir",
  "file": "src/pricing/sync.py",
  "symbol": "sync_prices",
  "symbol_kind": "function",
  "stable_symbol_id": "python:function:src/pricing/sync.py::sync_prices",
  "line_start": 42,
  "line_end": 78,
  "commit": "abc123"
}
```

The durable identity should be symbol-oriented:

```text
project + file + symbol_kind + symbol
```

Line numbers should be treated as display metadata, not identity.

### DocumentationClaimAnchor

A relationship connecting a claim to a code anchor.

```json
{
  "claim_id": "sync-prices-retries",
  "anchor_id": "anchor_456",
  "created_at": "2026-07-10T00:00:00Z",
  "created_from": "explicit_marker"
}
```

### CodeChangeEvent

A change event should be derived from Git diff, Hook Center file events, or symbol index comparison.

```json
{
  "event_type": "symbol_changed",
  "project": "menhir",
  "file": "src/pricing/sync.py",
  "symbol": "sync_prices",
  "symbol_kind": "function",
  "old_commit": "abc123",
  "new_commit": "def456",
  "change_kind": "body_changed"
}
```

Suggested change kinds:

```text
body_changed
signature_changed
symbol_deleted
symbol_renamed
file_moved
docstring_only
formatting_only
unknown
```

For v0, only a few kinds are required:

```text
body_changed
signature_changed
symbol_deleted
unknown
```

### DocumentationReviewReceipt

A review receipt records the result of inspecting the doc claim after code changed.

```json
{
  "claim_id": "sync-prices-retries",
  "anchor_id": "anchor_456",
  "outcome": "still_valid",
  "reviewed_at": "2026-07-10T00:00:00Z",
  "reviewed_by": "agent",
  "basis": "inspected_current_symbol",
  "notes": "Retry count remains three after the refactor.",
  "code_commit": "def456",
  "doc_commit": "abc123"
}
```

Allowed outcomes:

```text
still_valid
outdated
revised
dismissed
needs_review
broken_anchor
```

Outcome meanings:

| Outcome | Meaning |
| --- | --- |
| `still_valid` | Code changed, reviewer checked the claim, and the claim still appears correct |
| `outdated` | Claim no longer matches current code |
| `revised` | Documentation was updated to match current code |
| `dismissed` | Warning is not relevant and should be hidden for this code version/change |
| `needs_review` | Reviewer/agent could not confidently decide |
| `broken_anchor` | Anchor cannot be resolved to current code |

## Anchor syntax

Use explicit Markdown markers for v0.

```markdown
<!-- menhir:doc-claim id="sync-prices-retries" file="src/pricing/sync.py" symbol="sync_prices" kind="function" priority="high" -->

`sync_prices()` retries failed API requests three times.

<!-- /menhir:doc-claim -->
```

Supported marker attributes:

| Attribute | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable claim ID unique within the repo |
| `file` | yes | Repository-relative code file path |
| `symbol` | recommended | Function/class/method/script symbol |
| `kind` | optional | `function`, `class`, `method`, `module`, `script`, `api` |
| `priority` | optional | `low`, `normal`, `high`, `critical` |
| `owner` | optional | Responsible reviewer/team |
| `line_start` | optional | Display hint only |
| `line_end` | optional | Display hint only |

Line-only anchors should be allowed only as a fallback:

```markdown
<!-- menhir:doc-claim id="legacy-config-note" file="src/config.py" line_start="20" line_end="35" priority="normal" -->
```

Line-only anchors should be reported as less stable than symbol anchors.

## Review states

A documentation claim can have one computed state per anchor.

```text
current
needs_review
reviewed
dismissed
broken_anchor
```

Suggested meaning:

| State | Meaning |
| --- | --- |
| `current` | No known code change has invalidated the claim since last index/review |
| `needs_review` | Anchored code changed after the claim was indexed or last reviewed |
| `reviewed` | Reviewer confirmed the claim against the changed code |
| `dismissed` | Reviewer dismissed the warning for this change/version |
| `broken_anchor` | Menhir cannot resolve the anchor to current code |

State should be computed from durable facts, not manually overwritten as the source of truth.

For example:

```text
claim anchored at commit A
symbol changed at commit B
no post-B review receipt
=> needs_review
```

```text
claim anchored at commit A
symbol changed at commit B
receipt still_valid at commit C after B
=> reviewed/current-with-receipt
```

## Storage responsibility: Git vs Menhir

Use both, with clear responsibilities.

Git should store portable project metadata:

- explicit doc-claim markers
- claim ID
- code file/symbol anchor
- priority
- optional owner/policy

Menhir should store computed/evolving state:

- resolved anchors
- stale/review state
- code change events
- review receipts
- reverse lookup indexes
- reports/diagnostics

Do not store generated review state directly in Markdown in v0. Exporting summaries back to Git can be a later feature.

## Change detection policy

For v0, be conservative and deterministic.

```text
symbol body changed -> needs_review
symbol signature changed -> needs_review_high
symbol deleted -> broken_anchor
symbol unresolved -> broken_anchor
unrelated file change -> no doc stale
```

Do not attempt semantic classification in v0.

Later versions may classify:

```text
formatting_only
docstring_only
rename_only
likely_behavior_change
likely_refactor_only
```

But v0 should prefer false-positive review prompts over false-negative stale docs.

## Generated docs and versioned APIs

Generated docs need a separate policy.

```text
generated docs stale -> regenerate
manual docs stale -> review
versioned API docs stale -> compare against branch/tag/version target
```

Do not mix generated-doc workflows into v0.

For versioned APIs, anchors should eventually include a version target:

```json
{
  "api_version": "v1",
  "source_ref": "release/v1",
  "symbol": "sync_prices"
}
```

That is a later lane.

## Can Menhir infer affected paragraphs?

Eventually, yes. Not in v0.

Build order:

```text
explicit anchors
-> section-level anchors
-> paragraph inference
-> claim extraction
-> semantic affected-claim scoring
```

Explicit anchors are the reliable substrate. Inference should come later as an assistant that proposes anchors, not as the source of truth.

## Proposed implementation sequence

### Pack 1 — Documentation Claim Anchor Registry v0

Goal: parse explicit doc-claim markers and build a registry of documentation claims linked to code anchors.

Non-goals:

- no stale marking
- no CI checks
- no generated-doc support
- no LLM paragraph inference
- no automatic docs rewrite

Likely files:

```text
src/menhir/services/doc_claims.py
src/menhir/infrastructure/doc_claim_repository.py
scripts/maintenance/index_doc_claims.py
tests/test_doc_claims.py
docs/change-aware-documentation.md
CHANGELOG.md
```

CLI:

```bash
python scripts/maintenance/index_doc_claims.py --project menhir --docs docs --json
```

Output:

```json
{
  "claims_indexed": 12,
  "anchors_resolved": 10,
  "broken_anchors": 2,
  "claims": [
    {
      "claim_id": "sync-prices-retries",
      "doc_path": "docs/pricing.md",
      "file": "src/pricing/sync.py",
      "symbol": "sync_prices",
      "symbol_kind": "function",
      "priority": "high",
      "status": "resolved"
    }
  ]
}
```

Acceptance criteria:

- parses explicit Markdown doc-claim blocks
- rejects duplicate claim IDs within a project
- records claim text and nearest heading
- records code file/symbol/kind/priority/owner
- marks anchors as unresolved/broken if file or symbol cannot be resolved
- does not require a live backend for parser tests
- does not capture unrelated file contents beyond the explicit claim block being indexed

Tests:

```bash
pytest tests/test_doc_claims.py -q
pytest tests -q -k "doc_claim or documentation or anchor"
```

### Pack 2 — Documentation Stale Detection v0

Goal: given a changed file/symbol/commit, mark linked documentation claims as needing review.

Non-goals:

- no semantic classification
- no automatic rewrite
- no review receipts yet
- no CI blocking yet

Inputs:

```json
{
  "project": "menhir",
  "file": "src/pricing/sync.py",
  "symbol": "sync_prices",
  "symbol_kind": "function",
  "commit": "def456",
  "change_kind": "body_changed"
}
```

Output:

```json
{
  "stale_doc_claims": [
    {
      "claim_id": "sync-prices-retries",
      "doc_path": "docs/pricing.md",
      "reason": "anchored_symbol_changed",
      "symbol": "sync_prices",
      "commit": "def456",
      "priority": "high"
    }
  ],
  "count": 1
}
```

Potential endpoint:

```text
GET /api/docs/stale-claims?project=menhir
```

Potential CLI:

```bash
python scripts/maintenance/report_stale_docs.py --project menhir --json
```

Acceptance criteria:

- symbol body/signature change marks linked claims `needs_review`
- symbol deletion marks linked claims `broken_anchor`
- unrelated symbol changes do not affect unrelated docs
- line-only anchors are supported but flagged as fragile
- no filtering or deletion of claims
- no automatic doc updates

Tests:

```bash
pytest tests/test_doc_stale_detection.py -q
pytest tests -q -k "doc_claim or stale_doc or symbol or hook_center"
```

### Pack 3 — Documentation Review Receipts v0

Goal: record review outcomes for stale documentation claims.

Endpoint shape:

```text
POST /api/docs/review-receipts
GET  /api/docs/review-receipts
```

POST body:

```json
{
  "claim_id": "sync-prices-retries",
  "project": "menhir",
  "doc_path": "docs/pricing.md",
  "file": "src/pricing/sync.py",
  "symbol": "sync_prices",
  "outcome": "still_valid",
  "reviewed_by": "agent",
  "basis": "inspected_current_symbol",
  "notes": "Retry behavior remains three attempts."
}
```

Acceptance criteria:

- records review receipts as durable audit events
- validates allowed outcomes
- post-change receipts can resolve `needs_review` state
- pre-change receipts do not reassure current stale docs
- receipts do not rewrite docs
- receipts do not mutate code
- receipts do not clear Hook Center dirty state

Tests:

```bash
pytest tests/test_doc_review_receipts.py -q
pytest tests -q -k "doc_claim or review_receipt or stale_doc or api"
```

### Pack 4 — Reverse Lookup v0

Goal: ask which docs describe a code object.

Endpoint/CLI:

```text
GET /api/docs/for-symbol?project=menhir&file=src/pricing/sync.py&symbol=sync_prices
```

Output:

```json
{
  "docs": [
    {
      "claim_id": "sync-prices-retries",
      "doc_path": "docs/pricing.md",
      "section_heading": "Retry behavior",
      "priority": "high",
      "state": "current"
    }
  ],
  "count": 1
}
```

Acceptance criteria:

- returns claims anchored to a file/symbol
- includes review/stale state
- supports file-only lookup
- supports symbol lookup
- no mutation

### Pack 5 — CI Advisory v0

Goal: optional CI command for stale high-priority docs.

Command:

```bash
python scripts/maintenance/check_stale_docs.py --project menhir --priority high --fail-on-stale
```

Default behavior should be report-only.

Exit behavior:

```text
0 = no stale claims above threshold
1 = stale claims found above threshold and --fail-on-stale was set
```

Acceptance criteria:

- report-only by default
- fail only with explicit flag
- can filter by priority
- can emit JSON for CI artifacts
- does not require LLM calls

## Suggested graph model

```text
(:DocumentationClaim {claim_id, doc_path, section_heading, claim_text, priority, owner})
(:CodeAnchor {file, symbol, symbol_kind, stable_symbol_id, line_start, line_end, commit})
(:CodeChangeEvent {file, symbol, change_kind, commit, changed_at})
(:DocumentationReviewReceipt {claim_id, outcome, reviewed_at, reviewed_by, basis, notes})
```

Relationships:

```text
(:DocumentationClaim)-[:DESCRIBES]->(:CodeAnchor)
(:CodeChangeEvent)-[:TOUCHES]->(:CodeAnchor)
(:DocumentationClaim)-[:HAS_REVIEW_RECEIPT]->(:DocumentationReviewReceipt)
(:DocumentationReviewReceipt)-[:REVIEWS_ANCHOR]->(:CodeAnchor)
```

Computed state rules:

```text
No relevant change after latest index/review -> current
Relevant change after latest review -> needs_review
Anchor cannot resolve -> broken_anchor
Latest review outcome dismissed -> dismissed for that change
Latest review outcome still_valid after change -> reviewed/current-with-receipt
Latest review outcome outdated -> needs_revision
Latest review outcome revised -> current/reviewed
```

## API sketch

Potential endpoints, staged across packs:

```text
POST /api/docs/index-claims
GET  /api/docs/claims
GET  /api/docs/stale-claims
GET  /api/docs/for-symbol
POST /api/docs/review-receipts
GET  /api/docs/review-receipts
```

Tiering suggestion:

```text
GET endpoints -> readonly
index/report endpoints -> agent
review receipt POST -> agent
admin destructive/reset endpoints, if ever added -> operator
```

## Formatter / MCP output

When a doc claim is stale, expose a concise warning:

```json
{
  "claim_id": "sync-prices-retries",
  "doc_path": "docs/pricing.md",
  "claim_text": "sync_prices() retries failed API requests three times.",
  "doc_stale": true,
  "doc_stale_reason": "anchored_symbol_changed",
  "code_anchor": {
    "file": "src/pricing/sync.py",
    "symbol": "sync_prices",
    "symbol_kind": "function"
  },
  "stale_action": "review_documentation_against_current_symbol",
  "stale_advisory": "This documentation claim describes code that changed after the claim was indexed or reviewed. Inspect the current symbol before relying on the claim."
}
```

For reverse lookup:

```json
{
  "file": "src/pricing/sync.py",
  "symbol": "sync_prices",
  "documentation_claims": [
    {
      "claim_id": "sync-prices-retries",
      "doc_path": "docs/pricing.md",
      "state": "needs_review",
      "priority": "high"
    }
  ]
}
```

## Real DB smoke plan

Once Pack 1-3 exist, add smoke coverage to GitHub issue #25 or a dedicated doc-staleness smoke issue.

Smoke fixture:

1. Create a doc claim anchored to a simple function.
2. Index doc claims.
3. Change the function body.
4. Emit or ingest a symbol-changed event.
5. Confirm stale doc claim appears.
6. Confirm reverse lookup shows the doc.
7. Record `still_valid` review receipt.
8. Confirm claim no longer appears as unreviewed stale for that change.
9. Record `outdated` receipt for another claim.
10. Confirm strong advisory appears.
11. Confirm wrong-symbol/wrong-path receipts do not reassure.
12. Confirm pre-change receipts do not reassure.

Safety checks:

```text
no file content capture beyond explicit doc claim text
no transcripts captured
no automatic doc rewrite
no code mutation
no dirty clearing
no CI blocking unless explicitly requested
```

## Open questions and recommended answers

### Should changes trigger review only for semantic changes, or any edit?

For v0: any symbol body/signature change should trigger review.

Reason: deterministic review prompts are safer than missing stale documentation. Semantic classification can be added later as an advisory label, not as the source of truth.

### How should generated docs be handled?

Generated docs should use a separate `generated=true` policy.

Suggested behavior:

```text
generated docs stale -> regenerate
manual docs stale -> review
```

Do not mix this into v0.

### Can Menhir infer which paragraph is affected by a code change?

Eventually yes, but v0 should not depend on inference. Use explicit markers first. Later, add an assistant that proposes anchors and claim spans for review.

### Should review status be stored in Git, Menhir, or both?

Use Git for explicit anchors and policy. Use Menhir for computed state and review receipts. Export back to Git later only if needed.

## Recommended first task

Start with Pack 1:

```text
Documentation Claim Anchor Registry v0
```

This establishes the registry of explicit doc claims and code anchors. Everything else depends on it.

PR title:

```text
feat: add documentation claim anchor registry v0
```

Expected first implementation branch:

```text
feat/doc-claim-anchor-registry-v0
```

Definition of done:

```text
Explicit Markdown doc-claim anchors can be parsed, validated, resolved to code anchors where possible, and reported without stale marking or mutation.
```
