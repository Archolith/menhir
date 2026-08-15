# Menhir M10 — Explorer Research Labs LLM & AI Security Audit (A7)

**Repository:** `Archolith/menhir`  
**Source revision audited:** `efe4583dc62c6db303f003d5ce1604017787ff39` (`main` matched this SHA before the report write)  
**Audit mode:** read-only static source review; this report is the only repository file created  
**Menhir source corpus:** exactly the six requested files under `src/menhir/explorer/`, **3,095 physical lines measured**  
**Execution:** no Menhir runtime execution or model calls. Garak, DeepTeam, and Promptfoo: **NOT RUN — no execution environment.**

## Executive Summary

This slice contains six code-supported findings: **five Medium and one Low**. There is no supported High or Critical finding in the six-file corpus.

The most important result is that the Extraction Lab is not fully prompt-faithful to the production Graphiti path it is intended to measure. Its default arm does call the locked Graphiti `extract_nodes`/`extract_edges` machinery rather than copying the prompts, but the lab constructs the current episode with `valid_at=utc_now()` and exposes no fixture field for the current message's reference time (`src/menhir/explorer/extraction_lab.py:507-516`). Graphiti 0.29.2 production `add_episode` instead stores the caller's `reference_time` as `episode.valid_at` (`graphiti_core/graphiti.py:1084-1111`), and edge extraction copies the latest episode's `valid_at` into the model context as `reference_time` (`graphiti_core/utils/maintenance/edge_operations.py:183-204`). The locked edge prompt renders that value as `REFERENCE_TIME` and instructs the model to use it for relative temporal expressions (`graphiti_core/prompts/extract_edges.py:105-160`). A historical/replayed fixture containing relative time therefore measures a prompt with the lab run time, not the production event reference time.

The Extraction Lab also mutates Graphiti's process-global `prompt_library.extract_nodes.extract_message` to implement prompt variants. It restores the wrapper per arm, and arms are sequential inside one `run_extraction_lab` invocation, but there is no lock across simultaneous invocations. A variant request can therefore alter the prompt seen by a concurrent baseline request; two variant requests can also restore wrappers out of order and leave a stale variant installed (`src/menhir/explorer/extraction_lab.py:755-851`, `:911-954`). This is an instrument-integrity race rather than a production finding; whether Explorer and production extraction share a process is outside this six-file scope.

Judge reliability is mixed. Recall Lab's optional judge fails the whole judgment cleanly on malformed JSON, refusal text that does not validate, unavailable backends, or any failed pass (`src/menhir/explorer/recall_lab.py:180-289`). By contrast, Extraction Lab's fuzzy semantic scorer maps an unavailable scoring backend, an API exception, or an empty/malformed/refused non-JSON response to zero fuzzy matches and then returns ordinary numeric gold scores with no scoring-mode/degradation field (`src/menhir/explorer/extraction_lab.py:240-323`, `:384-433`, `:911-954`). That can make semantic matches look like extraction misses without recording that the grader failed.

Two other measurement surfaces can silently distort aggregates. The semantic-similarity lab converts a missing embedding to literal similarity `0.0`, which is indistinguishable from a genuine zero-similarity observation when the precision/recall curve and category means are computed (`src/menhir/explorer/shadow_semantic_similarity_lab.py:175-266`). And both Extraction Lab's fuzzy scorer and Recall Lab's optional judge put fixture/retrieved text directly into a model-facing user prompt; no structural data channel prevents instruction-shaped benchmark content from steering the grader (`src/menhir/explorer/extraction_lab.py:240-323`; `src/menhir/explorer/recall_lab.py:142-227`). This is graded Medium because the affected surface is research/evaluation, not a trusted production action, but a poisoned benchmark can still manufacture the evidence used for design decisions.

The requested six-file line total reconciles to **3,095**: 957 + 802 + 686 + 266 + 249 + 135. I attempted the requested shell-side `wc -l` check, but the shell could not resolve `github.com`, so the checkout never occurred and I do **not** claim a successful `wc` run. The reported counts were independently measured with pinned-SHA EOF probes through the GitHub connector and match the supplied total exactly. External Graphiti 0.29.2 files were opened only to verify dependency behavior and are not counted in the Menhir corpus.

CF-70, CF-55, and the already-known unreferenced helpers (including `recall_packet_prototype.py:_relevant`) were treated as supplied programme context and were not re-filed.

## Findings

### Medium

### X-1 — Historical extraction fixtures use the lab run time in the edge prompt instead of production reference time

**Severity: Medium**

