---
artifact_schema: 1
artifact_uuid: 066a19ff-3252-40e9-9435-7a5c9de44c79
artifact_type: investigation
artifact_status: COMPLETE
informs: fa523efe-1bf6-42ed-ac0b-8aab187f9423
---

# Conversation Capture, Temporal Context, and Secret Handling for Menhir

**Discussion follow-up — 30 October 2026**  
**Status:** Post-MVP research proposal; no implementation, capture-policy change, or production activation.  
**Companion:** [Heuristic Memory Detection and Recall](menhir-heuristic-memory-research-2026-10-30.md).  
**Source baseline:** [`Archolith/menhir` main at `0e07898361fb0d6bebf36389c7c02f9aede9cea5`](https://github.com/Archolith/menhir/tree/0e07898361fb0d6bebf36389c7c02f9aede9cea5).  
**Reference use:** Preserves the follow-up discussion and review qualifications for future post-MVP planning. No active implementation consumer or implementation authority. `COMPLETE` describes this investigation record, not the proposed capability.

## 1. Architectural direction

The discussion suggests separating conversation capture from the decision to create useful memory. A turn that looks unimportant when it arrives may later explain a decision, correction, or failed approach. Capture-time triage permanently limits what retrospective analysis can discover.

The proposed direction is **complete turn capture within explicitly enabled conversations, subject to secret handling and a bounded retention policy, followed by selective processing and selective recall**. This is a design preference to investigate, not an approved change to the current producer contract.

“Complete” means accounting for every turn in the enabled source stream. It does not imply permanent verbatim storage, access to every host conversation, capture of hidden model reasoning, or collection of credentials. A redacted, omitted, oversized, unavailable, or expired turn needs an explicit representation of that limitation so downstream analysis does not mistake a partial sequence for a complete transcript.

There are three independent decisions:

| Stage | Question | Proposed behavior |
|---|---|---|
| Capture | What authorized evidence remains available for later analysis? | Preserve permitted conversation turns, their identity, order, provenance, and capture limitations. Apply secret policy first. |
| Process | Which evidence deserves extraction or deeper investigation? | Use cheap signals, bounded context, event-triggered correlation, and selective model calls. |
| Recall | What helps answer this query and is eligible to be shown? | Retrieve relevant sources and governed memory under existing scope, authority, and currentness rules. |

For example, “actually, keep the existing queue” becomes more useful when the preceding alternatives and objections remain accessible. That does not require every preceding turn to become a durable fact or receive an immediate LLM pass.

The original research recommends starting with already-retained evidence. That remains a useful low-risk experiment. Broader capture is a separate foundation decision that could proceed only through an explicit producer-contract and data-policy change.

## 2. What the inspected code establishes

These are repository findings at the pinned baseline, not claims about active production settings or a full dependency audit.

| Area | Finding | Practical limit |
|---|---|---|
| Turn capture | The shared hook uses deterministic triage and strips known harness-injected blocks before capture. [S1] | It is not a complete conversation recorder. Current producer exclusions remain in effect. |
| Evidence persistence | `record_turn_evidence` persists supplied text, capped at 8,000 characters; prompt length/hash use the supplied text, and free-form metadata is serialized as JSON. [S2] | A sequence may have truncated content. This method does not perform secret detection before storage. |
| Extraction context | The extraction policy accepts Graphiti previous episodes and supports bounded adjacent transcript context in corrective extraction. [S3] | Existing conversational context use is not evidence of a general transition detector or complete capture. |
| Episode succession | The schema recognizes `NEXT_EPISODE`. [S4] | Its actual creation and coverage in the Graphiti dependency were not verified in this discussion. Do not claim every episode has a chronological edge. |
| Recall adjacency | `ADJACENCY_EDGE_TYPES` contains only `RELATES_TO` and `MENTIONS`; `NEXT_EPISODE` is explicitly excluded by the August 23 owner ruling. [S5, S6] | Temporal succession does not earn a semantic adjacency ranking bonus. Consecutive turns can concern unrelated subjects. |
| Source-memory selection | Session-spread selection diversifies a cosine-ordered source pool across sessions. [S7] | It is not temporal-neighbor expansion or reconstruction of a conversation. |
| Display privacy | Privacy mode masks memory fields for human-facing views; log-line masking also recognizes quoted credential assignments. [S8] | Display masking leaves graph content unchanged; log masking is heuristic and is not general input sanitization. |
| File ingest | Path guards restrict roots and deny sensitive path patterns such as dotfiles, `.git`, logs, and backups. [S9] | A pasted secret in an otherwise permitted conversation or document is a different input surface. |

**Secret-handling conclusion:** protective controls exist, but this inspection did not find a general conversation-secret scrubber. In particular, the inspected turn-evidence writer stores text without such a step. Existing privacy mode cannot be treated as protection against retaining a pasted API key.

## 3. Temporal context before semantic transition detection

A cheaper baseline should precede claims that explicit transition detection is necessary: retrieve a relevant source, then inspect a bounded set of neighboring turns from the same conversation and authorized scope.

This is a separate operation from adding a ranking bonus for time adjacency. The existing exclusion of `NEXT_EPISODE` from semantic adjacency should remain intact during the experiment. Temporal neighbors supply context to inspect; they do not prove shared topic, causation, agreement, or authority.

The experimental context window should:

- Require exact conversation identity and namespace; enforce any available project/access boundaries.
- Preserve host turn order separately from event time and ingestion time. Late imports and asynchronous tool replies must not silently reorder the dialogue.
- Bound predecessor/successor counts, total bytes/tokens, and elapsed-time range. Document what happens when a limit cuts off relevant rationale.
- Expose truncation, omitted turns, capture gaps, redaction, and expiry; abstain when a conclusion depends on missing evidence.
- Preserve each source role and origin. A quoted vendor instruction or assistant summary remains that source's statement.
- Avoid interpreting adjacent turns as independent corroboration or automatically carrying a fact across a topic change.

Stable source conversation/turn identifiers and retry-safe ordering need a contract before relying on a chain. Decide whether chronological links should be materialized or reconstructed from indexed sequence metadata after inspecting actual writers and readers. A new edge type is not required merely to express this experiment.

## 4. Proposed capture and processing boundary

```text
Explicitly enabled conversation source
    -> normalize identity, role, order, and source metadata
    -> secret policy before persistence or external processing
    -> sanitized source evidence + capture/omission receipt
    -> selective extraction and bounded temporal-context expansion
    -> provisional transition findings
    -> existing admission / assertion / artifact / View route, when qualified
    -> scoped recall with source citations
```

The evidence record should account for source client, namespace, conversation ID, source turn ID, role, sequence/order, observed time, optional event time, truncation/redaction status, and retention policy. This is an inventory of requirements, not a new parallel schema: first reconcile it with TurnEvidence, episodes, their existing admission joins, and erasure machinery.

User turns, assistant turns, and tool results require distinct handling. Capture permission does not grant a source factual authority. Assistant restatements cannot independently corroborate their own inputs. Tool output can establish an observed result only under a defined event-specific authority rule. Treat conversation content as untrusted data during extraction, including instructions embedded in retrieved sources.

Processing should be asynchronous and bounded. Ordinary capture can use inexpensive validation and deterministic signals; a decisive event can trigger a bounded backward look. Avoid processing the entire conversation with an LLM on every new turn.

## 5. Secret handling is a prerequisite for broader capture

Before expanding capture, define a shared policy at the ingestion boundary, with producer-side filtering as defense in depth. Cover live capture, imports/replays, direct API/MCP writes, and attachments in the enabled scope. Sanitization must happen before raw content reaches durable queues, graph records, extraction models, embeddings, telemetry, or error logs.

The first design should address:

1. **Detection and disposition.** Test recognizable credentials, private-key blocks, authorization headers, connection strings, and labeled password/token assignments. Use typed placeholders such as `[REDACTED_API_KEY]` where context can safely survive. Define whether uncertain or unsupported inputs are rejected or held locally without durable raw persistence. Scanner failure must not silently admit unchecked raw content.
2. **Meaningful limits.** Pattern scanning misses unlabeled or unusual secrets and can redact harmless examples. Measure both false negatives and false positives. Do not promise secret-free storage from regex checks alone.
3. **Metadata and secondary copies.** Apply the policy to source metadata and error paths as well as turn text. A credential in a URL or a metadata value must not bypass the text scrubber. Prefer reason codes and source pointers over diagnostic copies of content.
4. **Identity and integrity.** Specify how redaction affects source hashes, deduplication, retries, and exact-span citations. A hash of sanitized text must be labeled as such; do not imply byte-for-byte original integrity or retain raw secret fingerprints casually. Placeholder length changes need sanitized-text offsets or an explicit safe mapping.
5. **Failure behavior.** Capture failures must not interrupt the host conversation. Record safe gap/status information for retry or abstention; retries must not spool raw secrets to an unprotected file.

No credential vault, raw-secret retention exception, or encryption scheme is proposed here. Such a requirement would need its own concrete use case and design. Already-retained secrets also require an explicit cleanup procedure; adding an input scanner does not remove historical copies or revoke exposed credentials.

## 6. Retention, erasure, and provisional findings

Raw evidence retention, protection of admitted memory, and recall visibility need independent policies. Choose a documented finite raw-evidence retention window before a capture experiment; the discussion did not settle its duration. Expired evidence cannot be rediscovered, and that limitation must remain visible.

Findings and negative receipts need a data policy too. A rejected finding containing exact source spans can become another sensitive-content store. Prefer pointers, minimal reason codes, and bounded diagnostic retention. Propagate source erasure or loss of authorization to derived findings, projections, indexes, caches, and replay artifacts under the existing erasure contract. Document backup handling and any interval before deletion becomes effective.

Deleting a source must not leave a derived assertion appearing fully supported. Current/historical projections should be invalidated, repaired, or marked unsupported as appropriate. Sanitized evidence still needs access controls: removing a token does not make the surrounding conversation public.

## 7. Qualifications from the document review

The original proposal is a sound research starting point. Before converting it to an executable plan:

- **Measure capture coverage separately from detector quality.** High precision on retained evidence can hide missing decisions in discarded turns. Use synthetic or explicitly authorized complete-stream fixtures to measure capture losses without broadening production collection.
- **Make shadow answer evaluation concrete.** Run findings in an isolated benchmark context packet with source pointers and provisional labels. Keep authoritative production state unchanged. Independently adjudicate answer support and uncertainty.
- **Narrow the first taxonomy.** Begin with explicit project decisions and explicit reversals. Incident detection and verified resolution require separate evidence rules; a claim of “fixed” alone is not a passing test result.
- **Treat 150 sequences as a pilot.** Use them to discover failure modes and estimate feasibility. A small set spread across many slices cannot establish rare-error safety or release readiness. Report uncertainty and choose a larger held-out evaluation when warranted.
- **Separate naming for priority and delivery stage.** Use priority labels for importance and Stage A/B/C for execution order, avoiding the original document's overloaded P0–P2 terminology.

The antivirus analogy supplies inspiration. Concrete mechanisms and measured answer improvements must determine whether any broader subsystem is justified.

## 8. Proposed post-MVP experiment sequence

### Stage A — Capture accounting and secret policy

Define the enabled source boundary, role/sequence contract, retention window, omission semantics, secret policy, and erasure behavior. Use synthetic credentials and test fixtures, never real credentials. Verify sanitization before every persistence and external-processing boundary, including failure/retry paths. Inventory the Graphiti chronological-edge writer rather than assuming it is complete.

### Stage B — Temporal-context baseline

On a frozen authorized corpus, compare current recall with bounded neighboring-turn expansion. Measure answer correctness with citations, evidence coverage, wrong-conversation/project leakage, unsupported claims, tokens, latency, and cost. Keep the existing semantic adjacency allowlist unchanged.

### Stage C — Incremental value of transition detection

Compare three arms under documented equivalent budgets:

| Arm | Behavior |
|---|---|
| A | Current configured Menhir recall, including the source-memory lane when enabled in the frozen baseline. |
| B | A plus bounded temporal-neighbor context. |
| C | B plus provisional explicit-decision/reversal findings in the benchmark context packet. |

Hold source corpus, graph state, commit, model/embedding versions, query set, and retrieval settings fixed. Record added compute/review costs and use a separate held-out set. Report paired fixed/broken answers, abstentions, detection errors, and uncertainty intervals. Evaluate capture-policy variants separately; do not attribute gains from newly available source text solely to a better detector.

If B closes most of the gap, keep the simpler mechanism. C earns further work only if it improves source-supported answers beyond B at an acceptable documented cost, without breaking scope, provenance, currentness, or erasure invariants. Set numerical thresholds and cost ceilings before the held-out run; they were not agreed in this discussion.

### Stage D — Owner decision

Only after positive evidence should an executable plan propose a reversible canary or broader capture activation. Implementation, production collection, and activation remain outside this document's authorization. This work remains outside MVP launch gates.

## 9. Open decisions

1. Which host conversations and roles can be explicitly enabled, and how is that choice exposed?
2. What finite retention window balances retrospective usefulness and data minimization?
3. How should ambiguous secret detections and scanner outages be represented without retaining raw content?
4. Which stable host identifiers and sequence fields support late arrival, retry, and conversation reconstruction?
5. What temporal-window budget preserves useful rationale across topic changes?
6. Does temporal expansion alone outperform current recall, and does transition detection add enough beyond it?
7. Which existing writer/read contracts should own admitted decisions and reversals?

## Sources

All code links are pinned to the inspected baseline. No detector, secret scanner, complete-capture producer, or benchmark was implemented for this follow-up.

- **[S1]** [Shared turn-evidence hook](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/scripts/hooks/menhir_turn_evidence_common.py) and [producer contract](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/docs/turn-evidence-producers.md).
- **[S2]** [TurnEvidence repository and `record_turn_evidence`](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/infrastructure/turn_evidence_repository.py#L215).
- **[S3]** [Extraction policy: previous episodes and adjacent-context repair](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/infrastructure/graphiti_extraction_policy.py#L120).
- **[S4]** [Schema relationship vocabulary](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/infrastructure/schema.py#L11).
- **[S5]** [Recall adjacency allowlist](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/domain/recall.py#L56).
- **[S6]** [CF-247 owner ruling and regression tests](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/tests/test_cf247_adjacency_edge_types.py#L19).
- **[S7]** [Source-memory session spreading](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/domain/source_memory_spread.py).
- **[S8]** [Display and log privacy implementation](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/privacy.py) and [security posture](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/docs/security-posture.md).
- **[S9]** [Filesystem ingest path guard](https://github.com/Archolith/menhir/blob/0e07898361fb0d6bebf36389c7c02f9aede9cea5/src/menhir/core/ingest_guard.py).
