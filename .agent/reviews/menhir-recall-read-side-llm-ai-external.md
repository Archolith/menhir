# Menhir Recall / Read-Side LLM & AI Security Audit

**Repository:** `Archolith/menhir`  
**Source revision audited:** `db31ebaef0bb9bd5a650429f7b850e28087904a5` (`db31eba`)  
**Audit mode:** read-only static review; this report is the only file created  
**Primary read-side corpus:** 9 files / **5,186 physical source lines** under `src/menhir/services/` (coverage table below)  
**Execution:** none. No reproduction claims are made.

## Executive Summary

The default recall path has one strong read-time trust control and one important missing one. The strong control is staged review: `CANDIDATE`-scope entities are unconditionally removed before scoring (`src/menhir/services/recall_pipeline.py:560-564`). The missing control is origin provenance after that admission decision. Generic entity recall hydrates scoring metadata from a projection that omits `source`, `sources`, and `source_confidence`; temporal fact enrichment likewise carries `r.fact` but not `fact_source` (`src/menhir/infrastructure/cypher.py:299-327`, `:330-337`). The default scorer then ranks on retrieval similarity, adjacency, recency, prominence, conflict, and type boost only (`src/menhir/services/scoring_service.py:163-170`). The agent-context renderer labels authority/conflict/warden state where those features apply, but does not label whether ordinary recalled text was human-asserted, LLM-extracted, LLM-repaired, or otherwise synthetic (`src/menhir/services/context_builder.py:397-424`). **Direct answer to question 1: once a generic memory has crossed the admission boundary into a recallable scope, the default read path does not distinguish human-asserted from machine-generated origin when ranking or rendering it.** The exceptions are special/opt-in paths described below; they do not change that default-path conclusion.

Four findings are supported by code. **R-1 (High)** is the most serious: `build_context` is a `readonly` tool whose namespace defaults to empty; an unpinned client converts that to `None`. Menhir passes that `None` as Graphiti `group_ids`; the locked Graphiti 0.29.2 implementation only adds its node group predicate when `group_ids` is non-`None`. Menhir's own defense-in-depth metadata namespace filter is likewise conditional on an explicit namespace. A recallable memory in another namespace that ranks for the query can therefore be returned through the normal context renderer. Client namespace pinning blocks this when configured, but `client_namespaces` defaults to an empty mapping. This is a code-proven cross-namespace read path, not an inference from a comment or symbol name.

**R-2 (Medium)** is selection poisoning: on the shipped default path, a stored entity's Graphiti search representation participates in its own candidate retrieval score. The locked Graphiti version runs node BM25 against its `node_name_and_summary` index and node cosine against `n.name_embedding`; Menhir treats the resulting fused score as similarity, adds it directly to final relevance, sorts on that score, and keeps only bounded candidate/result sets. A planted recallable entity whose stored name/summary representation is crafted to match later target queries can therefore improve its own chance of selection and indirectly crowd competitors. The stronger variant—Menhir's separate `n.content_embedding` lane—is experimental/default-off. This finding is about self-promotion through stored search material, not a claim that one candidate can directly decrement another candidate's score.

**R-3 (Medium)** is limited to the shadow context-composition research path. Stored fact text and endpoint names are inserted without escaping into the shadow model's user prompt. The prompt uses labels and a separate system role, and the candidate count is capped at 30, but there is no per-fact text cap or structural encoding of fact data. An instruction-shaped stored fact can therefore influence the shadow classifier or tie-break judgement. Reachability materially limits the issue: `shadow_context_composition` defaults to `False`, dispatch is detached after real ingest, and the prediction is logged as lifecycle telemetry rather than used to change the production extraction/write (`src/menhir/services/enrichment_steps.py:96-100`, `:673-685`, `:700-744`). Under the supplied severity rubric this is Medium because, if enabled, the content can influence a non-gating model judgement; it is not High because the judgement has no production action or trusted write effect.

**R-4 (Low)** is the provenance gap itself: the graph stores provenance fields and the frontier path can derive evidence families, but ordinary recall discards those origin fields before scoring and does not render them. This is Low under the supplied calibration because it is a provenance/trust-signalling defect rather than, by itself, a control defeat. It materially increases the poisoning value of R-2.

Cross-session persistence is also clear but is not filed separately. The runtime resolves an effective session ID and passes it to `ContextBuilderService`, but the builder does not pass it into `RecallService.recall` (`src/menhir/core/backend_runtime_data_ops.py:220-228`; `src/menhir/services/context_builder.py:211-229`). Generic recall defaults `include_session=False`, so `SESSION` memories are excluded rather than scoped to the current session (`src/menhir/services/recall_service.py:107-125`; `src/menhir/services/recall_pipeline.py:558-568`). Durable `PERSISTENT` memory is therefore intentionally cross-session. A planted durable memory can reappear in later sessions in the same namespace; the containment boundary that matters is namespace, and R-1 shows that boundary is opt-in for unpinned callers.