**Triggering content / payload shape.** An Extraction Lab request that evaluates a current message whose fact extraction depends on relative temporal language or temporal bounds — for example, a historical/replayed fixture containing phrases such as “last week”, “yesterday”, or another expression resolved against the current episode reference time.

**Reachability chain.** Entry is `run_extraction_lab()` (`src/menhir/explorer/extraction_lab.py:911-954`), reached by the already-confirmed Explorer research surface from CF-70 (not re-filed here). The default tuning is `prompt_variant="baseline"`, `context_episode_count=10`, and no current-message reference-time control exists (`src/menhir/explorer/extraction_lab.py:24-66`). `_run_extraction_arm()` unconditionally computes `now = utc_now()` and assigns both `created_at=now` and `valid_at=now` to the current `EpisodicNode` (`src/menhir/explorer/extraction_lab.py:507-516`), then calls Graphiti `extract_edges` with that episode (`src/menhir/explorer/extraction_lab.py:555-564`). In locked Graphiti 0.29.2, production `add_episode()` retrieves context relative to its caller-supplied `reference_time` and assigns `valid_at=reference_time` to the episode (`graphiti_core/graphiti.py:1084-1111`). Graphiti edge extraction then chooses the latest episode by `valid_at` and writes that value into `context['reference_time']` (`graphiti_core/utils/maintenance/edge_operations.py:183-204`); `extract_edges.edge()` renders it into `<REFERENCE_TIME>` and explicitly instructs the model to use it for relative temporal expressions (`graphiti_core/prompts/extract_edges.py:105-160`).

**Effect if reached.** The lab and production model prompts differ deterministically in `REFERENCE_TIME` for a replay whose production reference time is not the lab execution time. The lab can therefore report extraction behavior for a temporal prompt that production did not use. The proposition serializer subsequently records only `edge.fact`, source/target UUIDs, and edge UUID (`src/menhir/explorer/extraction_lab.py:575-591`), so the lab result does not surface the extracted `valid_at`/`invalid_at` values that would make this divergence obvious.

**Why Medium.** This is a measurement-integrity failure in a research instrument. It does not directly change production state, but conclusions about extraction behavior on temporal/historical fixtures can be based on a non-production prompt.

### X-2 — Process-global extraction prompt patches are serialized per request, not across concurrent lab requests

**Severity: Medium**

**Triggering content / payload shape.** Two overlapping `run_extraction_lab()` invocations where at least one arm requires a prompt patch: a non-baseline prompt variant, non-empty `known_entities`, or non-empty `retrieved_context`. A concurrent victim arm may itself be the default baseline.

**Reachability chain.** Entry is `run_extraction_lab()` (`src/menhir/explorer/extraction_lab.py:911-954`). The default arm is baseline and the patch-producing controls are opt-in (`src/menhir/explorer/extraction_lab.py:24-66`). `_run_extraction_arm()` calls `_apply_extraction_patches()` before awaiting Graphiti extraction (`src/menhir/explorer/extraction_lab.py:500-545`). `_apply_extraction_patches()` reads the current process-global wrapper, installs `VersionWrapper(variant_extract_message)` into `prompt_library.extract_nodes.extract_message`, and returns a restore closure that writes the saved wrapper back (`src/menhir/explorer/extraction_lab.py:755-851`). The per-arm `finally` invokes the restore (`src/menhir/explorer/extraction_lab.py:615-630`). `run_extraction_lab()` uses a list comprehension to serialize arms only inside that one invocation; there is no module lock, request lock, or ownership token around the global mutation (`src/menhir/explorer/extraction_lab.py:911-954`).

**Effect if reached.** If request A installs variant A and awaits its LLM call, a concurrent baseline request B can execute while the global wrapper still points at A; B's own baseline `_apply_extraction_patches()` is a no-op, so B can silently measure variant A as “baseline.” With two patched requests, A can save production then install A, B can save A then install B, A can restore production while B is active, and B can finally restore A — leaving A's wrapper installed after both requests have completed. These outcomes follow from the save/assign/await/restore ordering; no runtime reproduction is claimed.

**Why Medium.** Concurrent research runs can corrupt each other's experimental condition without an error signal, and the second interleaving can leave later lab runs under a stale prompt. Whether that stale global also reaches production traffic depends on process topology outside this slice and is left under Open Questions.

### X-3 — Extraction Lab judge failures silently become ordinary extraction misses or exact-only scores

**Severity: Medium**

**Triggering content / payload shape.** An arm with at least one unmatched extracted item and at least one unmatched gold item, combined with any of: failure to construct the scoring backend; a backend lacking `create_chat_completion`; an exception from the fuzzy judge call; an empty response; a refusal/non-JSON response; or malformed JSON that contains no parseable `matches` list. A `None` return from a nominally successful call is a different case: it can raise during parsing and make the arm fail rather than silently degrade.

