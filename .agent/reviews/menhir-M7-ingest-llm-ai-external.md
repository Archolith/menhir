# Menhir M7 Ingest LLM & AI Security Audit (A7)

**Repository:** `Archolith/menhir`  
**Branch:** `main`  
**Audited commit:** `db31ebaef0bb9bd5a650429f7b850e28087904a5`  
**Scope:** exactly the eight requested files under `src/menhir/services/`  
**Method:** static code reading only; no execution, shell, filesystem probe, network security tooling, or dynamic prompt testing  

The two procedure references in the original brief were explicitly withdrawn. This review does **not** use `.agent/audit/PROBE-PROTOCOL.md` and does not substitute any missing LLM-audit procedure.

`wc -l`: **NOT RUN — no execution environment.** I independently reconciled line counts with line-addressed repository reads pinned to `db31eba`, reading each file through its actual EOF. The measured total is **3,122 lines**, matching the corrected brief.

Runtime initialization defaults for `_enrichment_enabled`, `_graphiti_episode_max_estimated_tokens`, `_max_enrichment_attempts`, `_context_window_retry_attempts`, `_budget_settings_max_calls`, `_budget_settings_window_s`, and `_budget_settings_max_per_job` are not defined in these eight files. I do not infer them from comments or from out-of-scope files. Findings that traverse enrichment therefore state when the gate is conditional and identify an unavailable default rather than inventing one.

## Executive Summary

The audited ingest path has **one High, two Medium, and one Low finding; no Critical finding was established from the scoped code**.

The highest-value result is a complete, code-proven **LLM-output-to-persistent-memory path**: once Graphiti returns an edge with a synthetic fact, M7 passes the raw composed episode back to `ctx.llm.repair_edge_facts()`, accepts any truthy returned fact, and writes it directly through `update_edge_facts()` with no independent semantic or entailment check. The `fact_source="llm_repaired"` marker is useful provenance, but it does not gate the write. This is the ingest-side persistence half needed to connect an instruction-bearing episode to durable memory without re-filing the already-confirmed downstream findings.

Separately, there is **no content/instruction boundary before Graphiti** in these eight files. The user-tier admission gate changes source provenance, not the episode text. The same `episode` string is persisted; `compose_episode_body()` adds an optional diff with a plain text delimiter; preflight is size-only; and the resulting body is passed to `graphiti_client.add_episode()`. The supplied audit context already establishes that Graphiti interpolates episode content into its extraction prompt, so this report does not re-file that downstream interpolation.

The LLM budget controls are also weaker than their names imply. Session accounting adds one timestamp per enrichment attempt, not per observed LLM call. The per-job count is checked before extraction while a fresh attempt defaults to zero; later `started` usage events can exceed the configured limit but only cause a warning, and the counter is deleted in `finally`. Thus an already-running enrichment job is not hard-stopped by either call budget.

On Lane C, ingest does preserve useful trust metadata: the raw episode is stored with `source` and `source_confidence`, extracted nodes/edges are stamped with source metadata, and repaired edge facts are labeled `llm_repaired`. However, the supporting turn-evidence relationship is best-effort and can be absent while the episode remains durable. The supplied confirmed recall-side result (CF-39) means provenance marking should not be confused with a content barrier: a planted payload can still matter if it reaches recall.

## Findings

### High

#### L-1 — Prompt-influenced LLM edge repair is written directly to persistent graph state

**Severity: High**

**Trigger / payload shape.** An enrichment job reaches this path when all of the following are true:

1. enrichment is enabled and the episode reaches Graphiti;
2. Graphiti returns at least one extracted edge;
3. at least one returned edge has a `fact` beginning with `SYNTHETIC_FACT_PREFIX`; and
4. `ctx.llm.repair_edge_facts(episode_content, stubs)` returns a truthy string for that edge.

