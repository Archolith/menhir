---
artifact_schema: 1
artifact_uuid: 13153472-959c-4039-9c21-72e7a940870a
artifact_type: plan
artifact_status: PROPOSED
---

# menhir -- server-side assistant-memory writer (#168 decision 1b)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Not started. Created 2026-10-26; nothing named for it exists on main as of 2026-11-07.
Parents: #168 decision 1, option b, and
`menhir-statement-time-supersession-plan-2026-10-25.md` (item 10).
Supersedes the writer placement in `menhir-agent-memory-writer-plan-2026-10-23.md` P3 (bench-side
writer). That plan's P1 (tier guard, #377) and P2 (recall provenance) have landed and are reused.
Code read: Menhir `b7bea48c`, bench reference
`scripts/longmemeval/lib/memory_writer.py` at f5bdf10. Investigator trace, with its load-bearing
claims re-checked by hand where they are cited below.

## 1. Goal

A client sends each assistant turn verbatim with `role="assistant"`. Menhir decides what to
remember from it: it keeps the raw turn as a source memory and runs an LLM writer that stores 0..N
standalone, third-person, dated notes ("On 2023/05/01 the assistant recommended ..."), grounded on
that turn's evidence, at the agent tier, store-only by default.

Owner principle (2026-10-26): anything that decides what gets remembered belongs in Menhir; the
bench is a plain client. Under it, a writer result counts as Menhir's ability only when the writer
runs inside Menhir.

Expected result: on `o500-ssaA30`, notes from Menhir reach the bench writer pilot's 29-30/30, against
0/30 for `src-spread-par`. The (a) pilot (raw turns store-only, no writer) shows the baseline
without a writer.

## 2. Constraints (from code at b7bea48c)

- **Intake.** REST `POST /api/memory` (`api/routes.py:495-567`) and MCP `add_memory` both reach
  `IngestService.queue_episode_for_enrichment` (`services/ingest_intake.py:69-396`). `role` is
  validated against `TURN_ROLES` (`:101-106`, `domain/turn_role.py:13`) and stored as
  `:Episodic.turn_role` by `create_pending_episode` (`infrastructure/episode_lifecycle.py:109-203`),
  a MERGE on `uuid`. Idempotent replay returns early on `ingest_key` (`ingest_intake.py:117-137`).
- **Store-only.** `extraction_mode=store_only` is durable on the node. The worker embeds, then
  finishes READY under a fence and returns before any extraction or budget step
  (`services/ingest_worker.py:205-211`, `_finish_store_only` `:312-327`,
  `mark_episode_store_only` `episode_lifecycle.py:1137-1182`, fence = state ENRICHING + owner +
  `processing_started_at`). READY is terminal: no sweep revisits a READY episode.
- **Derived episodes today.** Only `create_evidence_projection` (`episode_lifecycle.py:441-569`) makes
  a second episode from a turn. It is user-only (`t.role = 'user' AND t.declarant = 'user'`, `:499`),
  MERGEs on the turn id, and is created best-effort after the parent. A crash between the two writes
  leaves no projection, and nothing recreates it.
- **Claim and recovery predicates match every `:Episodic`.** `claim_pending_episode`
  (`episode_lifecycle.py:815-987`) takes any PENDING/FAILED `:Episodic`. Stale ENRICHING is reset by
  `episode_maintenance.py:29-71`. A pre-writer Menhir that claims an `:Episodic` it does not
  recognize extracts it (the store-only branch is the only exit before extraction).
- **LLM.** `LLMAdapter._chat_text` (`infrastructure/llm.py:235-309`) has no JSON mode. None of the
  three backends' `create_chat_completion` (`infrastructure/providers.py:143, 255, 338`) takes a
  `response_format`. `_strip_thinking_tags` collapses whitespace. Default `max_retries=8`.
- **The per-job LLM cap does not bind.** Chat calls are started `report_only=True`
  (`providers.py:290-301`), and over-cap report-only calls only warn (`ingest_worker.py:394-410`).
  Spend is recorded to `llm_usage_events` only through the per-episode callback that the worker
  installs (`ingest_worker.py:153-157`, `_record_episode_llm_usage` `:375-521`). The bench cost
  guard reads that table.
- **Tier.** Non-user/manual sources are agent tier (`ingest_intake.py:169-197`). Agent episodes can't
  found Views (`domain/scalar_view_authority.py:66`). The #377 guard keeps a human edge against
  a non-human cause (`infrastructure/graphiti_edge_invalidation.py:136-148`).