**Reachability chain.** `run_extraction_lab()` always attempts to build a shared scoring backend; on construction failure it catches the exception and leaves `llm_backend=None`, then still runs every arm (`src/menhir/explorer/extraction_lab.py:911-954`). `_score_extraction()` sends exact-match leftovers to `_fuzzy_matched_counts()` (`src/menhir/explorer/extraction_lab.py:324-359`, `:384-415`). `_fuzzy_matched_counts()` returns `(0, 0)` when the backend/callable is missing or the call raises, and `_parse_fuzzy_matches()` returns `[]` for no JSON object, invalid JSON, or a non-list `matches` field (`src/menhir/explorer/extraction_lab.py:240-323`). `_scored_set_comparison()` then uses those zero fuzzy counts in the ordinary recall/precision arithmetic and unsupported count (`src/menhir/explorer/extraction_lab.py:384-433`). The successful arm result contains `gold_scores` but no field stating whether fuzzy judging ran, failed, or was unavailable (`src/menhir/explorer/extraction_lab.py:575-614`).

**Effect if reached.** A semantic-equivalent extraction that required fuzzy matching is scored as a miss when the judge failed, and the extracted item can also be counted as unsupported. Backend-construction failure silently changes the entire run from two-tier scoring to exact-string-only scoring. The returned arm can remain `ok=True` and `degraded=False`, so consumers cannot distinguish “model extraction was wrong” from “grader did not run.”

**Why Medium.** The failure mode directly biases the primary measurement produced by the research lab while preserving a normal-looking successful result.

### X-4 — Benchmark and retrieved text is inserted directly into evaluator prompts, allowing fixture/result content to steer the grader

**Severity: Medium**

**Triggering content / payload shape.** Instruction-shaped text in content that the lab itself asks an LLM to grade. Two code-proven examples are: (a) an unmatched gold/extracted string in Extraction Lab that contains instructions aimed at the fuzzy matcher, or (b) a Recall Lab query or retrieved memory name/content that contains instructions aimed at the optional retrieval judge. The injected instruction can ask for a syntactically valid response, so schema validation is not a trust boundary.

**Reachability chain A — Extraction Lab fuzzy judge.** `GoldExtraction.mentions` and `.propositions` accept free strings in the lab request, and extracted fact/name text also enters scoring (`src/menhir/explorer/extraction_lab.py:90-118`, `:324-359`). `_fuzzy_match_prompt()` interpolates each unmatched item directly into a user prompt using Python `repr`; there is no escaping or separate data role beyond textual headings (`src/menhir/explorer/extraction_lab.py:240-250`). When a scoring backend exists and both unmatched sets are non-empty, `_fuzzy_matched_counts()` sends that prompt to `create_chat_completion()` and accepts returned `(extracted_index, gold_index)` pairs after range and one-to-one checks (`src/menhir/explorer/extraction_lab.py:276-323`). Those accepted pairs increase matched counts used in recall and precision (`src/menhir/explorer/extraction_lab.py:384-433`). The default lab path automatically attempts to create this scoring backend (`src/menhir/explorer/extraction_lab.py:936-954`).

**Reachability chain B — Recall Lab optional judge.** `RecallLabRequest.query` is free text and `judge` defaults to `False` (`src/menhir/explorer/recall_lab.py:65-93`). When the caller opts into judging, `run_recall_lab()` requires at least two healthy arms and an available `judge_llm`, then calls `judge_recall_lab()` (`src/menhir/explorer/recall_lab.py:618-674`). `_judge_prompt()` writes the raw query plus each retrieved memory's `name`/`content` into the judge user prompt; each result text is length-capped at 800 characters but not structurally escaped from instructions (`src/menhir/explorer/recall_lab.py:142-178`). `_judge_pass()` sends that prompt to the model and validates response shape/identifiers, not whether the decision was influenced by result text (`src/menhir/explorer/recall_lab.py:190-227`).

**Effect if reached.** There is no code-level boundary that makes benchmark/retrieved prose non-instructional to the evaluator. If the model follows instruction-shaped fixture or memory content and emits a valid response, Extraction Lab can gain or lose fuzzy matches and Recall Lab can change arm scores/winner while all parsers succeed. This report does not claim a particular model will obey a particular payload; the supported issue is that untrusted benchmark data and grader instructions share the same model-facing prompt channel and a successful poisoned response is accepted as ordinary evidence.

**Why Medium.** This is a research/evaluation prompt-injection surface rather than a trusted production action. The harm is nevertheless real if a poisoned fixture or stored memory manufactures the benchmark evidence used to choose production changes.