The attacker-relevant content shape is an instruction-bearing `episode` or attached `diff` that survives the size preflight and is present in `episode_content`; dynamic execution would be required to prove a particular string deterministically induces a chosen repair result, and this report does not claim such a reproduction.

**Reachability chain.** `queue_episode_for_enrichment()` stores the caller's raw `episode` as `content` (`src/menhir/services/ingest_intake.py:126-143`) and queues it only when `_enrichment_enabled` is true (`src/menhir/services/ingest_intake.py:244-251`). The initialization default of `_enrichment_enabled` is **not defined in the scoped corpus**. `compose_episode_body()` returns that content verbatim when there is no diff and otherwise appends the diff after a plain `--- git diff ---` marker (`src/menhir/services/enrichment_steps.py:160-172`). After Graphiti extraction, `stamp_and_finalize()` invokes `_repair_synthetic_edge_facts()` with the composed episode body (`src/menhir/services/enrichment_steps.py:1104-1107`).

Inside the repair helper, the gate is explicit: `ctx.llm` must be non-`None`, extracted edges must exist, and the edge fact must begin with `SYNTHETIC_FACT_PREFIX` (`src/menhir/services/enrichment_steps.py:754-772`). The helper then calls `ctx.llm.repair_edge_facts(episode_content, stubs)` and, for any truthy response, constructs an update containing the returned text as `fact` with `fact_source="llm_repaired"`; the updates are then written with `ctx.graph_adapter.update_edge_facts(updates)` (`src/menhir/services/enrichment_steps.py:791-822`).

**Independent check.** There is no schema, length, entailment, source-grounding, second-model, deterministic, or human validation of `repaired_fact` between the LLM response and the graph update in this path. The only acceptance condition is truthiness. The `fact_source="llm_repaired"` field is a provenance marker, not a write gate.

**Effect if reached.** Model output influenced by the ingested episode becomes a stored edge assertion. The episode is subsequently eligible to be marked READY (`src/menhir/services/enrichment_steps.py:1194-1200`). This meets the supplied High calibration: **model output reaches a trusted sink unvalidated**.

**Relationship to confirmed findings.** This does not re-file CF-4, CF-69, CF-39, the supplied `infrastructure/llm.py:98` interpolation, or Graphiti's `extract_nodes.py:127` interpolation. It establishes the M7-side persistence sink: raw episode context is supplied to an LLM repair step and the returned assertion is written directly.

**Recommended control.** Treat edge repair output as untrusted model output. Before `update_edge_facts()`, require a deterministic grounding/entailment check against the source episode (or a separately isolated verifier whose prompt does not inherit untrusted instructions), enforce an output schema and bounded fact length, and preserve `llm_repaired` provenance. If validation fails, retain the synthetic fallback or quarantine for review rather than writing the proposed fact.

### Medium

#### L-2 — M7 has no instruction/data boundary before the Graphiti prompt boundary

**Severity: Medium**

**Trigger / payload shape.** Any non-empty `episode` containing instruction-shaped text, or a `diff` containing instruction-shaped text, reaches this condition when enrichment is enabled and the composed body passes the configured size preflight. No special delimiter-breaking syntax is required. If `max_estimated_tokens` is configured as `0`, the preflight is disabled entirely (`src/menhir/services/enrichment_steps.py:214-226`). The initialization default of that setting is **not defined in the scoped corpus**.

**Reachability chain.** For `source="user"` or `source="manual"`, `queue_episode_for_enrichment()` calls `evaluate_user_tier_claim(..., claimed_text=episode, ...)` and uses only the returned `effective_source`; on gate evaluation error it downgrades to `agent_inference` (`src/menhir/services/ingest_intake.py:99-124`). The raw `episode` itself is then persisted unchanged as `content=episode` (`src/menhir/services/ingest_intake.py:126-143`). Other source values skip that user-tier branch entirely.