No Critical finding is supported. CF-39 and CF-55 were treated as established context and not re-filed. I also did not intentionally re-file CF-63, CF-74, or CF-75; their descriptions are not present in the supplied brief or the repository material available to this review, so register-owner de-duplication is still required before promoting any R-number here into the programme-wide CF register.

## Findings

### High

### R-1 — Unpinned `readonly` `build_context` defaults to a cross-namespace search

**Severity: High**

**Triggering content / payload shape.** A recallable entity in namespace **B** (for example, a `PERSISTENT` memory whose stored search representation is relevant to the query) plus a caller in a different namespace/trust context invoking `build_context(query=..., namespace="")` or omitting `namespace`. The caller must be unpinned: no `MENHIR_CLIENT_NAMESPACES` mapping for its client name.

**Code path.** The MCP tool is explicitly `readonly`; its endpoint declares `namespace: str = ""` and sends `namespace=namespace or None` to the backend (`src/menhir/mcp/tools/recall/build_context.py:70-93`). `BaseTool.execute()` applies a server-side namespace override only when `get_pinned_namespace()` returns a non-empty value; otherwise `_apply_pinned_namespace()` returns the original arguments unchanged (`src/menhir/mcp/contracts.py:282-302`, `:325-344`). The settings default for those pins is an empty mapping (`src/menhir/config/settings_model.py:326-333`). The backend resolves a session ID and passes both session and namespace to the context builder (`src/menhir/core/backend_runtime_data_ops.py:220-228`), but the builder's actual recall call sends only `query`, `preset`, `namespace`, and `include_invalidated=True` (`src/menhir/services/context_builder.py:211-229`). Recall translates the namespace with `group_ids = namespace_to_group_ids(namespace)` (`src/menhir/services/recall_pipeline.py:135-140`); the implementation returns `None` when the namespace is `None` (`src/menhir/domain/namespace.py:47-58`). Menhir's Graphiti wrapper then passes that value unchanged as `group_ids=group_ids` to `self.client.search_()` while configuring both BM25 and cosine node search (`src/menhir/infrastructure/graphiti_client.py:926-980`). The lockfile pins `graphiti-core` 0.29.2 (`uv.lock:579-582`). In that exact dependency, `search()` normalizes empty group IDs to `None` (`graphiti_core/search/search.py:154-155`), and both `node_fulltext_search()` and `node_similarity_search()` append `n.group_id IN $group_ids` **only** under `if group_ids is not None` (`graphiti_core/search/search_utils.py:600-602`, `:689-691`, tag `v0.29.2`). Finally, Menhir's metadata defense-in-depth namespace equality filter is also conditional on `if namespace is not None` (`src/menhir/services/recall_pipeline.py:582-589`). If the foreign entity ranks into the result set, its name/content is rendered into the context (`src/menhir/services/context_builder.py:397-424`) and the MCP wrapper returns that context (`src/menhir/mcp/tools/recall/build_context.py:100-107`).

**Reachability chain.** Entry point: `build_context` MCP tool, minimum tier `readonly` (`src/menhir/mcp/tools/recall/build_context.py:70-93`). Gate: server-side namespace pinning in `BaseTool._apply_pinned_namespace`; default configuration is no pins (`src/menhir/mcp/contracts.py:282-302`; `src/menhir/config/settings_model.py:326-333`). Default when gate is absent: empty tool argument becomes `None`; Menhir passes `group_ids=None`, and the locked Graphiti implementation omits both BM25 and cosine group predicates (`src/menhir/infrastructure/graphiti_client.py:926-980`; `graphiti_core/search/search_utils.py:600-602`, `:689-691`, tag `v0.29.2`). Effect if reached: matching recallable content from another namespace can be returned in agent context.

**Why High.** The supplied severity calibration defines cross-tenant leakage through retrieval as High. This finding establishes cross-namespace leakage. Where Menhir namespaces are used as the client/tenant isolation boundary, that is the calibrated High case.

**Important limit.** A pinned client cannot escape its configured namespace through this tool because the contract layer forcibly overwrites the argument before invoking the endpoint (`src/menhir/mcp/contracts.py:282-302`). Deployment configuration was not available, so this review does not claim every deployed client is exposed; it establishes the default reachable path for an unpinned `readonly` caller.

### Medium

### R-2 — A stored entity can promote its own recall through its search representation and indirectly crowd competitors

**Severity: Medium**

**Triggering content / payload shape.** A recallable stored entity whose **name/summary search representation** is deliberately shaped to overlap a later target query—for example, a stored entity name or summary using the vocabulary of a high-value future task. It must already be in a recallable scope; `CANDIDATE` entities do not qualify because they are removed before scoring.

