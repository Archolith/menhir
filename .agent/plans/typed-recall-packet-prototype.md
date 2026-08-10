# Typed Recall Packet Prototype

Status: **structured scalar/event packet retained; recall-side tentative-intent inference removed**

> **Boundary decision (2026-08-08): tentative typing is ingest-owned.** `_TENTATIVE_MODALITY` and
> all recall-side phrase classification have been removed. Tentative/plan authority will be
> admitted during ingest and materialized as an Intent State View under
> [`menhir-intent-state-view-2026-08-08.md`](menhir-intent-state-view-2026-08-08.md). Until that path
> exists, ordinary retrieved prose is General Content. Scalar/event packet presentation remains an
> inspection prototype, but no further packet-quality evaluation should treat recall-inferred
> tentative labels as valid evidence.

## Why

- The current LLM-facing context preserves scalar and event authority, but then presents recalled
  memories as a flat numbered list. That makes kind, authority, world time, and provenance harder
  for a consuming model to distinguish.
- Recall Lab already projects scalar state, scalar history, event history, assertions, evidence,
  and relationship content. A deterministic presentation prototype can make those roles legible
  before any production recall contract is changed.

## Scope

- Build a pure, deterministic formatter over the existing Recall Lab live-graph projection.
- Group entries into authoritative current state, advisory change history, completed events, and
  general content. Add admitted intent state only after the ingest-owned View exists.
- Preserve source/world time, learned time, derivation, grounding quotes, and source identities
  whenever the projection has them.
- Render both a compact prompt-oriented form and a structured form in a new Recall Packet tab.
- Explicitly mark the packet as an inspection-only prototype.
- Out of scope: production context changes, recall ranking changes, graph/schema changes, ingestion,
  new model calls, re-ingestion, or benchmark-specific rules.

## Proposed Design

- Add an Explorer-local pure formatter that accepts the existing live-graph dictionary and returns
  a versioned typed packet plus compact text.
- Treat current `scalar_state` Views as authoritative current state, `scalar_history` Views as
  advisory change history, and `event_history` Views/assertions as completed event evidence.
- Render admitted `intent_state` Views as advisory intent when that ingest-owned feature exists.
  Ordinary relationship content always remains General Content; this formatter performs no
  tentative phrase classification.
- Use source assertion/evidence links for grounding. Do not infer missing timestamps or source IDs.
- Add the packet to the existing Recall Lab task response and display it in a separate tab.

## Alternatives Considered

- Change `ContextBuilderService` immediately: rejected for this pass because it would alter the
  production prompt before the format is inspectable and tested.
- Store a new packet node in Neo4j: rejected because the packet is a disposable presentation over
  existing durable facts and Views.
- Let an LLM classify every memory: rejected because category and authority routing should be
  stable, cheap, auditable, and fail closed.

## Risks

- Natural-language modality is open-ended and is not classified by this formatter. All untyped
  content remains general context.
- The live projection may lack learned time or a direct evidence link for relationship facts. Missing
  values remain explicit; the formatter must not invent them.
- Event assertions and event Views can describe the same occurrence. The compact output should
  prefer View entries and use assertions as grounding/fallback rather than duplicate claims.
- Redaction must continue to hide quotes and content when Recall Lab reveal mode is off.

## Invariants

- Production recall output is byte-for-byte unaffected because the prototype is Explorer-only.
- Scalar state remains the sole current-state authority; history is advisory; events never imply
  current ownership or state; untyped content has no scalar, event, or intent authority.
- `valid_at` is world time and `learned_at` is ingest/belief time. The latter never substitutes for
  the former.
- No task IDs, gold answers, or benchmark-specific wording influence classification.
- Existing namespace isolation and Reveal/Hide privacy behavior remain intact.

## Validation

- Unit tests for current scalar, absolute/delta history, completed event, general content, missing
  metadata, event deduplication, and redaction compatibility.
- Recall Lab response/template tests for the new tab and inspection-only label.
- Focused Explorer regression suite.
- Manual browser check on representative scalar/event/content benchmark tasks.

## Docs To Update

- `CHANGELOG.md`
- This prototype plan is the design record; architecture/data-model/endpoints remain unchanged
  because this pass adds no production contract, schema, or route.

## Query-Filtered Follow-up

The complete inspection packet is useful to operators but is not an appropriate answer-model
context: it sends every projected relationship, including unrelated General Content. A full KU78
recall-only experiment confirmed that boundary error (60/78 versus the canonical 68/78, with 3.4x
the answer-input tokens).

The follow-up keeps production retrieval as the evidence selector and applies typing only after
selection:

1. Run the existing production recall arm read-only for the task namespace and question.
2. Match retrieved View UUIDs to the full inspection projection so selected scalar state retains
   authoritative value, derivation, time, quote, and source identity.
3. Add scalar history only for generic history/change intent and completed events only for generic
   event/time intent. Long shared evidence quotes never make unrelated scalar Views relevant.
4. Keep remaining ranked results as general context, preserve their rank and bounded temporal facts,
   and omit the unranked full memory inventory.
5. Enforce a deterministic 6,000-character packet budget, at most four general memories, bounded
   quotes/content, and stable ordering.

Retrieval and authority verdict UUIDs are the normal production selection boundary. The formatter
does not use benchmark phrases, gold answers, or domain vocabularies to add evidence. A small
identity-token fallback exists only for archived inputs that contain no durable IDs at all. Ranked
governance policies, architecture decisions, and coding facts therefore follow the same contract as
personal-memory content: their original memory type, rank, text, and temporal metadata are preserved
without special-case routing.

The endpoint is read-only and does not persist Recall Lab runs, update access timestamps, alter
ranking, mutate the graph, or call an answer/judge model. A local 78-task composition audit using
the saved canonical recall contexts reduced mean context from 4,233 to 1,717 characters, capped at
5,010 characters, with no budget violations. This is composition evidence, not a quality score;
an external focused answer/judge evaluation still requires a separately authorized call budget.
