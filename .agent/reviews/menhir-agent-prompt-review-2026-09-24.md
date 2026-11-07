---
artifact_schema: 1
artifact_uuid: 3139efdb-21da-4333-b410-4ad9521bc903
artifact_type: review
artifact_status: COMPLETE
---

# Menhir agent prompt review: coverage, and when and how to use it

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Date: 2026-09-24. Scope: what a Menhir MCP client is told, at `origin/main` 6f6c037b (v0.2.3):
- the server `instructions`;
- the tool descriptions;
- the shipped usage docs (`docs/agent-usage.md`, `docs/templates/AGENTS.menhir.md`).

Evidence comes from the Beacon/Menhir "why" evaluation (archolith-bench, branch `beacon-agent-task-eval`): 19 runs of setup M, which gives the agent file tools plus Menhir's remote MCP under a read-only key. Investigation only; nothing in Menhir was changed.

## Corrections after Astra's review

Astra (Codex gpt-6-astra, read-only, against the origin/main export) confirmed the central diagnosis and corrected these points. The corrections override the findings below:

- **Finding 1:** the one-liner lists provenance first, not structure.
- **Finding 2:** the guidance is "not delivered automatically by MCP", not "never received"; owners can paste the template.
- **Finding 3:** memory *may* hold recorded reasons; not every "why" question needs it. The recall description already links `include_invalidated` to superseded beliefs.
- **Finding 4:** compact and full recall both return summary excerpts (normally 160 characters) plus temporal facts; a pending-write fallback can include episode text. `get_provenance` follows `MENTIONS` links to linked episode excerpts (500 characters by default, up to 5,000), works on entity and View uuids only, and a node may have no sources.
- **Finding 5:**
  - The duplication comes from the class `description` plus the endpoint docstring (`contracts.py:746`), not the title. `compact` and `trace` are documented only on the unregistered module wrapper.
  - Default retrieval is vector plus full-text search over entity nodes, with facts attached afterwards.
  - `include_invalidated` keeps expired fact edges on results already selected (at most 5 per node); it is not a history search.
  - `blast_radius` can return memory previews, so "`query_structure` holds no rationale" is wrong.
  - `get_artifact` returns metadata and document locations, not the body.
- **Finding 6:** the stdio server also pins the gateway discovery tools, and remote listings respect per-client allowlists as well as tiers. Pinning provenance is a surface change, to be tested separately.
- **Section A:** Astra cut "try 2–3 phrasings", "anything that looks odd" and "memory wins on why", which invite over-use and over-trust. It added:
  - identifier discipline (namespace, project and workspace are distinct);
  - one focused query first;
  - citing sources and separating recorded reasons from inference;
  - checking dates, conflicts and stale anchors;
  - the empty-result and error semantics.

  The instructions and descriptions under test are Astra's exact text: a local patch script applies them to a scratch copy of the 9432a60d export. The served instruction is 1,819 characters.
- **Verification:** compare old and new text on matched runs, each starting from a reset memory state (recall stamps access times). Score grounded answers first; recall and provenance rates are diagnostics.

## Check result (2026-09-24)

Two matched arms: M on w1, w3, w5, w7 and w8 × 2, on Luna. Each arm started from a fresh restore of the graph copy. The only difference was the served text:
- **old:** the 113-character one-liner and the current descriptions;
- **new:** Astra's instruction (1,819 characters) and descriptions.

Answers from both arms were mixed and graded blind by Claude (`results/beacon-eval-why-prompt-check-2026-09-24/grades-claude.json`).

| | old | new |
|---|---|---|
| Runs that called `recall_memories` | 5/10 | **10/10** |
| Runs that read `get_provenance` | 4/10 | **8/10** |
| Blind-graded point recall | 0.63 | **0.70** |
| Cost (10 runs) | $0.131 | $0.128 |

Per task (old → new): w1 0.50 → 0.67; w3 1.00 → 1.00; w5 0.50 → 0.33; w7 0.50 → 0.50; w8 0.67 → **1.00**.

- **w8:** with the new text, both runs found the memory-only point ("the old defaults pointed at containers that no longer exist"). No earlier M run had found it (0 of 5).
- **w5:** the drop is the repo-answerable "never committed" point, which one old run read from `.gitignore`. Neither arm got the memory-only "on-brand evidence" point.
- **Cost and regressions:** there is no extra cost, and no regression on w1 or w3.

This is a small sample (2 runs per task and arm). The behaviour change (recall 5/10 → 10/10) is clear; the score gain is modest and needs a larger run to confirm.

## Findings

### 1. The server instructions do not mention memory (high)

Both `src/menhir/mcp/server.py:35` and `src/menhir/api/mcp_remote.py:17` send one sentence, 113 characters:

> Inspect provenance, govern durable context, and analyze repository structure and change impact for coding agents.

Before #38 (b64e664b, 2026-08-30, "position Menhir around provenance and governance") the text was:

> Long-term graph memory for AI agents. Store, recall, and manage memories.