**Code path.** Menhir's default `search_scored()` config runs both Graphiti node BM25 and cosine and RRF-fuses them (`src/menhir/infrastructure/graphiti_client.py:926-980`). The lockfile pins Graphiti 0.29.2 (`uv.lock:579-582`). In that dependency, node BM25 calls the `node_name_and_summary` fulltext index (`graphiti_core/search/search_utils.py:644-647`, tag `v0.29.2`), while node cosine explicitly scores `n.name_embedding` (`graphiti_core/search/search_utils.py:757-764`, tag `v0.29.2`). Menhir records the returned score as the candidate `similarity` (`src/menhir/services/recall_pipeline.py:590-604`). `ScoringService` adds that similarity directly to adjacency, recency, prominence, conflict, and type terms (`src/menhir/services/scoring_service.py:163-170`), sorts descending on `final_score` (`src/menhir/services/scoring_service.py:207-231`), and recall takes a bounded top slice after optional frontier processing (`src/menhir/services/recall_pipeline.py:1430-1465`). The service defaults are `limit=10` and `candidate_k=50` (`src/menhir/services/recall_service.py:107-125`).

**Reachability chain.** Entry point: ordinary `RecallService.recall` / `build_context`. Gate: entity must survive structural/freshness/review/session filters; in particular `scope == CANDIDATE` is dropped (`src/menhir/services/recall_pipeline.py:552-568`). Default search gate: Menhir's separate `CONTENT_VECTOR` lane over `n.content_embedding` is experimental/default-off, so this finding is limited to the stored representation used by Graphiti's default BM25/cosine search, not arbitrary `n.content` on Menhir's optional content-vector path (`src/menhir/domain/retrieval_tuning.py:41-47`; `src/menhir/config/settings_model.py:297-314`). Effect if reached: the planted entity can raise its own retrieval/final rank and consume one of the bounded candidate/result positions, indirectly displacing lower-ranked competitors.

**Why Medium.** This is influence on a non-gating retrieval judgement, matching the supplied Medium calibration. There is no code path here for one memory to directly reduce another candidate's score; “suppress competitors” is only the ordinary consequence of occupying bounded top-k slots.

**Interaction with R-4 and CF-39.** The attack value is higher because default generic scoring has no assertion-origin trust penalty (R-4), and CF-39 already establishes that selected memory text is rendered verbatim into operator agent context. CF-39 is not re-filed here.

### R-3 — Shadow composition inserts stored facts into a model prompt without escaping or per-fact length bounds

**Severity: Medium**

**Triggering content / payload shape.** A stored candidate fact (or endpoint name) containing instruction-shaped text, e.g. a `fact_text` that tells the model to ignore the classification task or emit a chosen `fact_uuid`. The fact must be retrieved into the shadow candidate snapshot, and shadow context composition must be enabled.

**Code path.** Shadow retrieval copies `fact_text`, `source_name`, and `target_name` from graph rows into `ShadowCandidateFact`; candidate facts are capped by count at 30 (`src/menhir/services/shadow_context_composition.py:53-65`, `:249-305`). `_candidate_payload()` forwards those values unchanged (`src/menhir/services/shadow_context_composition.py:552-556`). `compose_shadow_prediction()` dynamically resolves `classify_shadow_context` and calls it with the current episode plus those payloads (`src/menhir/services/shadow_context_composition.py:431-444`). A genuine tie similarly calls `break_shadow_tie` with surviving candidate payloads (`src/menhir/services/shadow_context_composition.py:660-667`). The LLM adapter constructs lines as `fact_uuid: source_name — fact_text — target_name` and concatenates them under the textual heading `REAL CANDIDATE FACTS`; it caps the current message at 2,000 characters but does not cap or escape each stored fact (`src/menhir/infrastructure/llm.py:87-100`). The tie prompt does the same under `TIED CANDIDATES` (`src/menhir/infrastructure/llm.py:120-130`). The calls do use separate system and user roles (`src/menhir/infrastructure/llm.py:351-380`, `:382-397`), so there is instruction/data separation at the role level between Menhir's system instructions and the whole user payload, but not structural separation between current-message text and stored-fact data inside that user payload.

**Reachability chain.** Entry point: ingest enrichment. Gate: `EnrichmentContext.shadow_context_composition: bool = False` (`src/menhir/services/enrichment_steps.py:96-100`). When enabled, dispatch occurs after the real ingest gate has been released and is detached from real episode completion (`src/menhir/services/enrichment_steps.py:673-685`). `_dispatch_shadow_composition()` creates a background task; `_run_shadow_composition_and_log()` runs the model and records an `ingest_shadow` lifecycle event (`src/menhir/services/enrichment_steps.py:700-744`). Effect if reached: stored content can influence only the shadow classification/tie-break prediction and telemetry trace.

**Why Medium, not High.** The supplied rubric puts influence on a non-gating judgement at Medium. The shadow result does not gate extraction, modify the graph, or trigger an external action on this path; the feature is default-off and observe-only. That reachability distinction is explicit here to avoid grading the symbol as though it were production control flow.

### Low

### R-4 — Generic recall drops assertion-origin provenance before ranking and does not label it in agent context

**Severity: Low**

**Triggering content / payload shape.** Two recallable generic memories/facts with equivalent retrieval/ranking attributes but different origin—for example one human-asserted and one LLM-extracted or LLM-repaired. The content has already passed any admission decision and is no longer `CANDIDATE` scope.

