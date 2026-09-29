---
artifact_schema: 1
artifact_uuid: 7a69ef8f-b1da-440d-9684-776b869e6cd8
artifact_type: plan
artifact_status: PROPOSED
---

# menhir -- scalars on by default for new installs

Status: **Steps 1-5 merged 2026-09-29; steps 6-7 not started.** Baseline `de370260`. Parent #168
(scalar part of #170, with the scalar grounding items from #169). Evidence contract and ratified
limits: `menhir-168-phase0-baseline-contract.md`.

| Step | PR | Merged |
|---|---|---|
| 1 agent-quoted evidence | #198 | `ca2bf04d` |
| 2 value in quote (#89) | #199 | `9d1b4769` |
| 3 decimal equality (#152) | #195 | `474c5920` |
| 4 evidence-only input (#95) | #196, Bench #10 | `a9def52d`, Bench `32ec8ea0` |
| 5 lenient defaults | #197 | `56436722` |

Step 4 deleted the `user:` fallback; Bench now records TurnEvidence for every user turn instead.
Found on the way: multi-word numbers ("twenty-five" -> 5), filed as #200, fix in #201.
Still open: the perceiver-version rule for existing graphs (steps 3 and 5).

## Owner decisions (2026-09-28)

1. **Input route: agent-quoted, for now.** When saving a memory, the agent passes the user's statement
   verbatim. Menhir stores it as `:TurnEvidence` labeled agent-quoted, and scalars read it. Hooks stay
   optional; hook-based verification/upgrade is deferred.
2. **Agreement: lenient.** Threshold `2/3` with attribute, scope and subject reconciliation on. It must
   still pass the ratified zero wrong-owner / zero false-current limits.
3. Menhir **cannot verify** an agent quote is the user's words. Agent-quoted evidence is therefore
   trusted for *perception only*, never for authority.

## Why scalars do nothing on a stock install today

Scalar work discovery (`memory_graph_adapter.list_scalar_dirty_namespaces`, `:1534`) reads
`:TurnEvidence {role:'user', declarant:'user'}` if any exists. Otherwise it falls back to `:Episodic`
content `STARTS WITH 'user:'` (`personal_memory_queries.py:28,48,96,133`), a prefix ingest no longer
writes (#95). TurnEvidence comes only from opt-in client hooks (`scripts/hooks/`, not in the wheel). A
stock install has neither, so scalar_state on = zero input, silently.

## Safety property

> Evidence an agent supplied must never be treated as proof that the user said something. It may feed
> scalar perception; it must not grant user-tier admission, count as user foundation for recall
> authority, or feed lanes that have not opted in.

Every current reader of user-role TurnEvidence, and its required behavior for `source_kind="agent_quoted"`:

| Reader | Location | Agent-quoted |
|---|---|---|
| User-tier admission gate | `domain/truth/admission_gate.py:57` `evaluate_user_tier_claim` (checks role/session/text, **not** source) | **Deny** (downgrade to agent_inference). Otherwise an agent can self-certify `source="user"` at tier 1.0 |
| Scalar authority foundation | `scalar_view_repository.py:308` `scalar_view_has_user_foundation` + `assertions_have_user_foundation`; used at `recall_pipeline.py:1050` | **Not a foundation** |
| Scalar discovery / batch | `turn_evidence_repository.py:352`, `:382` | **Include** |
| Counter discovery / load | `turn_evidence_repository.py:239`, `:323` | Exclude (counters group decides later) |
| Event discovery / batch | `turn_evidence_repository.py:429`, `:458` | Exclude (events group decides later) |
| Episode lifecycle | `episode_lifecycle.py:238` | Audit in step 1; decide include/exclude with a test |
| `evidence_exists()` switch | `turn_evidence_repository.py:231` | Any user-role record flips the whole graph to the evidence path. Acceptable; step 4 removes the dead fallback anyway |

Step 1 must re-grep for every `role = 'user'` / `role: 'user'` reader before closing, since new readers are
the obvious bypass.

## Work (in order; each a separate PR, offline-testable)

### 1. Agent-quoted evidence through `add_memory`
- `mcp/tools/ingest/add_memory.py` and `add_memory_and_track.py`: new optional `user_statement: str`.
  Tool text says: the user's exact words only, never paraphrased, never a third party's words.
- `services/ingest_intake.py` `queue_episode_for_enrichment` (`:62`): when `user_statement` is given,
  call `record_turn_evidence(text=..., role="user", declarant="user", source_kind="agent_quoted",
  source_client=<client name>, session_id, namespace)` (`turn_evidence_repository.py:103`), then link the
  episode through the existing provenance-only branch (`:176`, non-gated). Mutually exclusive with
  `turn_evidence_uuid`. Failure to record must not fail the ingest.
- Apply the reader table above: exclusions in the gate, the foundation checks, and the counter/event queries.
- Duplicates: `turn_key` is derived from session+text; a hook capture of the same prompt has a different
  session key. Decide dedupe (e.g. namespace + prompt_hash within a window) and test it.
- Tests: agent-quoted cannot grant `source="user"`; a View founded only on agent-quoted evidence has no
  user foundation; scalar discovery finds it; counter/event discovery does not; namespace isolation;
  empty/whitespace statement is ignored.

### 2. #89: the value must appear in its quote
`services/typed_scalar_rules.py:823` `_ground_span` checks position only. After locating the span, require
the value in it as digits, number words, or normalized money/unit form; otherwise drop with an `on_drop`
reason. This matters more now that the quote is agent-supplied. Tests: #89's two observed cases drop;
spelled-out values still commit; existing zero-match/ambiguous tests unchanged.

### 3. #152: `10` == `10.0` == `10.00`
`domain/typed_assertion.py:160` `_num_norm` keeps Decimal scale, splitting votes and `assertion_key`
(`:363`). Canonical numeric form for voting and identity (no float; signed zero equal), with display
precision kept. New installs have no stored keys; state the upgrade/replay rule for existing graphs
(perceiver-version bump) rather than stranding keys.

### 4. #95: remove the silent dead end
Either fix the fallback (`personal_memory_queries.py`) to select by something ingest writes, or delete
it and log/report "no scalar input" when scalar_state is on with no evidence. The fallback exists for
legacy LME fixtures; check Bench (`8a8d629`) still needs it before deleting.

### 5. Lenient defaults
`config/settings_model.py`: `personal_memory_scalar_threshold` 1.0 -> 2/3 (`:346`),
`personal_memory_scalar_reconcile_attribute/scope/subject` -> True (`:323-335`). Update `.env.example`
(`:214-248` says these are the defaults and "OFF in production"), the settings comments, and default-value
tests. Record the perceiver-version rule for existing namespaces.

### 6. Confirmation run (paid; needs owner go-ahead and dollar ceiling)
Same inputs, `openai/gpt-6-luna`, scalar_state off vs on (lenient). Use the four-stage breakdown from #170
(assertion emitted, subject bound, View materialized, fold correct), Bench's scalar tools
(`run_scalar_state_e2e.sh`, `scalar_state_coverage.py`, `inspect_scalar_state_graph.py`), and the input as
agent-quoted `add_memory` calls, not hooks. Before the run: add `gpt-6-luna` pricing to the LME cost
summarizer; override Bench's `MENHIR_BENCHMARK_MODE=1` for the scheduler-driven arm or drive consolidation
explicitly. Report quote fidelity (does the quote contain the value) separately.

### 7. Flip the default
Only if step 6 passes the ratified limits: `personal_memory_scalar_state_enabled` -> True, with docs,
`.env.example`, opt-out note, and the limitation "agent-quoted facts are unverified and advisory".

## Explicitly not in this plan
- `personal_memory_scalar_view_authority_enabled` (override): stays off. Agent-quoted facts can never
  gain authority even when it is on (safety property).
- `personal_memory_scalar_history_enabled`, the deterministic router, and canonical-self mode: separate decisions.
- Hook-verified upgrade of agent-quoted records: deferred.
- D1 (#193): path-W runs set models by hand until it is fixed.

## Open questions
- Should agent-quoted evidence ever feed counters/events, or only after those groups' own runs?
- Is `source_client` from the MCP client name good enough as a label, or does it need auth-derived identity?
- Luna reasoning tokens vs `personal_memory_consolidation_max_tokens` (8192): check truncation in step 6.