`compose_episode_body()` performs no escaping or instruction/data structural separation. It preserves `content` verbatim and, if a diff exists, appends up to `50,000` diff characters under a plain text delimiter (`src/menhir/services/enrichment_steps.py:154-172`). `build_episode_preflight_rejection()` estimates tokens from character count and rejects only on size; a zero limit disables it (`src/menhir/services/enrichment_steps.py:214-243`). `run_preflight_rejection()` applies that size check to the same composed body (`src/menhir/services/enrichment_steps.py:361-368`). If it passes, `run_graphiti_extraction()` passes `compose_episode_body(ctx.claimed)` as `episode_body` (`src/menhir/services/enrichment_steps.py:620-632`), and `add_episode_with_timeout()` forwards that string unchanged into `graphiti_client.add_episode()` (`src/menhir/services/enrichment_steps.py:1408-1418`).

**Gate/default.** The actual enrichment queue gate is `_enrichment_enabled` (`src/menhir/services/ingest_intake.py:244-251`); its initialization default is not present in the eight-file corpus. The `IngestGate` class is not a trust gate: it is a concurrency/per-namespace serialization primitive with constructor default `concurrency=1` (`src/menhir/services/ingest_gate.py:29-56`).

**Effect if reached.** Instruction text and factual data occupy the same episode string at the downstream Graphiti LLM boundary. The supplied audit context already confirms unescaped Graphiti prompt interpolation, so the ingest-side negative is definitive: **M7 does not create a semantic boundary before handing content to that prompt**.

This is Medium under the supplied calibration because the content is **bound only by convention**. This finding does not claim a new action-taking exploit or re-file the already-confirmed downstream injection findings.

**Recommended control.** Introduce an explicit untrusted-data contract at the M7/LLM boundary: structured fields for source text versus operator instructions, a fixed system instruction that says source text is data, robust delimiting/serialization that downstream templates preserve, and a content-independent output validator. Keep size limits as resource controls, but do not treat them as injection defenses.

#### L-3 — LLM call budgets are accounting/backpressure signals, not hard per-call limits

**Severity: Medium**

**Trigger / payload shape.** The exact runtime trigger is an enrichment attempt for which the LLM usage callback emits more `phase == "started"` events than `_budget_settings_max_per_job` before the attempt finishes. Code reading proves the control does not stop those calls. It does **not** prove a particular attacker text deterministically forces a chosen number of calls; that exploitability question remains open without execution.

**Reachability chain.** Before extraction, `_process_pending_episode()` calls `_check_session_budget()` once (`src/menhir/services/ingest_worker.py:164-169`). `_check_session_budget()` maintains a deque keyed by session and, after checking the current length, appends exactly one `now` timestamp for the attempt (`src/menhir/services/ingest_queue.py:235-263`). It does not append once per `LLMUsageEvent`.

The per-job gate then executes **before** `run_graphiti_extraction()`: `job_llm_count = self._job_llm_call_counts.get(episode_uuid, 0)`, so a fresh attempt uses a default count of `0`; only an already-over-limit count would requeue before extraction (`src/menhir/services/ingest_worker.py:189-212`). During the running job, `_record_episode_llm_usage()` increments the counter on `phase == "started"`; exceeding the limit causes only a warning and does not cancel, reject, or backpressure the active call (`src/menhir/services/ingest_worker.py:252-266`). Finally, the attempt unconditionally removes the counter with `self._job_llm_call_counts.pop(episode_uuid, None)` (`src/menhir/services/ingest_worker.py:238-245`).

**Gate/default.** The per-attempt counter's code default is `0`. If `max_llm_calls_per_enrichment_job` is supplied to `configure()`, it is clamped to at least `1` (`src/menhir/services/ingest_queue.py:95-97`); the initialization default of `_budget_settings_max_per_job` is not in scope. Likewise, supplied session-window call limits are clamped to at least `1`, but their initialization defaults are not in scope (`src/menhir/services/ingest_queue.py:84-94`).