**Code path.** Menhir's broader memory return projection contains `n.source`, `n.sources`, and `n.source_confidence` (`src/menhir/infrastructure/cypher.py:270-296`). The **recall scoring** projection `ENTITY_METADATA_FIELDS`, however, contains name/scope/type/content/summary and relevance/lifecycle fields but omits all three origin fields (`src/menhir/infrastructure/cypher.py:299-327`); `fetch_candidate_metadata()` is built directly from that projection (`src/menhir/infrastructure/memory_queries.py:281-300`). Likewise the temporal-fact projection contains `r.fact` and time fields but no `fact_source` (`src/menhir/infrastructure/cypher.py:330-337`), and the shadow fact-edge projection contains fact text/endpoints/time but no origin field (`src/menhir/infrastructure/cypher.py:344-355`). Generic candidate construction therefore receives no assertion-origin field; its `source` is instead a `CandidateSource` describing **retrieval admission** (VECTOR/BM25/PENDING/FILE_LINKED/etc.), not who asserted the content (`src/menhir/services/recall_pipeline.py:590-620`; `src/menhir/domain/retrieval_tuning.py:28-47`). The default score formula has no origin/trust term (`src/menhir/services/scoring_service.py:163-170`). The generic context markers are scalar authority, superseded view, warden label, and conflict only; origin is not rendered (`src/menhir/services/context_builder.py:397-424`). Temporal fact text is rendered with source/world time and belief role, but not `fact_source` (`src/menhir/services/context_builder.py:136-155`).

**Reachability chain.** Entry point: ordinary recall/build_context. Gate: the content must be in a recallable scope. Always-on staged review does remove `CANDIDATE` scope before ranking (`src/menhir/services/recall_pipeline.py:560-564`), so this finding does **not** claim there is no read-time trust control whatsoever. Once admitted, the default generic scorer has no human-vs-machine origin signal. Effect if reached: human-asserted and machine-generated generic memories are ranked and displayed equivalently when their normal relevance attributes are equivalent.

**Optional/special-case controls do not clear the default-path gap.** Runtime frontier defaults set oracle ranking, warden gating, belief gating, evidence anchoring, and shadow tracing off (`src/menhir/config/settings_model.py:297-314`). When a frontier portion is active, recall can fetch derived `evidence_kinds` and project provenance (`src/menhir/services/recall_pipeline.py:1395-1407`; `src/menhir/services/recall_support.py:683-735`). `EvidenceOracle` exists, but the actual `default_oracles()` code list contains Semantic, Structure, Scope, Temporal, and Intent—not EvidenceOracle (`src/menhir/services/retrieval_oracles.py:278-299`). Special scalar/event authority features also default off (`src/menhir/services/recall_service.py:93-103`). These are real provenance-aware seams, not default generic ranking/rendering controls.

**Why Low.** Under the supplied calibration, this is a provenance/trust-labelling gap. Its security significance comes from composition with poisoning/retrieval behavior such as R-2 rather than from an independent trusted sink.

## Provenance At Read Time

### Direct answer

**Default generic Entity/fact recall does not retain an origin-trust distinction after admission.** A machine-generated generic memory and a human-asserted generic memory that have the same ordinary retrieval/ranking attributes are selected and rendered the same way. The read path does carry several things named “source,” but they are not equivalent:

- `CandidateSource` means **how recall found/admitted the candidate** (vector, BM25, pending, file-linked, facet, etc.), not who asserted the memory (`src/menhir/domain/retrieval_tuning.py:28-47`).
- `RetrievalScoreKind` describes the retrieval score's scale/provenance, not content trust (`src/menhir/domain/retrieval_tuning.py:69-75`).
- Stored entity `source` / `sources` / `source_confidence` exist in broader graph projections, but are absent from `ENTITY_METADATA_FIELDS`, which is the projection used by ordinary scoring (`src/menhir/infrastructure/cypher.py:270-327`; `src/menhir/infrastructure/memory_queries.py:281-300`).
- Temporal `r.fact` is fetched without `fact_source` (`src/menhir/infrastructure/cypher.py:330-337`).
- Context rendering exposes no generic origin label (`src/menhir/services/context_builder.py:397-424`).

### Always-on distinctions that do exist

This is not a claim that recall is trust-blind in every dimension. The following filters are real code-enforced controls before scoring: structural nodes are dropped, `CANDIDATE` review-tier nodes are dropped, `GONE` freshness is dropped, `SESSION` scope is dropped by default, superseded Views are dropped unless requested, and disabled scalar-history Views are dropped (`src/menhir/services/recall_pipeline.py:552-581`). The most important provenance-adjacent one is `CANDIDATE`: staged-review content does not influence ranking until promoted.

Those controls answer a different question from human-vs-machine origin. Once a node is recallable, ordinary scoring receives no `source_confidence` or assertion-origin tier.

### Optional/special trust signals