- **Source-memory lane.** `_build_source_memories` (`services/recall_pipeline.py:192-267`) runs a
  cosine search over embedded `:Episodic` (`infrastructure/memory_queries.py:411-474`) with no role,
  source or extraction-mode filter. Evidence projections are excluded by the structural predicate
  (`domain/structural_memory.py:69`). Content is hard-cut at `source_memory_max_chars` with a trailing
  ellipsis (`recall_pipeline.py:239-243`). The default is 600 (`config/settings_model.py:477`, range
  100..4000). `SourceMemory` (`domain/recall.py:306-320`) has no provenance field.
- **Provenance on ranked results exists.** `domain/recall_provenance.py` (`said_by`), filled post-rank
  (`recall_pipeline.py:2272-2302`). It is based on episode `source`, and source memories don't get it.
- **MCP `add_memory`** (`mcp/tools/ingest/add_memory.py:12-24`) takes `role`, but not `store_only` or
  `idempotency_key`, though `backend.queue_episode` accepts both.
- **Stated design stance.** `/api/turn-evidence` documents that "the server runs no LLM"
  (`api/routes.py:617-647`). The writer runs in the worker, not on that endpoint, but the docs
  must say Menhir now runs a server-side writer for assistant turns when it is enabled.

## 3. State machine and safety properties

Actors: the client (REST/MCP); the intake handler; N ingest-worker tasks in one serve process; the
maintenance scheduler (stale-lease recovery, embedding repair); the LLM provider; an operator
restarting or redeploying (an app-only deploy can overlap an old and a new process only if the
writer lease allows it; see open question 4).

Durable facts: parent `:Episodic` (the raw turn), its `:TurnEvidence`, a new `:AssistantWriterJob`
node, child note `:Episodic` nodes, and `llm_usage_events`.

Safety properties:
- **W1, one note set per job.** The committed notes for a job come from exactly one writer reply.
  Two replies never contribute to the same job.
- **W2, no lost jobs.** If the parent exists and the writer was on at intake, a job exists. If the
  job exists, it reaches READY (notes committed, possibly zero) or terminal FAILED after its attempt
  cap. Never neither.
- **W3, the raw turn is never hostage to the writer.** The parent's own lifecycle (embed, READY,
  recall) is independent of the job's outcome.
- **W4, no tier leak.** A note is agent tier: `source="agent-writer"`, `turn_role="agent"`. It
  never founds a View. If notes are ever extracted, they can't end a human edge (#377).
- **W5, old versions are blind to jobs.** No pre-writer Menhir query claims, recovers, extracts or
  recalls a job.

Transitions and the fact that authorizes each one:
1. **Intake.** In the same write as `create_pending_episode`, the parent and the job are created
   together when `turn_role == "assistant"` and the writer setting is on. The job MERGEs on
   `job_key = writer_version + (turn_evidence_uuid or parent uuid)`, with
   `writer_version = sha(prompt, model, granularity)`. An idempotent replay of the parent returns
   before the write, so it creates no second job. Authority: the request plus the setting.
2. **Claim.** `PENDING|FAILED (retry_after passed, attempts < cap)` goes to RUNNING, with `owner`,
   `claimed_at`, a `lease_expires_at` longer than the job's local timeout, and `attempts+1`.
   Authority: an atomic conditional write, modeled on `claim_pending_episode`.
3. **Run.** Read the parent content and date, plus the preceding user turn (open question 2). Make
   one LLM call with bounded `max_retries` and `max_tokens`, under a per-job usage callback (so
   spend reaches `llm_usage_events`). Parse strictly: malformed output means failure, never
   "no items". Cap the result at 20 notes per turn, as in the bench.
4. **Commit, one fenced transaction.** The fence is owner + `claimed_at` + state RUNNING. Inside it:
   MERGE each child `:Episodic` on `ingest_key = job_key + "#" + i`, with store-only, agent tier,
   the parent's `reference_time` and `session_id`, and `ADMITTED_ON` the turn evidence; record the
   output sha and note count on the job; and set READY. A lost fence writes nothing.
   Authority: the fence. This gives W1, because a second reply can only commit by holding the fence,
   which ends the first owner's ability to commit. Mid-run crashes leave the job RUNNING. After the
   lease, recovery makes it claimable again and the LLM call repeats; the cost of that is bounded by
   the attempt cap.
5. **Enqueue the children** (in-memory). A crash here leaves them PENDING `:Episodic`, which the
   existing pending sweep picks up. They then follow the existing store-only path (embed, READY).
6. **Fail.** An LLM error, a parse error or a timeout sends the job to FAILED with a backoff, and
   the job is terminal at the cap. The parent is untouched (W3).

Why a separate label and not `:Episodic`: every existing claim, recovery, recall and maintenance
query matches `:Episodic`. A job stored as one would be extracted by a pre-writer process (W5) and
would need a new exclusion in every structural predicate. `:AssistantWriterJob` gets W5 and the
recall exclusion with no change to those queries. The cost is a small claim/recover loop of its own.