**Effect if reached.** An already-running job can exceed the configured per-job LLM call limit, while the rolling session budget still consumes only one deque slot for that entire attempt. Usage is observed and logged, but not enforced as a hard call ceiling. This weakens cost containment and makes content-dependent multi-call behavior more expensive than the budget names imply.

**Recommended control.** Move the hard check into the callback path or the common LLM dispatch layer: atomically reserve a call before dispatch, reject/cancel when the reservation would exceed either the per-job or session limit, and count actual attempted LLM calls in the session window. Keep a separate job-attempt budget if desired, but name and meter it separately.

### Low

#### L-4 — Admission provenance linkage can fail after a user-tier episode is already durable

**Severity: Low**

**Trigger / payload shape.** A caller submits a `user`/`manual` episode that the admission evaluator grants with a non-empty evidence UUID, the pending episode write succeeds, and `graph_adapter.link_episode_admission()` then raises. `turn_evidence_uuid` defaults to `None` at the intake API (`src/menhir/services/ingest_intake.py:64-75`); this path therefore requires a granted/supplied evidence identifier rather than being the no-argument default.

**Reachability chain.** The episode is first persisted with `content=episode`, `source=effective_source`, and `source_confidence=source_confidence_for(effective_source)` (`src/menhir/services/ingest_intake.py:126-143`). Afterward, the code derives `admitted_on_uuid`; for a gated source it uses the verdict UUID only when `verdict.granted` is true. It then attempts `link_episode_admission()`, but catches every exception and continues (`src/menhir/services/ingest_intake.py:171-188`).

**Effect if reached.** The admitted episode remains durable at its effective source/confidence while the supporting evidence relationship is missing. That is a provenance/auditability gap, not a demonstrated privilege escalation: the admission decision itself already occurred before the failed link.

**Recommended control.** For trust tiers whose downstream interpretation depends on durable evidence lineage, either make the supporting link transactional with the episode write or persist an explicit `provenance_link_missing`/unverified-lineage state that downstream consumers can honor. At minimum, make the loss visible above DEBUG and retry the linkage independently.

## Content Boundary Trace

The following trace covers every **confirmed or explicit M7 LLM-boundary handoff** found in the eight-file corpus. Calls into out-of-scope services that merely *might* construct prompts are listed separately as Open Questions rather than asserted as prompt sites.

| Stage | Ingested content handling | Boundary result |
|---|---|---|
| User-tier admission | `claimed_text=episode` is evaluated, but only `effective_source` is consumed; the raw `episode` is later persisted unchanged (`ingest_intake.py:99-143`). | **No content transformation.** Provenance gate only. Filed in L-2. |
| Pending storage | `create_pending_episode(content=episode, source=effective_source, source_confidence=...)` (`ingest_intake.py:126-143`). | Raw payload becomes durable before LLM preflight. |
| Episode composition | Raw content is returned verbatim; optional diff is appended after `--- git diff ---`, with only the diff truncated at 50,000 characters (`enrichment_steps.py:154-172`). | **No escaping / no instruction-data separation.** Filed in L-2. |
| Size preflight | Character-count token estimate; `limit == 0` disables rejection (`enrichment_steps.py:214-243`; application at `:361-368`). | Resource-size control only; not a prompt-injection control. |
| Graphiti extraction | Composed body is passed to `add_episode_with_timeout()` (`enrichment_steps.py:620-632`) and then unchanged to `graphiti_client.add_episode()` (`:1408-1418`). | **Confirmed prompt-boundary handoff** using the supplied Graphiti interpolation fact. Filed in L-2; downstream interpolation not re-filed. |
| Synthetic edge repair | Composed episode is passed directly as `episode_content` to `ctx.llm.repair_edge_facts()`; truthy response is written to edge facts (`enrichment_steps.py:754-822`, invoked at `:1104-1107`). | **Confirmed direct LLM-to-persistence path.** Filed in L-1. |
| Shadow composition | When `shadow_context_composition` is enabled, composed episode is passed with `ctx.llm` into `run_shadow_composition_with_timeout()` (`enrichment_steps.py:721-733`). `EnrichmentContext` defaults this flag to `False` (`:84-102`), and the returned trace is logged via `record_lifecycle_event()` rather than used to mutate the production extraction in this file (`:734-742`). | **Cleared negative for production persistence in M7:** prompt-bearing observe-only path, default off. |
| Project narrative source | `build_project_narrative()` caps the string it builds at 4,000 chars, but `execute_project_ingest()` queues `result["narrative"]` from the backend directly (`project_ingest.py:121-169`). | Whether the production backend always uses the bounded builder is **Open Question**; no universal cap is claimed. |

