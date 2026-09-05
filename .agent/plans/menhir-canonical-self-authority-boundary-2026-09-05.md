---
artifact_schema: 1
artifact_uuid: 6c32dbb8-30b6-49df-a31e-491d424051aa
artifact_type: plan
artifact_status: IMPLEMENTING
---

# Canonical self: structural identity and verified attribution

Date: 2026-09-05. Owner: Charlie. Status: implementing; default-off; activation not authorized.
Base: `Archolith/menhir`, `feat/canonical-self-subject-endpoint-20260904`,
`a9d74bf0547a37499f4bebec6b263a054ae02bd4`.

**Recommendation: a hybrid.** Establish one canonical author identity structurally. Keep
AI-extracted self facts as non-authoritative proposals until the owner explicitly confirms the
exact assertion through a trusted mechanism. Never turn language interpretation into identity
or assertion authority. This protects attribution; it does not certify real-world truth.

**Authorization:** on 2026-09-05 the owner approved the recommended hybrid contract and requested
repository implementation. That approval covers code, documentation and local unit tests only. It
does not authorize deployment, production activation, a database write, disposable Docker tests,
live-provider tests or historical remediation. Each remains a separate approval gate.

## Why

The reviewed producer still converts `_requires_declared_author_endpoint()` and model-selected
marker edges into `EXPLICIT_SELF_SUBJECT`. The counterexample is:

```text
She said:
I will handle the deployment.
```

The scanner accepts the second line as the author's statement. The handoff records successful
final declaration and binding; the prior review independently reproduced the scanner behavior,
not the complete application. The binder's correct local invariant is therefore insufficient.
Do not expand quotation patterns or call a matching marker proof of speaker attribution.

## Scope

In scope: the complete canonical-self authority chain; semantic attachments from Graphiti,
typed-scalar and event-history writers; repair, replay, bulk persistence, and readers that could
promote proposals into self facts. Inventory other direct canonical-UUID writers before claiming
product-wide coverage. Preserve ordinary RBAC, application, database, customer and third-person
entities named `user`.

Out of scope: unrelated extraction improvements, provider changes, general memory redesign,
production activation, and consolidation/reclassification/deletion of historical forks. Roughly
66 same-named entities is historical handoff context, not a current census or proof they are self.

## Proposed Design

Extend existing admission, assertion, provenance, lifecycle and recall contracts rather than adding
a parallel memory store. Exact reusable schemas and writer locations are a Phase 0 deliverable.
The following names describe contracts, not preselected new classes or graph labels.

| Contract | Establishes | Does not establish |
|---|---|---|
| Author-of-turn evidence | Authenticated owning human, namespace, turn and projection lineage | Which extracted entity or proposition refers to that human |
| Canonical author endpoint | Menhir-constructed identity, using `self_uuid_for_namespace()` | Permission to attach arbitrary model-generated facts |
| Assertion authorization | Owner-confirmed subject/object, predicate, value, polarity and scope | General authority for other edges on the same node or turn |

Data flow: **trusted author metadata -> server-owned endpoint before extraction; free text ->
proposals with evidence; exact confirmed assertion -> guarded canonical attachment.** Authorship
can relate the author to a turn without asserting that every claim in the turn is about them.

The model may reference an existing transport handle but cannot mint identity authority or select
an extracted UUID for promotion. Create the structural endpoint independently of whether extraction
returns a marker or relationship. Keep unproven author references proposal-local/unresolved; do
not create a durable substitute self identity merely to avoid orphan pruning. Ordinary non-self
entities retain ordinary resolution. One canonical identity does not mean only one node named
`user`, nor that every unresolved reference has been semantically identified.

Proposals must not have authoritative domain edges to canonical self. Reuse an existing proposal
representation with explicit non-authoritative status and source provenance; confirm that no
reader treats its proposed subject as a settled graph relationship. A new persistent type or API
requires an explicit design delta before implementation.

Promotion requires a server-verifiable record of the owner's deliberate approval of the exact
structured assertion. Bind it to principal, namespace, claim digest/revision, evidence lineage and
policy version, including endpoint direction, polarity and temporal scope. A caller-supplied flag,
operator-tier agent credential, model output, source-span match, quote parser, confidence score,
second model, or unforgeable transport token is not owner confirmation. Select a nondelegated
confirmation mechanism in Phase 0; without one, ship no automatic promotion path.

Revalidate at the final write, make promotion idempotent, and invalidate/revoke dependent authority
when its evidence or approval is erased or changed. Confirmation is attribution approval, not
verification that the asserted fact is true.

## Alternatives Considered

| Option | Benefit | Cost / limitation |
|---|---|---|
| Automatic inferred self facts | Smallest change; retains automatic personalized recall | Occasional speaker misattribution is explicitly accepted; weaker contract |
| Verified facts only | Smallest strict attribution surface | Most ordinary personal statements remain unbound; confirmation burden |
| Hybrid, recommended | Preserves extraction and evidence while protecting authoritative facts | Proposal lifecycle and reader isolation add complexity; no model-only promotion |