### X-5 — Missing or invalid embedding observations are silently converted into genuine similarity 0.0 samples

**Severity: Medium**

**Triggering content / payload shape.** Any call to `score_rows(rows, embeddings)` where either text for a row has no entry in the supplied embedding map, or where `cosine_similarity()` receives unequal-length, empty, or zero-norm vectors.

**Reachability chain.** Entry is `score_rows()` in the semantic-similarity lab. `cosine_similarity()` returns literal `0.0` for unequal vector lengths, empty vectors, or zero norms (`src/menhir/explorer/shadow_semantic_similarity_lab.py:175-185`). `score_rows()` also substitutes `0.0` whenever either embedding lookup is missing, and `ScoredRow` carries only the float — no missing/degraded flag (`src/menhir/explorer/shadow_semantic_similarity_lab.py:188-207`). `precision_recall_curve()` treats every `ScoredRow.similarity` as a real observation and includes it in TP/FP/FN calculations (`src/menhir/explorer/shadow_semantic_similarity_lab.py:221-251`); `category_summary()` likewise includes it in count/mean/min/max (`src/menhir/explorer/shadow_semantic_similarity_lab.py:257-266`).

**Effect if reached.** A missing positive embedding becomes an apparent low-similarity positive and can create false negatives at thresholds above zero. A missing negative embedding becomes an apparently easy negative and can improve separation/precision. Category means/minima are also altered. The resulting aggregate contains no marker allowing a consumer to separate embedding-data loss from actual semantic distance.

**Why Medium.** This is silent missing-data imputation inside the measurement instrument. It can bias the similarity curve in either direction while returning fully valid output. The out-of-scope runner's behavior on partial embedding-service failure is not assumed; the trigger here is the incomplete/invalid embedding input explicitly accepted by these in-scope functions.

### Low

### X-6 — Recall judge's “exactly once” ranking check accepts duplicate ranking identifiers

**Severity: Low**

**Triggering content / payload shape.** A syntactically valid judge JSON response whose `ranking` array contains every expected alias at least once but duplicates one or more aliases, for example `['R1', 'R1', 'R2']` when the expected set is `{'R1', 'R2'}`, while the `scores` object contains the expected keys.

**Reachability chain.** `RecallLabJudgeResponse.ranking` is only constrained to a non-empty list (`src/menhir/explorer/recall_lab.py:103-116`). When Recall Lab judging is opted in (`judge=False` by default; `src/menhir/explorer/recall_lab.py:65-81`) and the healthy-arm/backend gates pass (`src/menhir/explorer/recall_lab.py:618-674`), `_judge_pass()` parses the response and checks `set(parsed.ranking) != expected` even though the prompt requires every identifier “exactly once” (`src/menhir/explorer/recall_lab.py:119-139`, `:190-227`). Set conversion discards duplicates. The accepted response is returned with every ranking list element mapped into `ranking_ids` (`src/menhir/explorer/recall_lab.py:218-236`).

**Effect if reached.** A malformed ordinal ranking can be reported as a successful pass. The current in-file winner-vote and average-score aggregation does not consume `ranking_ids`, so the duplicate does not directly change those aggregates (`src/menhir/explorer/recall_lab.py:260-295`); it corrupts the per-pass ranking artifact and any downstream analysis that treats that artifact as a permutation.

**Why Low.** The parser violates its stated response invariant, but the malformed ranking is not used by the in-file winner/score aggregate. Downstream use of `ranking_ids` is outside this slice and is not assumed.

## Lab-vs-Production Prompt Comparison