Additional raw-context handoffs that leave the eight-file scope are not promoted to prompt sites without reading the callee: `CorrelationService(..., llm=ctx.llm).check_correlation(...)` and `lifecycle_service.rehydrate_node(..., new_context=episode_content, ...)` occur after extraction (`enrichment_steps.py:1112-1182`). Their internal prompt construction and mutation rules are outside this audit.

## Injection-to-Persistence Chain

### Proven M7 chain

1. **Entry:** `queue_episode_for_enrichment(episode=...)` accepts the episode as a plain string (`ingest_intake.py:64-75`).
2. **Admission:** for `user`/`manual`, the gate evaluates the claim and chooses `effective_source`; it does not return a sanitized episode (`ingest_intake.py:99-124`).
3. **Durable raw storage:** the original `episode` is stored as `content` (`ingest_intake.py:126-143`).
4. **Enrichment gate:** only `_enrichment_enabled` determines whether the pending row is placed on the enrichment queue; its initialization default is outside this corpus (`ingest_intake.py:244-251`).
5. **Preflight:** the only content-independent guard is estimated size (`enrichment_steps.py:214-243`, `:361-368`).
6. **Primary LLM boundary:** the composed episode reaches `graphiti_client.add_episode()` unchanged (`enrichment_steps.py:620-632`, `:1408-1418`). The supplied audit context establishes the downstream prompt interpolation.
7. **Returned graph objects:** M7 accepts `graphiti_result.nodes`, `.edges`, and `.episodic_edges` (`enrichment_steps.py:919-923`). For non-empty results, it stamps their UUIDs with the episode's session/user/source/source-confidence/namespace metadata (`:1061-1078`). M7 does not perform a per-assertion semantic verification at this point. Whether Graphiti itself performs an independent semantic entailment check is outside the eight-file scope and remains an Open Question.
8. **Direct secondary LLM persistence:** synthetic edge facts are passed, with the raw episode context, to `ctx.llm.repair_edge_facts()`. A truthy returned fact is written directly (`enrichment_steps.py:754-822`). **This is the code-proven unvalidated model-output sink in L-1.**
9. **Final state:** the episode can then be marked READY (`enrichment_steps.py:1194-1200`).

### Checks the chain actually passes

- user-tier **provenance** admission when `source` is `user`/`manual`;
- optional estimated-token preflight;
- processing lease ownership;
- Graphiti result/receipt handling for empty or collapsed extraction;
- metadata stamping;
- optional/best-effort correlation and rehydration after the primary graph result already exists.

### Check that is absent in the proven repair sink

There is no independent source-entailment or instruction-resistance check on `repaired_fact` before `update_edge_facts()`. Truthiness plus the `llm_repaired` provenance label is the entire acceptance rule in M7.

## Admission Gate: Provenance vs. Content

The admission gate constrains **who/what trust tier the episode claims**, not what instructions the text can carry.

For `user`/`manual`, the gate receives the raw episode as `claimed_text` and can downgrade `effective_source`; an evaluation exception is caught and the source is downgraded to `agent_inference` (`ingest_intake.py:99-124`). The subsequent pending write still uses the original `episode` string (`:126-143`). No function in the eight-file corpus scans for instruction-shaped language, escapes model-control tokens, labels a substring as untrusted data, or rewrites the text into a structured data-only representation before Graphiti.