**Approved decision:** implement the hybrid contract. Default authoritative recall excludes
proposals; an operator may inspect the episode-local receipt as uncertain evidence, never as a
settled fact about the owner. Do not silently downgrade this guarantee.

### Phase 0 implementation decision

- Menhir verifies offline Ed25519 owner confirmations and never exposes a signer or stores a
  private key. The verifier pins the SHA-256 fingerprint of the raw 32-byte public key.
- A confirmation signs canonical compact, sorted UTF-8 JSON for one exact assertion payload:
  principal, namespace, episode and turn lineage, evidence digest, lane, endpoint direction,
  polarity, assertion text, temporal scope, claim revision, schema and policy version, plus the
  derived claim digest. Unknown, missing or mismatched fields fail closed.
- Confirmation documents live in a read-only directory under a filename derived from the episode
  UUID. Menhir records only bounded proposal/verifier receipts on `:Episodic` and an authorized
  payload on the Graphiti relationship; it stores neither signature nor private key.
- Revocation is immediate for default fact-edge recall: deleting or changing the confirmation makes
  the edge fail fresh verification. Structural canonical identity remains separate.
- `off` and `observe` preserve legacy writer/reader behavior. In `enforce`, unconfirmed Graphiti
  edges remain proposals, typed-scalar/event self writes are proposal-only, direct self-anchored
  View writes are rejected, and legacy self-derived canonical nodes, Views, scalars and event
  authority are excluded from default recall.

## Implementation Sequence

| Phase | Work | Exit evidence |
|---|---|---|
| 0. Read-only inventory and decision | Read the authorities below; verify local worktree/HEAD; follow structural-query guidance without triggering ingestion writes. Trace every declaration producer, canonical-UUID writer and reader. Specify reusable proposal schema, trusted confirmation mechanism, modes and compatibility. | Owner-approved contract; writer/reader map; exact implementation/test paths; unresolved access gaps recorded. |
| 1. Reproduce before changing behavior | Add the multiline case through final declaration, binding and resolution using a deliberately dangerous injected extraction payload. Add mixed valid/invalid edges, RBAC and unauthorized promotion cases. | Regression fails on the vulnerable baseline for the intended reason. No new quote heuristic. |
| 2. Separate identity from semantic authority | Construct the author endpoint before ordinary extraction/resolution; replace model-selected-node declaration with structural construction. Remove linguistic classification from all authority-producing paths. Enforce assertion authorization per edge at persistence. | Plain text cannot issue a declaration/proof; a trusted positive case still binds; zero self candidate/dedup calls. |
| 3. Wire proposals, confirmation and readers | Extend existing assertion/provenance contracts. Gate scalar/event/direct writers as well as Graphiti. Enforce proposal isolation in recall, summaries, context composition and derived views; add idempotent confirmation and invalidation. | No unverified self fact reaches authoritative storage or recall through another lane. No model-accessible approval shortcut. |
| 4. Prove lifecycle and adversarial behavior | Run focused tests and serial unit suite; after explicit local-test approval, use disposable Docker Neo4j/test Menhir and the real configured extraction model. Test the exact candidate image before any activation request. | Recorded commands, immutable image/model configuration, deterministic boundary results, real-model matrix and fresh independent review. |
| 5. Documentation and handoff | Correct guarantee claims and mode descriptions; record residuals and exact tested revision. Stop before production actions. | No unresolved authority bypass; implementation report separates passed, failed and unrun checks; owner receives a separate activation decision. |

Implement on a new feature branch derived from the plan commit, not in the reviewed worktree.
Suggested name: `feat/canonical-self-authority-boundary-20260905`. Use small commits by phase.
Phases 2 and 3 must not become independently deployable partial enforcement with unguarded writers.

### Initial file map

- `src/menhir/domain/self_identity.py`: evidence and declaration contracts; retain the UUID formula.
- `src/menhir/infrastructure/graphiti_extraction_patches.py`: endpoint construction/transport,
  final declaration and repair; remove classifier-driven authority.
- `src/menhir/infrastructure/self_binding.py` and `graphiti_model_patches.py` in the same directory:
  atomic rewrite, resolver partition, candidate isolation and persistence preservation.
- `src/menhir/domain/merge_eligibility.py` and `src/menhir/infrastructure/correlation_queries.py`:
  preserve merge immunity and mutation-time checks.
- Existing admission/assertion, scalar/event, lifecycle and recall modules: enumerate actual paths
  in Phase 0. Do not assume the Graphiti-only file list covers product-wide attachment authority.

## Invariants

Identity authority cannot increase because of any natural-language or model-output change.
Authorship alone never authorizes a semantic assertion. Proof for one edge cannot authorize other
edges on its node. Retry/repair cannot strengthen evidence; cross-turn/namespace reuse refuses.