## 4. Steps

Each step is one PR in Menhir unless marked. Order: S1-S4 first; S5-S7 are independent.

- **S1. LLM JSON call.** `infrastructure/providers.py` (the three `create_chat_completion`s): an
  optional `response_format` passed through where the backend supports it, ignored otherwise.
  `infrastructure/llm.py`: a `chat_json(system, user, *, operation, max_tokens, max_retries)` that
  returns the raw text without `_strip_thinking_tags` whitespace collapsing, with the caller
  parsing it. No existing call changes.
- **S2. Job repository.** New `infrastructure/assistant_writer_jobs.py`: create-with-parent (used by
  `create_pending_episode` through an optional `writer_job` argument in
  `episode_lifecycle.py:109-203`), `claim_writer_job`, `recover_stale_writer_jobs`,
  `commit_writer_notes` (the fenced transaction of transition 4, reusing the child-node shape of
  `create_pending_episode` and `link_episode_admission`), and `fail_writer_job`. A uniqueness
  constraint on `job_key` and on child `ingest_key` (the latter exists for ingest keys; verify).
- **S3. Writer domain.** New `domain/assistant_writer.py`: the prompt (ported from the bench
  `WRITER_PROMPT`, turn granularity), `writer_version`, the request builder (date, preceding user
  turn, assistant turn), and the strict parser (port `parse_items`). Pure functions, no I/O.
- **S4. Worker wiring.**
  - `services/ingest_worker.py`: a writer-job branch in the loop with its own claim, a usage
    callback keyed on the job id, a local timeout below the lease, and commit/fail.
  - `services/ingest_intake.py`: pass `writer_job` when role is assistant and the setting is on.
  - `services/maintenance_scheduler.py`: schedule `recover_stale_writer_jobs`.
  - Settings: `MENHIR_ASSISTANT_WRITER` (default off), `MENHIR_ASSISTANT_WRITER_MODEL`,
    `..._MAX_TOKENS`, `..._MAX_ATTEMPTS`, and `MENHIR_ASSISTANT_WRITER_NOTES_EXTRACT` (default off, so
    notes are store-only), via `config/settings_model.py`, `config/feature_flags.py`,
    `core/bootstrap.py:218-237`, and `services/ingest_queue.py` `configure`.
  - Docs: the README and the `/api/turn-evidence` docstring note on server-side LLM use.
- **S5. Source-lane long memories (decision 2).** `services/recall_pipeline.py:239-243`: cut at the
  last sentence boundary at or under the cap, falling back to the hard cut, behind a setting.
  `domain/recall.py` `SourceMemory` gets `said_by` (from the episode `source`, as in
  `recall_provenance.py`). It is rendered as `[agent-recorded]` only for non-user rows, in
  `context_builder.py:659-676` and the MCP/REST source sections. User rows stay byte-identical. The
  default cap is not changed here; the freeze build scores 600 and 2000 (parent plan item 10).
- **S6. MCP `add_memory` exposes `store_only` and `idempotency_key` (decision 3).**
  `mcp/tools/ingest/add_memory.py`, `add_memory_and_track.py`, with schema and docs. Small and
  independent.
- **S7. Bench (bench repo, separate session).** No writer code is needed. The plain client is the
  `LME_ASSISTANT_STORE_ONLY` flag (bench `403d5b2`), which sends every assistant turn verbatim,
  store-only, with `role="assistant"` and its turn evidence. With Menhir's writer on, that client
  gets notes server-side. The build records `MENHIR_ASSISTANT_WRITER*` in lineage. The bench writer
  (`adaptive-writer`) stays for reproducing the earlier pilots only.

## 5. Risks

- **Blocked:** none known.
- **Mitigable:**
  - *Fence race in the commit.* The commit relies on the same check-then-set idiom as the existing
    fenced writes. Mitigation: a two-worker test (below), plus checking that the lock is taken
    before the fence predicate is evaluated (e.g. by a SET-first idiom), the same way the existing
    fenced writes do it.
  - *Repeat spend on crash or lease loss.* Bounded by `..._MAX_ATTEMPTS` and the LLM's
    `max_retries`. Visible in `llm_usage_events`.
  - *Writer cost on a 500 build.* Measured: about $0.011 for 133 turns, so about $0.45 for 5,481
    turns. Store-only notes add only embeddings (#168 projection: $6-7 for writer plus store-only
    on o500).
  - *Notes crowd the source lane.* Spread takes each session's best row first
    (`domain/source_memory_spread.py`), so notes compete with their own raw turn. Watch the per-type
    numbers in the freeze build; a per-session cap is the fallback.
  - *Long-note truncation at 600.* Store-only notes lost 2/30 at 600 in the bench pilot. Mitigated by
    S5 and by scoring at 2000.