| Lab | Model-facing construction in this file | Comparison to the production path it measures |
|---|---|---|
| `extraction_lab.py` | Uses production Graphiti client construction, calls Graphiti `extract_nodes` and `extract_edges`, and for prompt variants wraps Graphiti's real `extract_message()` output instead of hand-copying the whole prompt (`src/menhir/explorer/extraction_lab.py:445-572`, `:755-851`). | **Partial match.** Baseline node prompt construction delegates to the locked Graphiti builder; locked Graphiti `extract_nodes` itself calls `prompt_library.extract_nodes.extract_message(context)` (`graphiti_core/utils/maintenance/node_operations.py:256-278`). Default lab context window is 10 (`src/menhir/explorer/extraction_lab.py:40-47`), matching Graphiti 0.29.2 `RELEVANT_SCHEMA_LIMIT = 10` (`graphiti_core/search/search_utils.py:55-65`). The edge prompt is **not reference-time faithful** for historical replay: X-1. The lab also hard-codes `None` for entity types/exclusions/custom extraction instructions and an empty edge-type map (`src/menhir/explorer/extraction_lab.py:536-564`); whether Menhir production supplies non-default values is outside the six-file scope, so no equality claim is made for those inputs. `combined_extraction` is an explicit experimental arm, not the normal single-episode production call sequence. |
| `recall_lab.py` | Retrieval arms call the supplied `recall_service.recall()` directly with `RetrievalTuningConfig`; no production LLM prompt is reconstructed (`src/menhir/explorer/recall_lab.py:575-616`). Its only chat prompt is the **lab-only optional judge** (`src/menhir/explorer/recall_lab.py:119-236`). | **Retrieval call-through, prompt comparison N/A.** This file does not build the production answer/context prompt. The judge prompt measures retrieval quality and is not a production prompt. |
| `recall_packet_prototype.py` | Deterministically formats graph/retrieval material into packet text; there is no LLM invocation or model-output parser in the file (`src/menhir/explorer/recall_packet_prototype.py:151-205`, `:650-802`). | **No in-scope production model path to compare.** The packet is model-oriented text, but this file does not send it to a model. Any claim that it matches a production answer prompt would require a downstream caller outside scope. |
| `shadow_semantic_similarity_lab.py` | Produces exact message/candidate strings to embed, then scores vectors; no chat prompt or LLM response parser (`src/menhir/explorer/shadow_semantic_similarity_lab.py:45-173`, `:175-266`). | **N/A for chat-prompt fidelity.** It measures an embedding-based alternative. Whether the out-of-scope embedding runner uses the same provider/preprocessing as a production shadow path was not inferred. |
| `shadow_llm_judge_lab.py` | Builds a custom pairwise judge system/user prompt from current message and candidate fact (`src/menhir/explorer/shadow_llm_judge_lab.py:32-72`). | **Lab-specific, not production prompt construction.** This file contains no model call. The external runner named in comments/docstring is outside scope; its actual wiring was not assumed. |
| `shadow_contrastive_judge_lab.py` | Builds a custom contrastive prompt containing the current message and all candidate facts (`src/menhir/explorer/shadow_contrastive_judge_lab.py:47-94`). | **Lab-specific, not production prompt construction.** This file contains parsing/scoring but no model call; external runner wiring is outside scope. |

One suspected prompt mismatch was disproved at the locked dependency. The lab hard-codes `source_description="extraction_lab"` (`src/menhir/explorer/extraction_lab.py:508-516`), and Graphiti's node-extraction context carries `source_description` (`graphiti_core/utils/maintenance/node_operations.py:114-135`), but Graphiti 0.29.2's `extract_message()` prompt renders entity types, previous messages, current message and custom instructions — not `source_description` (`graphiti_core/prompts/extract_nodes.py:83-180`). That hard-coded field therefore is not, by itself, a node-prompt fidelity defect.

## Judge Failure Handling

| Judge / parser | Empty, malformed, refused, or failed response | How it enters aggregate/scoring |
|---|---|---|
| Extraction Lab fuzzy matcher | Missing backend/callable and API exceptions return `(0, 0)`. Empty/non-JSON/malformed JSON and a non-list `matches` field parse as `[]` (`src/menhir/explorer/extraction_lab.py:252-323`). A `None` value returned without raising can fail later during regex parsing and be caught by the outer arm exception instead. | **Counted as misses, not skipped**, whenever the failure returns zero pairs: recall/precision use zero fuzzy matches and unsupported count rises (`src/menhir/explorer/extraction_lab.py:384-433`). Backend-construction failure silently makes the run exact-only (`src/menhir/explorer/extraction_lab.py:936-954`). This is X-3. |
| Recall Lab optional judge | `_parse_judge_json()` raises on no JSON, malformed JSON, or schema violation. `_judge_pass()` also raises on missing backend or bad identifiers. `judge_recall_lab()` catches any pass failure and returns one `ok: false` judgment (`src/menhir/explorer/recall_lab.py:180-289`). | **No partial aggregate.** A failed pass does not become a score or a miss; the entire multi-pass judgment fails. Before judging, failed/degraded retrieval arms are excluded if at least two healthy arms remain; if fewer than two remain the judge reports `skipped: true` (`src/menhir/explorer/recall_lab.py:618-661`). Top-level arm results still expose the failed/degraded arms. |
| Pairwise shadow judge | `None`/empty or text containing neither recognized token returns `None`. However parsing is substring-based: after checking `NO_MATCH`/`NO MATCH`, **any** response containing `MATCH` becomes `True` (`src/menhir/explorer/shadow_llm_judge_lab.py:73-86`). Thus `not a match` becomes `True`; a refusal that happens to mention the allowed labels can be coerced to a real class. | `confusion_counts()` explicitly counts true `None` values as `unparseable` and skips them from TP/FP/TN/FN; `precision_recall_f1()` uses only TP/FP/FN (`src/menhir/explorer/shadow_llm_judge_lab.py:95-118`). Operational parser→runner wiring is outside the six-file corpus, so the substring hazard is documented but not promoted to a finding under the reachability rule. |
| Contrastive shadow judge | `None`, no JSON object, invalid JSON, or a non-list `selected_candidate_ids` becomes `selected_ids=None` (`src/menhir/explorer/shadow_contrastive_judge_lab.py:181-217`). | `score_contrastive_result()` returns `None` for that failure instead of a zero score (`src/menhir/explorer/shadow_contrastive_judge_lab.py:232-249`). The aggregate denominator is not present in this file, so whether `None` is excluded, counted as failure, or reported separately is an Open Question rather than an inferred finding. |
| Semantic-similarity / recall-packet modules | No LLM judge response is parsed in these files. | N/A. |