Retain logical-to-physical namespace mapping, atomic node/name/UUID/edge-text/both-directions/index
rewrite with rollback, exclusion from every exact/similarity/LLM/override dedup path, canonical
property preservation, and merge immunity in either direction. Driver errors are not absence.
Concurrent first writes and bulk/replay must preserve actual database uniqueness, not just derive
the same UUID; verify the constraint/transaction behavior in the disposable database.

`off` preserves legacy behavior and promises no new protection. `observe` never rewrites, promotes,
alters extraction prompts, or claims measured enforcement yield. `enforce` applies the complete
approved contract. Existing Graphiti mode must not be advertised as covering other writers until
those writers are actually gated. Record the rollout configuration explicitly; default remains off.

## Validation

Extend `tests/test_graphiti_combined_extraction_closure.py`, `tests/test_self_binding.py`,
`tests/test_self_resolver_bypass.py`, `tests/test_canonical_self_endpoint_e2e.py`, and the existing
identity, producer-census, merge, scalar/event and recall tests identified in Phase 0.

| Case | Required result under the proposed hybrid |
|---|---|
| Exact owner-confirmed structured assertion | Correct canonical endpoint and proposition; no self search; normal ordinary-object resolution remains allowed |
| Plain `I own 37 postcards`, no confirmation | Evidence/proposal retained; no authoritative ownership edge or substitute durable self |
| Multiline/quoted/reported speech; mixed speakers | Dangerous injected self edge rejected even when the model emits the correct transport handle; other edge proof cannot authorize it |
| RBAC/application/database/customer `user` | Ordinary identity preserved; canonical candidate isolation holds |
| Questions, negation, hypothetical statements | No affirmative self fact; confirmation digest cannot be reused after polarity changes |
| Replayed, mismatched, stale or forged evidence/approval | No stronger authority, duplicate promotion, cross-scope attachment or partial rewrite |
| Repair, bulk, concurrent writes, existing canonical data | Unique canonical storage, complete rollback, preserved properties and merge refusal in both directions |
| Scalar/event/direct writers; summary/recall/view readers | No alternate attachment or silent promotion; proposal status survives every output boundary |

Use deterministic injected-payload tests for the trust boundary; real-model success alone is not a
proof. Real-model E2E must assert the exact proposition, direction, polarity and absence of forbidden
attachments, not only `READY` or `relation_count >= 1`. Include replay and multiple adversarial cases.
Verify no reserved marker leaks into persisted node/relationship properties.

Run `menhir artifacts validate . --repository menhir` and `pytest tests/ -m unit -q` serially in the
implementation checkout. Run live tests only after approval with dedicated disposable storage;
never inherit production database URLs, volumes or credentials. Record skipped/missing prerequisites
as unrun, not pass. The handoff's 166/95 passing counts are prior evidence, not this plan's results.

## Risks

Automatic personal recall coverage decreases until confirmation exists. Proposal labels can be
lost in summaries, caches or alternate readers. An agent-callable confirmation surface would simply
move the same authority bug upstream. New schema/policy combinations can make rollback unsafe:
rehearse app/config/data compatibility; do not assume flipping `off` or deploying older code is safe.
Scope control matters: add only the self-attribution protections, not a second memory architecture.

## Docs To Update

Update `.agent/architecture.md`, relevant `.agent/data_models.md` / `.agent/endpoints.md` sections
only if contracts change, `.agent/workflows/canonical-self-migration-runbook.md`, this plan/index,
the applicable release plan and changelog. Preserve historical entries as history; current docs
must distinguish structural identity, confirmed attribution, unverified proposals and world truth.

## Authorities and Remaining Approval Gates

Read locally before implementation (workspace documents were not available to the prior remote review):

- `C:\Users\thron\IdeaProjects\.agent\plans\menhir-canonical-self-remediation-plan.md`
- `C:\Users\thron\IdeaProjects\.agent\plans\menhir-canonical-self-subject-endpoint-design-2026-09-04.md`
- `C:\Users\thron\IdeaProjects\.agent\for-review\WRAPUP-2026-09-04-menhir-canonical-self-exact-node-resolver.md`
- Repository `.agent/workflows/canonical-self-migration-runbook.md`, `AGENTS.md`, artifact-authoring
  and feature-planning workflows; inspect existing governance/assertion contracts in Phase 0.

Conflicts require an explicit decision, not silently treating this proposal as superseding approval.
Production activation requires a separate owner-approved release with normal independent review,
backup, exact-image/config evidence and rollback controls. Historical-fork work remains a separate
approved, journaled, reversible migration beginning with a fresh read-only census after prevention
is proven. This plan does not authorize either operation.

Implementation verification is recorded in the review wrapup. Focused local unit suites pass on
the implementation branch. Repository artifact validation and the full serial unit suite remain
required before this plan is marked implemented. Docker, live-provider, deployment, production
activation and historical migration checks remain intentionally unrun without separate approval.