`IngestGate` is unrelated to this question: it is a semaphore plus per-namespace lock, with `concurrency=1` as its constructor default (`ingest_gate.py:29-56`).

## Budget, Retry, and Failure Paths

### Length / token budget

- Diff attachment is capped at **50,000 characters** before composition; the episode content itself is not truncated by `compose_episode_body()` (`enrichment_steps.py:154-172`).
- The composed body is rejected when the estimated token count exceeds `graphiti_episode_max_estimated_tokens`; `0` disables this preflight (`enrichment_steps.py:214-243`). The configured initialization default is outside the scoped files.
- When preflight rejects an oversized episode, M7 also creates a `raw_capture` entity containing the composed body before marking the episode failed (`enrichment_steps.py:369-391`). Whether `raw_capture` is recallable through the confirmed CF-39 path is outside scope and therefore an Open Question, not a finding.

### LLM call budget

L-3 applies. The session window meters one processing attempt, not actual `started` events, and the per-job counter cannot stop in-flight calls because its enforcement check occurs before extraction while over-limit events later only log.

### Retry and failure behavior

`handle_enrichment_failure()` first marks the episode FAILED, then classifies the exception and emits `retryable` as telemetry (`enrichment_steps.py:1261-1345`). In the scoped code, classification itself does **not** requeue the failed episode.

`enrichment_failures.py` routes unknown remote Graphiti timeout messages to `manual_review` before generic timeout markers are considered (`enrichment_failures.py:45-48`, `:74-96`). Combined extraction collapse is a `retryable` marker (`:27-42`), but no content-triggered automatic loop from that classification is implemented in these eight files.

There are explicit requeue mechanisms, but their triggers are different:

- session-budget exhaustion marks an episode pending with a delay (`ingest_worker.py:164-187`);
- circuit-open handling requeues after 30 seconds (`ingest_worker.py:213-229`);
- stale/orphan lease recovery requeues persisted pending work (`ingest_queue.py:188-234`);
- `requeue_failed_episode()` is an explicit callable operation that accepts a FAILED row and enqueues it (`ingest_queue.py:318-331`). The subsequent claim behavior and retry ceilings are implemented in the graph adapter and initialized outside this corpus (`ingest_worker.py:90-106`), so repeated explicit retry amplification is not asserted here.

**Conclusion on crafted-content retry amplification:** not established from the eight files. The budget-control weakness is established; a content-specific automatic re-enrichment loop is not.

## Lane-by-Lane Results

### Lane A — Prompt injection / content boundary

**Result:** L-2 (Medium) and the prompt-boundary trace above. M7 provides size controls and provenance classification but no instruction/data separation before Graphiti. The direct synthetic-edge repair path additionally feeds episode content to an LLM and persists the response (L-1).

- Garak: **NOT RUN — no execution environment.**
- DeepTeam: **NOT RUN — no execution environment.**
- Promptfoo: **NOT RUN — no execution environment.**
- Any dynamic prompt-injection/fuzz scan: **NOT RUN — no execution environment.**

### Lane B — Model output handling, resource budget, and failure behavior

**Result:** L-1 (High) and L-3 (Medium). The synthetic edge repair accepts a truthy LLM response directly into graph state, while call budgets observe usage without hard-stopping an active over-limit job. No content-triggered automatic retry loop was proven in scope.

- Dynamic malformed-output testing: **NOT RUN — no execution environment.**
- Cost-amplification execution/fuzzing: **NOT RUN — no execution environment.**
- Scan report: **NOT RUN — no execution environment.**

### Lane C — Agentic memory poisoning

**Result:** the planting side is real. A prompt-influenced repaired edge fact can become durable memory (L-1), and normal extracted graph objects are stamped with episode provenance before READY finalization (`enrichment_steps.py:1061-1078`, `:1194-1200`). Ingest also stores `source` and `source_confidence` on the raw pending episode (`ingest_intake.py:126-143`), and repaired facts receive a distinct `fact_source="llm_repaired"` marker (`enrichment_steps.py:806-822`).