## Scoring Edge Cases

### Extraction Lab

- `_scored_set_comparison()` guards all normal denominators (`src/menhir/explorer/extraction_lab.py:384-433`). Empty gold + empty extraction returns recall=1.0 and precision=1.0. Empty gold + non-empty extraction returns both 0.0. Non-empty gold + empty extraction returns both 0.0. Otherwise it divides by non-empty list lengths.
- Exact matching is one-to-one even with duplicate normalized strings: each gold index can be consumed once (`src/menhir/explorer/extraction_lab.py:395-410`). Fuzzy matching separately enforces in-range indices and one-to-one use (`src/menhir/explorer/extraction_lab.py:306-323`).
- `unsupported_inference_rate` explicitly returns 0.0 when there are no extracted items, avoiding division by zero (`src/menhir/explorer/extraction_lab.py:360-376`).
- A fuzzy-judge failure is **not** excluded from denominators; it is represented as zero fuzzy matches (X-3).

### Recall Lab judge

- `judge_passes` is constrained to 1 or 2, so `average_scores` cannot divide by an empty `judgments` list through the validated request path (`src/menhir/explorer/recall_lab.py:65-81`, `:260-304`).
- An explicit tie is represented by `winner_id=None` and increments `tie_votes`. A winner must have a strict majority (`max_votes > len(judgments) / 2`); otherwise `winner_id=None` (`src/menhir/explorer/recall_lab.py:260-289`). With one winner vote and one explicit tie in a two-pass run, no arm has a majority and `tied_ids` expands to all arm IDs because there is only one vote leader. This is conservative unresolved handling, not a fabricated win.
- Failed/degraded retrieval arms are removed before judging when at least two healthy arms remain (`src/menhir/explorer/recall_lab.py:618-674`). They are therefore **skipped from the judge comparison**, not scored as retrieval misses; their failure remains visible in the top-level `arms` payload. Whether a cross-run benchmark later treats those skipped arms as failures is outside this file.
- Duplicate ordinal identifiers can pass the set-based response check: X-6.

### Semantic-similarity lab

- Empty/invalid/missing vectors map to 0.0 instead of missing: X-5 (`src/menhir/explorer/shadow_semantic_similarity_lab.py:175-207`).
- With an empty `scored_rows` list and the default 41 thresholds, every curve point has precision=1.0, recall=0.0, F1=0.0; no division by zero occurs (`src/menhir/explorer/shadow_semantic_similarity_lab.py:221-251`).
- `num_thresholds` itself is not validated. `num_thresholds=1` reaches `i / (num_thresholds - 1)` and raises division by zero; `num_thresholds<=0` produces no points, after which `best_f1_point([])` would raise on `max()` if called (`src/menhir/explorer/shadow_semantic_similarity_lab.py:221-255`). No in-scope/default caller passes those values, so this is not promoted beyond an edge-case defect.
- `best_f1_point()` uses Python `max()` with only F1 as the key. Equal-F1 ties therefore select the first point in the supplied list; the lab-generated curve is ascending by threshold, so its own tied best-F1 choice is the **lowest** threshold among tied points (`src/menhir/explorer/shadow_semantic_similarity_lab.py:221-255`).
- `category_summary([])` returns `{}` without division (`src/menhir/explorer/shadow_semantic_similarity_lab.py:257-266`).

### Pairwise shadow judge