The frontier path can derive `evidence_kinds` from `SUPPORTED_BY :Evidence`, structural file anchors, and episode sources (`src/menhir/infrastructure/memory_queries.py:337-375`; `src/menhir/services/recall_support.py:683-735`). Warden gating can then remove refused candidates, but it is opt-in and defaults off (`src/menhir/config/settings_model.py:297-314`; `src/menhir/services/recall_support.py:631-648`). Scalar authority can carry foundation/evidence information in its special typed-assertion path, and the context renderer can mark a scalar as `current authority`; that feature defaults off (`src/menhir/services/recall_service.py:93-103`). These are exceptions, not evidence that the default generic Entity/fact path ranks by provenance.

### Search/read corpus for this negative

No shell execution environment was available, so I did not claim a literal `grep` run. The equivalent static term-search/read corpus was the **entire 5,186-line primary corpus** listed in the Coverage Table, with targeted supporting reads of the projection, domain, MCP contract, namespace, graph-adapter, and locked Graphiti search files. The provenance terms checked semantically/literally included `fact_source`, `source`, `sources`, `source_confidence`, `evidence_tier`, `evidence_kinds`, `foundation`, `warden`, `CandidateSource`, `CANDIDATE`, `user_flagged`, `bootstrap_scope`, and synthetic/self-source concepts. Conclusions above are based on the actual field projections and consumers, not on absence from an index.

## Content-To-Context Trace

The following are the stored/external-content joins found on the two requested composition surfaces. CF-39 is treated as already confirmed and is not assigned a new R-number. Code fragments below quote the join expression rather than the surrounding comments.

| Surface | Stored/external value reaching text | Exact join site | Boundary present | Gate / default |
|---|---|---|---|---|
| Generic recalled memory -> agent context | `mem.name`, `mem.content` | `line = f"[Memory {idx}] {name_with_markers}: {content}"` (or score-bearing variant), `src/menhir/services/context_builder.py:397-424` | Textual `[Memory N]` prefix and aggregate token budget; **no escaping and no per-memory content cap**. This is the already-confirmed CF-39 sink. | Ordinary `build_context`; active by default when results exist. |
| Temporal supporting fact -> same memory block | `temporal_fact.fact` | `lines.append(f"  - {happened} | {fact} | belief: {belief_role}")`, `src/menhir/services/context_builder.py:136-155` | `Source-time evidence:` label plus timestamp/belief-role separators; no escaping or per-fact char cap. Fact source/origin is not rendered. Aggregate memory-block token budget applies later. | Active when temporal facts were hydrated. |
| Scalar authority -> agent context | `verdict.attribute`, `verdict.value`, contributor `relation` and `stated_span` | `f"[Scalar authority: ...] {verdict.attribute} = {verdict.value} ..."` and `f"  - {contributor.relation}: {contributor.stated_span} ..."`, `src/menhir/services/context_builder.py:299-323` | Labeled authority block; aggregate token budget; no escaping of contributor wording. | `scalar_view_authority_enabled=False` by default (`src/menhir/services/recall_service.py:93-97`). |
| Event authority -> agent context | predicate/object, `time_basis`, domain, `stated_span` | `event_lines.append(f'  - source quote: "{verdict.stated_span}"')` plus heading/field joins, `src/menhir/services/context_builder.py:333-388` | Labeled authority/advisory block; `stated_span` is wrapped in quotes but not escaped/encoded; aggregate token budget. | `event_history_authority_enabled=False` by default (`src/menhir/services/recall_service.py:100-103`). |
| Stale-anchor metadata -> agent context | `stale_info["path"]` | `f"  ⚠️ Stale file anchor: {stale_path} ..."`, `src/menhir/services/context_builder.py:431-462` | Fixed advisory prefix; atomic budget with memory; no escaping of path. | Only when a recalled memory has `stale_anchor_info.stale_anchor`. |
| Stored TODO -> agent context | TODO `content`, priority, code ref | `todo_lines.append(f"- [{tag}]{ref} {snippet}")`, `src/menhir/services/context_builder.py:235-258` | Up to 3 matches; each content snippet capped to 80 chars; labeled `Related open TODOs`; no escaping. Cost reserved from aggregate budget. | Active when graph adapter exists and query is non-empty. |
| Supplementary timeline -> agent context | temporal fact text or memory content/name | context builder calls `render_bundles([timeline], ...)`, `src/menhir/services/context_builder.py:480-493`; renderer joins `body = "\n".join(b.lines)`, `src/menhir/domain/brief_builder.py:168-178` | `_MAX_LINE_CHARS = 240` and `_clip()` enforce a per-line cap (`src/menhir/domain/brief_builder.py:38`, `:53-55`); headings/chronology tags; no escaping. | `brief_builder_enabled=False` by default (`src/menhir/services/context_builder.py:205-209`). |
| Linked local wiki/reference document -> agent context | first bytes of linked file plus document name | `wiki_lines.append(f"- [[{doc['name']}]]{tag}: {content}")`, then `context += wiki_section`, `src/menhir/services/context_builder.py:519-561` | Reads first 200 characters per file; max 5 docs; wiki section budget capped to 30% of effective budget; text not escaped. | Requires graph adapter, recalled memory IDs, linked local path that exists. |
| Completed context -> MCP/tool result | `result["context"]` | `lines.append(str(result.get("context") or ""))`, `src/menhir/mcp/tools/recall/build_context.py:100-107` | Tool header/footer only; no further escaping of context body. | `readonly` tool. |
| Stored shadow fact -> shadow classifier model | candidate `source_name`, `fact_text`, `target_name` | `f"- fact_uuid={c['fact_uuid']!r}: {c['source_name']} — {c['fact_text']} — {c['target_name']}"`, then `f"REAL CANDIDATE FACTS:\n{lines ...}"`, `src/menhir/infrastructure/llm.py:87-100` | Separate system/user roles and textual `REAL CANDIDATE FACTS` delimiter. Current message capped to 2,000 chars; candidate count capped to 30 upstream; **no per-fact text cap or structural encoding/escaping**. | `shadow_context_composition=False`; observe-only. |
| Stored shadow fact -> shadow tie-break model | same fields for surviving candidates | same candidate f-string under `TIED CANDIDATES`, `src/menhir/infrastructure/llm.py:120-130` | Same role/textual separation; no per-fact escaping/cap. | Same default-off shadow gate; only 2+ survivors. |