- **Acceptable:** a duplicate raw turn sent without an idempotency key or turn evidence gets its own
  job and duplicate notes, the same as any duplicate ingest today.

## 6. Open questions (owner)

1. **Raw assistant turns when the writer is on.** Keep whatever the client sent (store-only or
   extracted), or have Menhir force store-only for `role="assistant"`? The principle points to
   Menhir deciding. Recommended: force store-only when the writer is on.
2. **The preceding user turn.** The bench writer sends it, and it names the topic. Server-side
   options: (a) the latest user `:TurnEvidence` in the same namespace and session recorded before
   the assistant turn's evidence; (b) an optional `reply_to_turn_evidence_uuid` on the request;
   (c) none. Recommended: (b) when given, else (a). Not measured; the pilot could ablate it.
3. **Notes store-only or extracted.** At a 2000 cap, the bench pilot scored 30/30 with store-only
   notes and 29/30 with extracted notes. At a 600 cap: 27/30 and 30/30. Recommended: store-only plus
   S5, since extraction is the expensive part ($14-21 against $6-7 projected for o500).
4. **Overlapping processes.** Does an app-only deploy ever run two serve processes with live workers
   against one graph? If not, W5 matters only for rollback onto an older image. It still holds,
   because jobs aren't `:Episodic`.

## 7. Validation

Counterexample tests (Neo4j on a throwaway container):
- Worker A claims a job and stalls past its lease. B claims it and commits. A resumes and commits:
  no-op. Exactly B's notes exist, and the job is READY once.
- A crash after the LLM reply and before commit: recovery, reclaim, one note set.
- A crash after commit and before enqueue: the children are PENDING, and the pending sweep finishes
  them store-only.
- An idempotent replay of the parent creates one job.
- A job node on a graph: `claim_pending_episode`, stale recovery, `list_pending_episode_uuids` and the
  source-lane query return nothing for it (W5).
- An LLM error or malformed JSON: the job goes FAILED with a backoff, then terminal at the cap. The
  parent is READY and recallable throughout (W3). Malformed output never commits zero notes.
- A zero-item reply: the job is READY with no children.
- Writer off: no job is created, and request digests and recall output are unchanged.
- Notes are agent tier and don't found a View. With note extraction on, a note restating a
  user fact with a different value doesn't end the user edge (#377 regression).
- S5: user rows render byte-identically; a long agent note is cut at a sentence boundary.

Measurement (paid; each step needs approval):
1. Pilot `o500-ssaA30`: Menhir writer on, bench `LME_ASSISTANT_STORE_ONLY=1`, lane chain, then score
   at 600 and 2000. Compare with the bench-writer pilot (29-30/30) and the (a) pilot. Expected about
   $0.05.
2. Then the real freeze build (#168 item 1c): o500 as the regression gate plus a frozen-config LME-S
   run, with the bench sending raw turns only.

## Coverage summary

- **Inspected:**
  - Menhir b7bea48c:
    - `api/routes.py` (495-567, 617-755) and `api/routes_support.py`;
    - `services/ingest_intake.py`, `services/ingest_worker.py` (74-581), `services/recall_pipeline.py`
      (192-267, 2272-2302);
    - `infrastructure/episode_lifecycle.py` (109-286, 441-664, 815-987, 1137-1189),
      `infrastructure/providers.py` (143-338), `infrastructure/llm.py` (189-309),
      `infrastructure/graphiti_edge_invalidation.py` (136-148);
    - `domain/turn_role.py`, `domain/recall_provenance.py`, `domain/structural_memory.py:69`;
    - `mcp/tools/ingest/add_memory.py`.
  - Investigator trace, with the cited worker, provider, recall and MCP lines re-read by hand.
  - Bench `memory_writer.py` (full) and `ingest.py` (turn routing and `_ingest_turn`).
- **Not inspected:**
  - The internals of `mark_episode_ready`, `mark_episode_failed` and `mark_episode_pending`, and how
    the existing fenced writes take their locks (needed for the S2 commit; read before coding).
  - The source tier tables in `domain/utils.py`.
  - Whether a uniqueness constraint exists on `:Episodic.ingest_key`.
  - The writer-lease semantics across deploys (open question 4).
- **Assumptions without code evidence:**
  - Ported to Menhir, the writer prompt reproduces the bench pilot's notes. Same prompt and model,
    but a different client path and no `response_format` until S1.
  - One LLM call per assistant turn fits well inside a lease of a few minutes.