- True unparseable rows are excluded from TP/FP/TN/FN while retained in a separate `unparseable` count (`src/menhir/explorer/shadow_llm_judge_lab.py:95-109`). Precision/recall/F1 do not include that count (`src/menhir/explorer/shadow_llm_judge_lab.py:112-118`). Thus an all-unparseable set yields TP=FP=FN=0, reported precision=1.0, recall=0.0, F1=0.0 plus the separate non-zero `unparseable` field. Consumers must inspect both.
- Substring coercion can prevent a malformed/refused answer from reaching the `unparseable` bucket at all (`src/menhir/explorer/shadow_llm_judge_lab.py:73-86`). Actual runner reachability remains out of scope.

### Contrastive shadow judge

- A valid empty `selected_candidate_ids: []` is a real scored result, not a parse failure: it fails `true_positive_preserved` when a positive exists and selects no negative (`src/menhir/explorer/shadow_contrastive_judge_lab.py:202-217`, `:232-249`).
- Unknown selected IDs are included in `selected_ids - true_ids`, so they set `any_negative_selected=True`; they are not silently treated as valid positives. `selected_categories` omits unknown IDs, but the negative flag still catches them (`src/menhir/explorer/shadow_contrastive_judge_lab.py:232-249`).
- Parse/call failures produce `None`; aggregate denominator treatment is outside this file.

### Recall packet prototype

No precision, recall, F1, similarity curve, or judge aggregate is computed in `recall_packet_prototype.py`. Its relevant edge cases are selection/budget formatting, not the scoring arithmetic asked in this audit.

## Disproved Candidates

1. **“Extraction Lab hand-copies the production Graphiti extraction prompt.” — Disproved.** Normal arms call Graphiti's real extraction functions (`src/menhir/explorer/extraction_lab.py:536-564`), and patched arms call Graphiti's locked `extract_message(context)` first, then edit the returned user prompt (`src/menhir/explorer/extraction_lab.py:755-851`). Graphiti 0.29.2 `extract_nodes` calls `prompt_library.extract_nodes.extract_message(context)` for message episodes (`graphiti_core/utils/maintenance/node_operations.py:256-278`). X-1 is a context-value mismatch, not a copied-template drift finding.

2. **“The lab's `source_description='extraction_lab'` changes the Graphiti node extraction prompt.” — Disproved at Graphiti 0.29.2.** The value exists in the constructed episode (`src/menhir/explorer/extraction_lab.py:508-516`) and Graphiti includes it in an internal context dictionary, but `extract_message()` does not render that key (`graphiti_core/prompts/extract_nodes.py:83-180`).

3. **“Recall Lab silently assigns scores when the optional judge refuses or returns malformed JSON.” — Disproved.** Parser/schema/backend errors propagate to `judge_recall_lab()`'s catch and produce `ok:false`; no winner or average scores are synthesized (`src/menhir/explorer/recall_lab.py:180-289`). X-6 is narrower: a JSON response with a duplicate ranking can still satisfy the set-based invariant.

4. **“Extraction set scoring has ordinary empty-set divide-by-zero.” — Disproved.** Empty gold/extracted cases and `total_extracted == 0` are explicitly handled (`src/menhir/explorer/extraction_lab.py:360-376`, `:416-433`). The scorer's problem is failure provenance (X-3), not denominator zero.

5. **“Contrastive judge silently accepts unknown selected candidate IDs as valid selections.” — Disproved.** Unknown IDs remain in `selected_ids - true_ids` and therefore set `any_negative_selected=True` (`src/menhir/explorer/shadow_contrastive_judge_lab.py:232-249`).

6. **“`recall_packet_prototype.py` directly exposes a prompt-injection path to a model.” — Not supported in this slice.** The module renders model-oriented text, but contains no model call. A downstream model consumer would be required to establish that reachability. The already-known unreferenced `_relevant` helper at line 475 was not re-filed.

7. **CF-70 and CF-55 were not re-filed.** CF-70 supplies programme-level Explorer exposure context; this audit does not duplicate its missing tier enforcement/default-enabled finding. No new six-file path was found that changes CF-55's `[UTC]` parser defect into a distinct lab finding.

## Open Questions