### Shadow reachability and effect

Stored candidates reach the model only after `_candidate_payload()` copies raw fact/end-point text (`src/menhir/services/shadow_context_composition.py:552-556`) and the classifier/tie code invokes the LLM collaborator (`src/menhir/services/shadow_context_composition.py:431-444`, `:660-667`). Candidate count is bounded at 30 (`src/menhir/services/shadow_context_composition.py:53-65`). The actual LLM calls use Menhir's system prompt separately from the generated user prompt (`src/menhir/infrastructure/llm.py:351-380`, `:382-397`). That is a meaningful boundary, but it is not an escaping boundary around stored data embedded inside the user message.

The production gate/effect is equally important: `shadow_context_composition` defaults false (`src/menhir/services/enrichment_steps.py:96-100`), dispatch is a fire-and-forget task after real ingest has completed/released the gate (`src/menhir/services/enrichment_steps.py:673-710`), and the resulting trace is recorded as lifecycle telemetry (`src/menhir/services/enrichment_steps.py:711-744`). No code in that reachability chain feeds the shadow selection back into the production extraction result or graph write.

## Selection Influence

### What can influence its own selection on defaults

A stored Entity's Graphiti search representation can influence its own candidate score. Menhir's `search_scored()` enables both BM25 and cosine node methods (`src/menhir/infrastructure/graphiti_client.py:926-980`). In the locked Graphiti 0.29.2 implementation, node BM25 uses the `node_name_and_summary` index and node cosine uses `n.name_embedding` (`graphiti_core/search/search_utils.py:644-647`, `:757-764`, tag `v0.29.2`; lock at `uv.lock:579-582`). The search score becomes `CandidateData.similarity`, the scorer adds it directly to relevance, and results are sorted descending (`src/menhir/services/recall_pipeline.py:590-620`; `src/menhir/services/scoring_service.py:163-170`, `:207-231`). Because both candidate generation and final result count are bounded, self-promotion can indirectly displace competitors (R-2).

### What was not proven

I did **not** find a default Menhir `n.content_embedding` mechanism by which arbitrary rendered `content` directly changes its own search score. The dedicated content-vector lane exists precisely for that and is default-off (`src/menhir/domain/retrieval_tuning.py:41-47`; `src/menhir/config/settings_model.py:297-314`). I also did not find a candidate-controlled field that subtracts from a competitor's score. Competitor suppression is by rank/top-k displacement only.

Optional fact-edge retrieval can search on `EntityEdge.fact`, but that feature is also default-off (`src/menhir/config/settings_model.py:297-314`). The shadow composition path selects candidate facts using literal/semantic relation to the current episode, but it is observe-only and not the production recall ranker.

## Memory Poisoning Across Sessions

`build_context` exposes a `session_id`, and the backend substitutes the effective current session if the caller omits one (`src/menhir/core/backend_runtime_data_ops.py:214-228`). That value is passed into `ContextBuilderService.build_context`, but the builder's recall invocation does not forward it (`src/menhir/services/context_builder.py:211-229`). This is not a hidden path: generic `RecallService.recall` has no session-id argument; it has only the boolean `include_session`, default `False` (`src/menhir/services/recall_service.py:107-125`). The pipeline therefore drops `SESSION`-scope nodes unless that boolean is true (`src/menhir/services/recall_pipeline.py:558-568`).

Consequently:

- A claim that **SESSION-scope memories from an old session are leaking into default build_context** is disproved by the explicit `include_session=False` filter.
- A durable `PERSISTENT` memory is not constrained to the session that originated it. It is eligible in later sessions by design. A poison planted once and promoted/persisted can therefore reappear later if it ranks.
- Namespace is the meaningful durable containment boundary. When an explicit namespace is supplied, both Graphiti group filtering and a metadata namespace equality check apply (`src/menhir/services/recall_pipeline.py:135-140`, `:582-589`). When namespace is `None`, the locked Graphiti node search omits the group predicate and Menhir skips the metadata namespace equality check; that is R-1.