The new text is positioning copy. It never says "memory", "recall", "decision" or "history", and it doesn't say when Menhir beats reading the repository. It also puts "repository structure" first, which is the job agents actually used it for.

**Evidence:**
- In 8 of 19 M runs the agent never called `recall_memories`: w8 5/5, w5 2/2, w7 1/2.
- Every one of those questions names concrete files (`scripts/…`, `docker-compose.test.yml`, `.gitignore` paths). The agent treated each as a code question and used Menhir only as a code index: `query_structure` 67 calls against `recall_memories` 29.
- `query_structure` was the first Menhir call in 13/19 runs.
- Two w8 runs noted that "available history shows only the snapshot commit", so they knew history was missing, and still never asked memory.
- The only point unique to memory on w8 ("the old defaults already pointed at containers that no longer exist") was never found.

### 2. The good guidance exists, but no client receives it (high)

`docs/agent-usage.md` (179 lines) and `docs/templates/AGENTS.menhir.md` (26 lines) cover:
- the bootstrap order;
- structure-first exploration and `blast_radius`;
- targeted recall "for prior decisions, failures, preferences, or constraints";
- `rate_recall`;
- write discipline;
- tracked writes and status semantics;
- failure behaviour.

Both are opt-in: a repository owner has to paste the template into AGENTS.md. Nothing from them reaches an MCP client. The evaluated agent had file tools and could read the Menhir repository, but these docs were never pointed at, so it didn't use them.

### 3. No guidance on *when* memory beats the code (high)

Neither the instructions nor any tool description gives the rule the evaluation supports: **the code shows what; memory shows why.** Where M did recall, it won exactly on the questions whose answer exists only in history (blind human grades, 7 tasks × 2 runs):
- w1: 0.67 against A's 0.33;
- w3: 1.00 against 0.25;
- w7: 0.50 against 0.25.

The template's rule 4 ("prior decisions, failures, preferences, or constraints") is close. But it's missing:
- "why" questions;
- rejected alternatives;
- "anything that looks odd before you change it";
- "when git history is absent or shallow".

It is also rule 4 of 8, after a structure-first rule 2 ("Before filesystem exploration, call query_structure(projects)").

### 4. Recall returns summaries; the exact reason needs `get_provenance`, and nothing says so (medium)

Compact recall returns entity and fact summaries, not episode text. The runs that got the recorded reason word for word read the source episode with `get_provenance`: 8 calls, 6 of them in w1 and w3, the two biggest M wins. Nothing links the two tools. The `recall_memories` description doesn't mention `get_provenance`, and `get_provenance` describes itself as verifying a summary, not as the way to read the reason.

Issue #147 shows the other side: a rationale that enrichment didn't extract as a fact can't be found at all, even with well-aimed queries.

### 5. Tool description defects (medium)

| Tool | Problem |
|---|---|
| `recall_memories`, `recall_context_memories`, `read_flagged_memories`, `build_context`, `query_structure` | The first sentence is repeated ("Search memories by semantic similarity.\n\nSearch memories by semantic similarity."). Title and docstring are concatenated, wasting tokens in every client. |
| `recall_memories` | Says "semantic similarity", undersold: it is hybrid retrieval over entities and facts. `compact` and `trace` are real parameters but undocumented; agents used them anyway (6 and 4 times). The `emotional` and `conflict` presets are documented as partial, which is noise for agents. There's no advice on writing queries (name the component and the decision; try the rejected alternative as a second phrasing). |
| `recall_context_memories` | "Read non-flagged startup context memories (recent + relevant)". Doesn't explain `reader_id` or `workspace`, or that it runs after `read_flagged_memories`. |
| `build_context` | No guidance on when to use it rather than `recall_memories`. |
| `query_structure` | Good list of query types, but doesn't say the graph holds no rationale or history. |
| `include_invalidated` | This is the parameter for history questions ("what was tried and dropped"), but nothing connects the two. Agents used it 9 times, unprompted. |
| `list_artifacts`, `get_artifact`, `list_artifact_questions` | Plans, reviews and open design questions are a strong "why" source; the descriptions only say "list" and "read". |

### 6. Stdio and remote clients see different surfaces (medium)

The stdio server (`create_gateway_server`) makes 8 tools visible and hides the rest behind `search_tools`/`call_tool`:
- `recall_memories`, `add_memory`, `query_structure`, `build_context`;
- `read_flagged_memories`, `recall_context_memories`, `list_todos`, `add_todo`.

The remote server lists every tool the client's tier allows. So a stdio agent never sees `get_provenance`, `get_todo`, `list_artifacts` or `rate_recall` unless it searches. The guidance should name the tools that matter so a gateway agent knows to look for them. `get_provenance` should be considered for the pinned set.

### 7. Guidance asks for tools the tier cannot call (low)

The template tells every agent to call `rate_recall` after using recall output. `rate_recall` requires the `agent` tier, so a read-only client (like the evaluated one: 21 tools) cannot follow the rule.

## Coverage of the 59 tools

Tiers are 21 readonly, 20 agent and 18 operator. For each feature area, the table shows where it is covered: the MCP instructions (I), the tool description (D), `agent-usage.md` (U) and the AGENTS template (T).