- **Pairwise shadow judge runner reachability.** `parse_judge_response()` accepts substrings rather than an exact token (`src/menhir/explorer/shadow_llm_judge_lab.py:73-86`), so responses such as `not a match` can become `True`. This module does not call the model or wire parser output into `JudgedRow`; that runner is outside the six-file corpus. Under the evidence contract, the parser defect is not promoted until the actual model→parser→aggregate chain is opened.
- **Contrastive failure denominator.** `score_contrastive_result()` returns `None` for parser/call failure, but no aggregate over multiple contrastive cases exists in scope (`src/menhir/explorer/shadow_contrastive_judge_lab.py:202-249`). The external runner must be checked before stating whether failed cases are skipped, counted as misses, or separately reported.
- **Shadow-judge prompt injection reachability.** Both shadow judge prompt builders interpolate message/candidate fixture text directly (`src/menhir/explorer/shadow_llm_judge_lab.py:63-72`; `src/menhir/explorer/shadow_contrastive_judge_lab.py:85-94`). Because their model callers are outside scope, this report does not promote those raw prompt surfaces as reachable injection findings. X-4 covers the two evaluator model calls that are fully present inside the six-file corpus.
- **Menhir production extraction arguments.** The lab passes `None` for entity types, excluded types, and custom extraction instructions and supplies an empty default edge-type map (`src/menhir/explorer/extraction_lab.py:536-564`). Graphiti production accepts caller-provided values for those arguments (`graphiti_core/graphiti.py:1084-1135`). Determining whether Menhir's production caller actually supplies non-default values would require opening out-of-scope Menhir production files; no assumption is made here.
- **Cross-request/global-prompt process topology.** X-2 proves that simultaneous invocations in one Python process are unsafe. Whether Explorer requests can overlap in the same process, and whether production extraction shares that process-global Graphiti prompt library, belongs to routing/runtime code outside this slice. No production cross-talk claim is made without that evidence.
- **Threshold sweep non-default arguments.** `precision_recall_curve(num_thresholds=1)` divides by zero and `best_f1_point([])` raises, but no in-scope/default caller passes those shapes (`src/menhir/explorer/shadow_semantic_similarity_lab.py:221-255`). Runner configuration would determine practical reachability.
- **Fixture provenance and poisoning controls.** The shadow labs import fixture builders from files outside the six-file corpus. This audit did not inspect how those fixtures are admitted, reviewed, or mutated. Prompt-injection risk from the in-scope evaluator prompts is assessed from their input interfaces; the actual provenance of the committed shadow fixtures remains outside scope.

## Coverage Table

The Menhir source corpus was exactly the six requested files. No other Menhir source file or prior audit report was read for findings. Locked Graphiti 0.29.2 dependency files were opened only to verify the lab's production-prompt delegation/reference-time semantics and are not part of this line corpus.

| File | Measured physical lines | Coverage |
|---|---:|---|
| `src/menhir/explorer/extraction_lab.py` | 957 | Full file; extraction construction, Graphiti delegation, prompt patching, fuzzy judge, scoring, failure handling, concurrency |
| `src/menhir/explorer/recall_packet_prototype.py` | 802 | Full file; packet rendering, selection/budget behavior; no model call/scoring found |
| `src/menhir/explorer/recall_lab.py` | 686 | Full file; production recall call-through, optional LLM judge prompt/parser, aggregation, failed-arm handling |
| `src/menhir/explorer/shadow_semantic_similarity_lab.py` | 266 | Full file; fixture-row shaping, cosine behavior, missing embeddings, PR/F1 curve, ties/empty cases |
| `src/menhir/explorer/shadow_contrastive_judge_lab.py` | 249 | Full file; contrastive prompt, parsing, failure sentinel, scoring |
| `src/menhir/explorer/shadow_llm_judge_lab.py` | 135 | Full file; pairwise prompt, substring parser, confusion counts, PR/F1, skipped unparseables |
| **Total** | **3,095** | **Reconciles exactly: 957 + 802 + 686 + 266 + 249 + 135 = 3,095** |

**Line-count method.** Pinned-SHA connector reads were extended to EOF for each file; the last returned source line establishes the counts above. A shell `wc -l` verification was also attempted as requested, but the shell's GitHub checkout failed before source retrieval with `Could not resolve host: github.com`; consequently the `wc` command had no files to count. I therefore report the connector-measured EOF counts, not a fabricated `wc` result.

**External dependency verification used for question 1:** `getzep/graphiti` tag `v0.29.2`, specifically `graphiti_core/graphiti.py`, `graphiti_core/utils/maintenance/node_operations.py`, `graphiti_core/utils/maintenance/edge_operations.py`, `graphiti_core/prompts/extract_nodes.py`, `graphiti_core/prompts/extract_edges.py`, and `graphiti_core/search/search_utils.py`. These were not included in the Menhir source count.

## Review Confidence /100

**95/100.** Confidence is high for the six-file static findings: the corpus reconciles exactly to 3,095 lines, finding citations were re-derived from pinned-SHA source reads, and the prompt-fidelity conclusion was checked against Graphiti 0.29.2 implementation rather than Menhir comments. Confidence is not 100 because the requested shell `wc -l` could not complete due DNS, no runtime/model experiment was executed, and the actual runners/aggregators for the two shadow judge modules are intentionally outside the six-file scope. Those gaps are kept as Open Questions rather than converted into findings.