No separate finding is filed for cross-session persistence because persistent memory crossing sessions is the feature's intended semantics, not by itself a defeated control. The security consequence is the combination of durable admission + later ranking + context rendering.

## Lane Results

| Lane | Result |
|---|---|
| Static read-side code review | **RUN** — full 5,186-line primary service corpus, plus targeted supporting reachability/projection files. |
| Provenance field / consumer trace | **RUN** — entity projection, temporal fact projection, candidate construction, scoring, optional frontier provenance, and context markers traced end-to-end. |
| Content-to-agent-context trace | **RUN** — generic memory, temporal facts, authority blocks, stale metadata, TODO, timeline, wiki, and MCP wrapper traced. |
| Content-to-shadow-model trace | **RUN** — candidate snapshot -> payload -> prompt builder -> LLM call -> detached telemetry traced. |
| Selection-influence trace | **RUN** — default locked Graphiti BM25/cosine node search, optional Menhir content-vector distinction, scoring formula, sort, bounded slice traced. |
| Session / namespace containment trace | **RUN** — MCP defaults, pin contract, backend effective session, context builder, namespace conversion, Menhir metadata filter, and locked Graphiti group predicates traced. |
| Dynamic reproduction / adversarial payload execution | **NOT RUN — no execution environment.** |
| Garak | **NOT RUN — no execution environment.** |
| DeepTeam | **NOT RUN — no execution environment.** |
| Promptfoo | **NOT RUN — no execution environment.** |

No statement in this report attributes hypothetical results to the tools that were not run.

## Disproved Candidates

1. **“There is no read-time trust control at all.” — disproved.** `CANDIDATE` scope is unconditionally skipped before scoring (`src/menhir/services/recall_pipeline.py:560-564`). Structural/GONE/session/superseded controls also exist. R-4 is narrower: assertion **origin** is absent after admission on the generic path.

2. **“`CandidateData.source` is the memory's human/LLM provenance.” — disproved.** It is `CandidateSource`, the retrieval/admission lane (VECTOR/BM25/PENDING/FILE_LINKED/etc.) (`src/menhir/domain/retrieval_tuning.py:28-47`; `src/menhir/services/recall_pipeline.py:590-620`).

3. **“EvidenceOracle protects default recall ranking.” — disproved.** `default_oracles()` actually constructs Semantic, Structure, Scope, Temporal, and Intent oracles; it does not include `EvidenceOracle` (`src/menhir/services/retrieval_oracles.py:278-299`). The active frontier controls that could apply warden verdicts default off (`src/menhir/config/settings_model.py:297-314`).

4. **“Old SESSION memories are recalled merely because `build_context` carries a session_id.” — disproved.** The session ID is not forwarded to recall, and default recall drops `SESSION` scope (`src/menhir/services/context_builder.py:211-229`; `src/menhir/services/recall_pipeline.py:558-568`). The cross-session issue is durable `PERSISTENT` content, not old session-local nodes.

5. **“Arbitrary memory body text self-selects through Menhir's default content-vector lane.” — disproved.** The separate Menhir content-embedding lane is default-off (`src/menhir/domain/retrieval_tuning.py:41-47`; `src/menhir/config/settings_model.py:297-314`). R-2 is intentionally limited to the stored representation used by Graphiti's default BM25/cosine node search.

6. **“Shadow composition can change the production extraction/write.” — disproved on the inspected reachability chain.** It is gated off by default, dispatched after production ingest, and the background function records an `ingest_shadow` lifecycle trace (`src/menhir/services/enrichment_steps.py:96-100`, `:673-685`, `:700-744`). No return value from that background task is consumed by the real ingest path.

7. **“The shadow prompt has no boundary at all.” — disproved.** It uses separate system and user messages plus explicit `CURRENT MESSAGE` / `REAL CANDIDATE FACTS` / `TIED CANDIDATES` textual regions (`src/menhir/infrastructure/llm.py:87-130`, `:351-397`). R-3 is specifically that stored values inside the user message are unescaped/unstructured and lack a per-fact cap.

8. **“A literal grep showing no caller proves a structure query is dead.” — not used.** `MemoryGraphAdapter.query_structure()` assembles the callee name dynamically with `getattr(self._structure, f"query_{query_type}", None)` (`src/menhir/infrastructure/memory_graph_adapter.py:1075-1079`). No reachability downgrade in this audit was based on absence of a literal function-name reference.

9. **CF-55 `[UTC]` temporal fail-open — not re-filed.** The shadow eligibility call remains at the supplied call site around `src/menhir/services/shadow_context_composition.py:639-643`; this audit treated CF-55 as established and did not assign it an R-number.

10. **CF-39 verbatim generic context rendering — not re-filed.** The sink is present at `src/menhir/services/context_builder.py:397-424` and is used only as downstream reachability context for R-1/R-2/R-4.

## Open Questions