| Area | Tools | I | D | U | T | Gap |
|---|---|---|---|---|---|---|
| Bootstrap | read_flagged_memories, recall_context_memories, get_client_context | – | partial | yes | yes | Not in I; `reader_id`/`workspace` unexplained in D |
| Recall: when | recall_memories | – | – | partial | partial | No "why / history / looks odd / no git history" rule anywhere |
| Recall: how | recall_memories, build_context | – | partial | partial | partial | Query phrasing, `include_invalidated`, compact vs trace, the path to provenance |
| Reading exact reasons | get_provenance | – | partial | – | – | Nothing links recall to provenance |
| Structure and impact | query_structure (12 query types) | partial | yes | yes | yes | Should say the graph holds no rationale |
| Writes | add_memory, add_memory_and_track, add_candidate | – | yes | yes | yes | Not in I |
| Retention | flag/unflag/promote/close_memory | – | yes | partial | – | Semantics still open (#119) |
| Todos | add/list/get/close/supersede/resolve/reopen/link | – | yes | partial | – | When to read open todos is not covered |
| Artifacts | list/get/relationships/questions/link/supersede/transition/audit/relocate | – | yes | – | – | Not presented as a source of "why" |
| Conflicts | list_conflicts (+4 operator) | – | yes | – | – | When a conflict should change an answer is not covered |
| Enrichment ops | status/watch/queue/trace (+ operator repair) | – | yes | yes | yes | Fine |
| Beacon | get_beacon_evidence | – | yes | yes | – | Fine |
| Admin | mint/revoke/list_clients, scheduler, namespace, orphans | – | yes | – | – | Operator only; fine |

## Proposed changes

### A. Server instructions (both transports, one shared constant)

The draft below is about 230 words, well within typical client limits, and replaces the one-liner:

> Menhir is long-term memory plus a code-structure graph for coding agents. The code shows *what*;
> memory shows *why*: decisions and their reasons, alternatives tried and rejected, past incidents,
> owner preferences, and what is current versus superseded.
>
> **When to recall** (`recall_memories`): before changing a design choice, default, constraint, or
> anything that looks odd; whenever a question asks why, or about history; when git history is
> missing or shallow; before redoing work that may have been done. Use `query_structure` for layout,
> imports, tests, endpoints and change impact (`blast_radius` before editing); it holds no rationale.
>
> **How:**
> 1. At session start, call `read_flagged_memories`, then `recall_context_memories`, with the same
>    `reader_id` and workspace key.
> 2. Ask a natural-language question naming the component and the decision. Try 2–3 phrasings (the
>    decision, the rejected alternative, the symptom). Pass `file_context` for code-linked questions,
>    and `include_invalidated=true` for history.
> 3. Results are summaries of entities and facts. To read the exact recorded reason, call
>    `get_provenance` on a result's uuid. Plans, reviews and open design questions are in
>    `list_artifacts` / `list_artifact_questions`.
> 4. An empty result means "not found with this query", not "no history".
> 5. The current code wins on what the code does; memory wins on why, unless the code shows the
>    reason changed.
> 6. Store verified decisions with their reasons (`add_memory`); never secrets.

### B. Tool descriptions

1. Remove the duplicated first sentence (the title and docstring concatenation) in all five tools.
2. `recall_memories`: say what is returned; point to `get_provenance` for source text; document `compact`/`trace`; drop the partial presets from the agent-facing text; add one line on phrasing queries.
3. `get_provenance`: "Read the source episode(s) behind a recall result: the exact recorded wording of a decision or reason."
4. `query_structure`: add "Structure only: no history or rationale; use recall_memories for why."
5. `list_artifacts` / `list_artifact_questions`: mention plans, reviews and open design questions as a source of decisions.

### C. Surface consistency

- Consider pinning `get_provenance` in the stdio gateway's `always_visible` list.
- Make the template's `rate_recall` rule conditional on the tool being available (the read-only tier lacks it).

### D. Docs and template

- Move a "when memory beats the code" rule to the top of `AGENTS.menhir.md`.
- Soften "Before filesystem exploration, call query_structure(projects)" so it doesn't frame Menhir as a code index first.
- Keep A, the template and `agent-usage.md` from drifting: the instructions are the default everyone gets, and the docs expand on them.

## How to verify (cheap)

The archolith-bench harness measures this directly. Change only the instructions and descriptions, in a scratch build of the Menhir export, then rerun M on the tasks where agents skipped recall: w5 ×2, w7 ×2 and w8 ×3. That's 7 runs, about $0.10.

Success means:
- recall happens in most of those runs, against 1 of 7 now;
- w8 finds the "containers no longer exist" point at least once;
- the blind-graded score doesn't drop on w1 and w3.

## Related

- Menhir #147: a recorded rationale that enrichment didn't extract as a fact can't be recalled (w2).
- Results: `archolith-bench/results/beacon-eval-why-2026-09-24/`, including `grades-claude.json`.
- The w8 rerun: `results/beacon-eval-why-w8-rerun-2026-09-24/`.