The supplied confirmed CF-39 result establishes the recall-side premise that recalled memory can render verbatim into an operator agent context; this report does not re-file CF-39. The relevant M7 conclusion is that provenance metadata **exists**, but it is not a content quarantine. The turn-evidence relationship can also be missing after an admitted write (L-4).

- End-to-end poison-then-recall execution: **NOT RUN — no execution environment.**

## Disproved Candidates

These candidates were considered and rejected after reading the surrounding code.

1. **“The user-tier admission gate sanitizes prompt injection.” — Disproved for M7.** The gate consumes the raw episode to decide `effective_source`, then the exact `episode` is persisted (`ingest_intake.py:99-143`). No sanitized value replaces it.
2. **“`IngestGate` is the content/admission gate.” — Disproved.** It is only concurrency plus per-namespace serialization (`ingest_gate.py:29-56`).
3. **“The Graphiti preflight is an injection filter.” — Disproved.** It only estimates size; `limit == 0` disables it (`enrichment_steps.py:214-243`).
4. **“Malformed/timeout model output automatically causes an in-module retry loop.” — Disproved in the eight-file corpus.** Failure handling marks FAILED and emits classification metadata (`enrichment_steps.py:1261-1345`); unknown remote timeouts classify `manual_review` (`enrichment_failures.py:45-48`, `:74-96`). External scheduler behavior is not inferred.
5. **“Candidate review protects the queued ingest path.” — Disproved for this M7 call path.** `CandidateService.approve()` is a separate candidate promotion workflow (`candidate_service.py:34-80`); none of the other seven scoped files routes queued episode enrichment through it. Candidate approval reachability from external transports is outside this audit.
6. **“Shadow composition can poison production memory in this file.” — Disproved for the scoped implementation.** The flag defaults false (`enrichment_steps.py:84-102`); when enabled, the prediction is converted to a trace and logged (`:721-742`), not used by this file to replace the production Graphiti result.

## Open Questions

1. **Runtime defaults outside scope.** What are the actual initialized/default values for `_enrichment_enabled`, the estimated-token ceiling, attempt ceilings, session budget, and per-job LLM budget? This report does not make a default-on claim without them.
2. **Graphiti semantic validation.** The supplied context confirms unescaped prompt interpolation, but this audit did not inspect Graphiti's internal persistence/semantic verification. M7 itself performs no independent per-assertion verification before stamping returned UUIDs (`enrichment_steps.py:1061-1078`). Determine whether Graphiti has a genuinely independent entailment/control check rather than shape sanitation only.
3. **Correlation merge boundary.** M7 constructs `CorrelationService` with `llm=ctx.llm` and calls `check_correlation()` on extracted nodes (`enrichment_steps.py:1112-1145`). Does model output inside that out-of-scope service authorize a graph merge or other trusted mutation, and what deterministic veto exists?
4. **Rehydration boundary.** M7 passes the raw composed episode as `new_context` to `lifecycle_service.rehydrate_node()` for compressed nodes (`enrichment_steps.py:1146-1182`). Does that service place `new_context` into a prompt, and can its model output overwrite durable memory? Lifecycle is explicitly out of scope here.
5. **Relationless repair context.** `run_graphiti_extraction()` can build a loader containing up to two preceding turn-evidence texts and passes it into `add_episode_with_timeout()` (`enrichment_steps.py:601-632`). The use of that loader occurs outside the scoped files. Determine whether prior untrusted turn text can become second-order prompt instructions during relationless repair.
6. **Scheduler metadata egress.** Raw `claimed.content` is placed into `build_episode_parent_metadata()` and passed to `emit_scheduler_task_event()` (`enrichment_steps.py:528-539`). Whether that crosses a network/trust/tenant boundary is outside scope. If it does, assess data minimization and tenant isolation.
7. **Raw-capture recallability.** Oversized preflight-rejected content is copied into a `raw_capture` graph entity (`enrichment_steps.py:369-391`). Is that entity excluded from normal recall and CF-39 rendering? If not, a payload rejected from the LLM path could still be planted as retrievable raw memory.
8. **Ungated evidence UUID ownership.** For non-`user`/`manual` sources, M7 accepts a caller-supplied `turn_evidence_uuid` into `admitted_on_uuid` and calls `link_episode_admission()` (`ingest_intake.py:171-188`). The adapter's ownership/namespace checks are outside scope. Verify that an episode cannot link evidence across session/user/tenant boundaries.
9. **Explicit failed requeue semantics.** `requeue_failed_episode()` enqueues a FAILED row (`ingest_queue.py:318-331`), while the worker delegates actual claiming and max-attempt enforcement to `graph_adapter.claim_pending_episode()` (`ingest_worker.py:90-106`). Verify that repeated explicit requeue cannot bypass the intended attempt ceilings.
10. **Project narrative cap reachability.** `build_project_narrative()` caps its own output at 4,000 chars, but `execute_project_ingest()` queues the backend's returned `result["narrative"]` directly (`project_ingest.py:121-169`). Verify that the production backend always uses the bounded builder and identify which scan fields can contain repository-controlled text.
11. **Content-to-call-count exploitability.** L-3 proves the configured call caps are not hard enforcement. Dynamic testing is still required to establish which attacker-controlled episode shapes reliably cause high LLM call counts or synthetic-edge repair.