1. **CF-63 / CF-74 / CF-75 register de-duplication.** Their descriptions/code paths were not supplied and the programme-wide confirmed-findings register was not present at the repository path checked. The technical mechanisms in R-1 through R-4 are independently supported, but the register owner should compare them against those opaque IDs before assigning new CF numbers. This is a de-duplication question, not uncertainty about the cited code paths.

2. **Deployment namespace pinning.** Source defaults are unpinned (`client_namespaces = {}`), which makes R-1 reachable. A deployment that pins every exposed client to a namespace would block the demonstrated MCP path. Runtime/deployment configuration was not available, so no claim is made about the percentage of deployed callers that are unpinned.

3. **Non-default frontier deployment.** Runtime source defaults have oracle ranking / warden / evidence-anchor application off, but an operator can enable them by environment. Without deployment configuration, this audit cannot say whether a particular running instance currently applies provenance-derived warden decisions. The default-code finding R-4 remains accurate.

4. **End-to-end attacker control over the exact stored name/summary used in R-2.** The read-side mechanism is proven: the stored node search representation drives candidate selection. This lane did not re-audit the ingest extractor's exact controllability over final entity naming/summary, because that is M7-side behavior and the question here is the read side. Therefore the report does not claim a reproduced chosen-name/summary poisoning payload.

5. **Consumer treatment after the MCP tool result.** CF-39 already establishes that recalled memory is rendered into operator agent context. This review did not independently execute an external agent to measure instruction-following rate, so no claim is made about how often a specific injected string changes downstream behavior.

## Coverage Table

**Measurement method:** no execution environment was available, so `wc -l` was not run. Physical source-line totals were re-derived from the pinned GitHub file contents using line-addressed reads through EOF. The primary corpus is exactly the nine read-side service files below; supporting files used only to prove entry-point, namespace, projection, prompt, or dependency reachability are listed afterward and are **not** included in the 5,186-line total.

| Primary file | Measured physical lines | Review focus |
|---|---:|---|
| `src/menhir/services/context_builder.py` | 572 | Agent-context joins, aggregate budgets, authority blocks, TODO/wiki/timeline composition, session parameter handling |
| `src/menhir/services/recall_service.py` | 146 | Recall defaults and feature gates |
| `src/menhir/services/recall_pipeline.py` | 1,796 | Candidate generation, namespace/session/review filters, provenance-frontier activation, ranking/top-k, temporal hydration |
| `src/menhir/services/recall_policies.py` | 323 | Content selection, temporal fact construction, authority helpers |
| `src/menhir/services/recall_support.py` | 810 | Optional frontier oracle/warden, derived evidence metadata, shadow support |
| `src/menhir/services/retrieval_oracles.py` | 299 | Evidence/semantic/scope/temporal oracle behavior and default oracle set |
| `src/menhir/services/scoring_service.py` | 231 | Floor and final relevance formula / sort |
| `src/menhir/services/hybrid_retrieval.py` | 246 | Retrieval-source semantics, BM25/vector fusion, admission-source distinction |
| `src/menhir/services/shadow_context_composition.py` | 763 | Candidate-fact snapshot, LLM payloads, deterministic filters, tie-break, observe-only trace |
| **Total** | **5,186** | **Reconciled exactly to the stated primary corpus** |

Supporting Menhir files read but excluded from the measured primary total: `src/menhir/mcp/tools/recall/build_context.py`, `src/menhir/mcp/contracts.py`, `src/menhir/mcp/service_access.py`, `src/menhir/core/backend_runtime_data_ops.py`, `src/menhir/config/settings_model.py`, `src/menhir/domain/namespace.py`, `src/menhir/domain/recall.py`, `src/menhir/domain/retrieval_tuning.py`, `src/menhir/domain/brief_builder.py`, `src/menhir/infrastructure/cypher.py`, `src/menhir/infrastructure/memory_queries.py`, `src/menhir/infrastructure/memory_graph_adapter.py`, `src/menhir/infrastructure/graphiti_client.py`, `src/menhir/infrastructure/llm.py`, `src/menhir/services/enrichment_steps.py`, and `uv.lock`. For the dependency-level namespace and search-representation checks, the exact locked dependency `graphiti-core==0.29.2` was read at tag `v0.29.2`, specifically `graphiti_core/search/search.py` and `graphiti_core/search/search_utils.py`; those external files are also excluded from the Menhir corpus total.

## Review Confidence

**92 / 100.**

Confidence is high because all nine primary service files were read through EOF at the pinned commit, the highest-value paths were traced through concrete callers/field projections rather than symbol names, and every finding has a code-backed entry point, default gate, and effect. The namespace finding was additionally checked against the exact dependency version pinned by `uv.lock`, so it does not rest on Menhir's namespace comments. Confidence is reduced by three constraints: no execution/reproduction environment; no deployment configuration to determine whether all clients are namespace-pinned or frontier gates are enabled; and no available descriptions for confirmed CF-63/CF-74/CF-75, which prevents perfect programme-register de-duplication. The latter is why this report uses only R-n identifiers and explicitly asks for register comparison before CF promotion.