## Coverage Table

**Corpus:** exactly the eight files named in the brief, all pinned to `db31eba`. Each file was read from line 1 through EOF. No other service implementation was used to establish a finding.

| File | Measured lines | Review coverage |
|---|---:|---|
| `src/menhir/services/enrichment_steps.py` | 1,462 | Full file: composition, size preflight, Graphiti handoff, shadow LLM path, synthetic edge repair, metadata stamping, finalization, failure handling, timeout wrapper |
| `src/menhir/services/ingest_intake.py` | 420 | Full file: entry points, user-tier admission, raw persistence, evidence linkage/projection, queue gate, direct ingest wrapper |
| `src/menhir/services/ingest_worker.py` | 391 | Full file: claim path, budget checks, extraction dispatch, usage callback, counter lifecycle, circuit requeue, heartbeat |
| `src/menhir/services/ingest_queue.py` | 371 | Full file: configuration setters, rolling session budget, stale recovery, pending/failed requeue, shutdown |
| `src/menhir/services/project_ingest.py` | 199 | Full file: narrative construction/cap and project queue handoff |
| `src/menhir/services/candidate_service.py` | 100 | Full file: candidate list/approve/reject; checked for M7 reachability |
| `src/menhir/services/enrichment_failures.py` | 96 | Full file: parse/manual-review/terminal/retryable classification |
| `src/menhir/services/ingest_gate.py` | 83 | Full file: semaphore and per-namespace locking; confirmed non-content role |
| **Total** | **3,122** | **Reconciled exactly** |

Measured total arithmetic: `1,462 + 420 + 391 + 371 + 199 + 100 + 96 + 83 = 3,122`.

## Review Confidence

**92 / 100** — subjective review confidence, not a measured security score.

Confidence is high because the entire requested 3,122-line corpus was read at the exact commit, EOF was independently reconciled for all eight files, and every finding citation above was re-read in a small exact line range before filing. Confidence is reduced because this was intentionally static-only: no Garak/DeepTeam/Promptfoo execution, no dynamic injection reproduction, and no inspection of out-of-scope Graphiti internals, adapter implementations, `CorrelationService`, lifecycle rehydration, scheduler transport, or recall implementation. Those uncertainties are isolated under Open Questions rather than folded into findings.
