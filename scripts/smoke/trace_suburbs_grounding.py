"""Definitive trace: where does suburbs→Chicago corruption occur?

Each trial uses a completely fresh namespace. Captures:
  1. Raw combined-extraction entities (names)
  2. Raw combined-extraction edge endpoints (entity names, not UUIDs)
  3. Node dedup decisions (per-entity: new vs merged, target name if merged)
  4. Pre-resolution UUID → resolved UUID map
  5. Edge endpoints after UUID remapping
  6. Final persisted graph state

Reports per-trial structured results and aggregate statistics.

Usage:
    python scripts/smoke/trace_suburbs_grounding.py [trials]
    OPENAI_CHAT_MODEL=gpt-4o-mini python scripts/smoke/trace_suburbs_grounding.py 20
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

MENHIR_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(MENHIR_ROOT / "src"))
load_dotenv(MENHIR_ROOT / ".env")

os.environ["NEO4J_URI"] = os.getenv("MENHIR_LME_NEO4J_URI", "bolt://localhost:7694")
os.environ["NEO4J_PASSWORD"] = "lmedata123"
os.environ["GRAPHITI_LLM_PROVIDER"] = "openai"
os.environ["MEMORY_GRAPHITI_PROVIDER"] = "openai"
os.environ["LLM_CHAT_PROVIDER"] = "openai"
os.environ["MEMORY_CHAT_PROVIDER"] = "openai"
os.environ["OPENAI_CHAT_MODEL"] = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
os.environ.setdefault("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Suppress noisy loggers, keep our trace and graphiti extraction/dedup
logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
logging.getLogger("trace").setLevel(logging.INFO)

trace_log = logging.getLogger("trace")


@dataclass
class EdgeTrace:
    source_entity_name: str
    target_entity_name: str
    fact: str
    source_uuid_pre: str = ""
    target_uuid_pre: str = ""
    source_uuid_post: str = ""
    target_uuid_post: str = ""
    source_resolved_name: str = ""
    target_resolved_name: str = ""


@dataclass
class TrialResult:
    trial: int
    namespace: str
    model: str
    temperature: float | None = None

    # Step 1: raw combined extraction output
    raw_extracted_entities: list[str] = field(default_factory=list)
    raw_extracted_edges: list[dict] = field(default_factory=list)
    orphaned_entities: list[str] = field(default_factory=list)

    # Step 2: node dedup
    dedup_decisions: list[dict] = field(default_factory=list)
    uuid_map: dict[str, str] = field(default_factory=dict)

    # Step 3: edge remapping
    edge_traces: list[dict] = field(default_factory=list)

    # Step 3b: edge invalidation
    invalidation_traces: list[dict] = field(default_factory=list)

    # Step 4: final graph
    final_entities: list[str] = field(default_factory=list)
    final_edges: list[dict] = field(default_factory=list)
    suburb_entity_exists: bool = False
    suburb_edge_correct: bool = False
    chicago_edge_expired: bool = False
    verdict: str = ""  # PASS, FAIL_A (extraction), FAIL_B (resolution), FAIL_C (invalidation), FAIL_OTHER


async def run_trial(trial_num: int, total: int) -> TrialResult:
    """Run one complete ingestion trial in a fresh namespace."""
    from menhir.config import MemorySettings
    from menhir.core import build_memory_services, prepare_memory_runtime
    from menhir.domain import IngestStatus, new_session

    namespace = f"grounding-trace-{uuid4().hex[:8]}"
    result = TrialResult(
        trial=trial_num,
        namespace=namespace,
        model=os.environ["OPENAI_CHAT_MODEL"],
    )

    settings = MemorySettings.from_env()
    built = build_memory_services(settings)
    await prepare_memory_runtime(built)

    # Capture actual temperature from the graphiti LLM client
    try:
        result.temperature = built.graphiti_client.client.llm_client.temperature
    except AttributeError:
        pass

    session = new_session("grounding-trace", session_id=namespace)

    # Build episode list (same as bench fixture)
    episodes_text = ["user: My friend Rachel lives in Chicago."]
    episodes_text.extend(f"user: Thanks for the information. Turn {i}." for i in range(12))
    episodes_text.append("user: Miami Beach sounds fun, but I've been there before.")
    episodes_text.append("user: I'm thinking of somewhere more relaxed.")
    episodes_text.append(
        "user: My friend Rachel actually just moved back to the suburbs again,"
        " so I was thinking of somewhere not too far from a major city."
    )
    episodes_text.append("user: Any suggestions?")

    # --- Instrument the combined extraction ---
    import graphiti_core.utils.maintenance.combined_extraction as _ce_mod
    _orig_ce = _ce_mod.extract_nodes_and_edges

    captured_raw: dict = {}

    async def _instrumented_ce(clients, episode, previous_episodes, **kwargs):
        nodes, edges, idx_map = await _orig_ce(clients, episode, previous_episodes, **kwargs)

        # Only instrument the suburbs episode (the one we care about)
        ep_list = episode if isinstance(episode, list) else [episode]
        content = ep_list[0].content if ep_list else ""
        if "suburb" in content.lower():
            captured_raw["nodes"] = [n.name for n in nodes]
            captured_raw["edges"] = [
                {
                    "source_name": _get_node_name(e.source_node_uuid, nodes),
                    "target_name": _get_node_name(e.target_node_uuid, nodes),
                    "fact": e.fact,
                    "source_uuid": e.source_node_uuid,
                    "target_uuid": e.target_node_uuid,
                }
                for e in edges
            ]
            # Identify orphans (nodes with no edge connections)
            connected = set()
            for e in edges:
                connected.add(e.source_node_uuid)
                connected.add(e.target_node_uuid)
            captured_raw["orphans"] = [n.name for n in nodes if n.uuid not in connected]
            # Note: the function already drops orphans internally, so we capture
            # the list before the return. Actually, the extraction already dropped them.
            # Let me re-instrument at the raw LLM response level.

        return nodes, edges, idx_map

    # Deeper instrumentation: capture raw LLM response before node/edge processing
    _orig_llm_response = None

    import graphiti_core.prompts.extract_nodes_and_edges as _ene_mod
    _CombinedExtraction = _ene_mod.CombinedExtraction

    async def _instrumented_ce_deep(clients, episode, previous_episodes, **kwargs):
        """Wrap combined extraction to capture the raw LLM extraction response."""
        ep_list = episode if isinstance(episode, list) else [episode]
        content = ep_list[0].content if ep_list else ""

        if "suburb" not in content.lower():
            return await _orig_ce(clients, episode, previous_episodes, **kwargs)

        # Monkey-patch LLM client to capture raw response for this call only
        llm_client = clients.llm_client
        _orig_gen = llm_client.generate_response

        async def _capture_gen(messages, response_model=None, **gen_kwargs):
            resp = await _orig_gen(messages, response_model=response_model, **gen_kwargs)
            prompt_name = gen_kwargs.get("prompt_name", "")
            if "extract_nodes_and_edges" in prompt_name:
                captured_raw["llm_raw"] = resp
            return resp

        llm_client.generate_response = _capture_gen
        try:
            nodes, edges, idx_map = await _orig_ce(clients, episode, previous_episodes, **kwargs)
        finally:
            llm_client.generate_response = _orig_gen

        captured_raw["returned_nodes"] = [n.name for n in nodes]
        captured_raw["returned_edges"] = [
            {
                "source_name": _get_node_name(e.source_node_uuid, nodes),
                "target_name": _get_node_name(e.target_node_uuid, nodes),
                "fact": e.fact,
                "source_uuid": e.source_node_uuid,
                "target_uuid": e.target_node_uuid,
            }
            for e in edges
        ]
        connected = set()
        for e in edges:
            connected.add(e.source_node_uuid)
            connected.add(e.target_node_uuid)
        # The function already drops orphans, so returned_nodes only has connected ones.
        # Check the raw LLM response for entities that were extracted but not in returned_nodes.
        if "llm_raw" in captured_raw:
            raw_entities = [
                ent.get("name", "") for ent in
                captured_raw["llm_raw"].get("extracted_entities", [])
            ]
            raw_edge_endpoints = []
            for re in captured_raw["llm_raw"].get("edges", []):
                raw_edge_endpoints.append({
                    "source": re.get("source_entity_name", ""),
                    "target": re.get("target_entity_name", ""),
                    "fact": re.get("fact", ""),
                })
            captured_raw["raw_entities"] = raw_entities
            captured_raw["raw_edge_endpoints"] = raw_edge_endpoints

        return nodes, edges, idx_map

    _ce_mod.extract_nodes_and_edges = _instrumented_ce_deep

    # --- Instrument node resolution ---
    import graphiti_core.graphiti as _g_mod
    _orig_resolve = _g_mod.resolve_extracted_nodes

    captured_dedup: dict = {}

    async def _instrumented_resolve(clients, extracted_nodes, episode=None,
                                     previous_episodes=None, entity_types=None,
                                     existing_nodes_override=None):
        # Check if this is the suburbs episode
        ep_content = episode.content if episode else ""
        nodes, uuid_map, dup_pairs = await _orig_resolve(
            clients, extracted_nodes, episode, previous_episodes,
            entity_types, existing_nodes_override,
        )
        if "suburb" in ep_content.lower():
            captured_dedup["extracted"] = [(n.uuid, n.name) for n in extracted_nodes]
            captured_dedup["resolved"] = [(n.uuid, n.name) for n in nodes]
            captured_dedup["uuid_map"] = dict(uuid_map)
            captured_dedup["merges"] = [(ext.name, res.name) for ext, res in dup_pairs]
        return nodes, uuid_map, dup_pairs

    _g_mod.resolve_extracted_nodes = _instrumented_resolve

    # --- Instrument edge pointer resolution ---
    from graphiti_core.utils import bulk_utils as _bu_mod
    _orig_resolve_ptrs = _bu_mod.resolve_edge_pointers

    captured_edge_remap: list[dict] = []

    def _instrumented_resolve_ptrs(edges, uuid_map):
        # Capture before state
        pre_state = []
        for e in edges:
            if "suburb" in (e.fact or "").lower():
                pre_state.append({
                    "fact": e.fact,
                    "source_pre": e.source_node_uuid,
                    "target_pre": e.target_node_uuid,
                })
        result = _orig_resolve_ptrs(edges, uuid_map)
        # Capture after state
        for i, e in enumerate(edges):
            if "suburb" in (e.fact or "").lower():
                pre = pre_state.pop(0) if pre_state else {}
                captured_edge_remap.append({
                    **pre,
                    "source_post": e.source_node_uuid,
                    "target_post": e.target_node_uuid,
                    "source_changed": pre.get("source_pre") != e.source_node_uuid,
                    "target_changed": pre.get("target_pre") != e.target_node_uuid,
                })
        return result

    _bu_mod.resolve_edge_pointers = _instrumented_resolve_ptrs

    # --- Instrument edge invalidation (resolve_extracted_edge) ---
    import graphiti_core.utils.maintenance.edge_operations as _eo_mod
    _orig_resolve_edge = _eo_mod.resolve_extracted_edge

    captured_invalidation: list[dict] = []

    async def _instrumented_resolve_edge(
        llm_client, extracted_edge, related_edges, existing_edges, episode, edge_type_candidates=None,
    ):
        # Only trace edges whose fact mentions suburbs or chicago
        fact_lower = (extracted_edge.fact or "").lower()
        is_relevant = "suburb" in fact_lower or "chicago" in fact_lower

        if is_relevant:
            trace_entry = {
                "new_fact": extracted_edge.fact,
                "new_src": extracted_edge.source_node_uuid,
                "new_tgt": extracted_edge.target_node_uuid,
                "related_edges": [
                    {"idx": i, "fact": e.fact, "src": e.source_node_uuid, "tgt": e.target_node_uuid,
                     "expired": bool(getattr(e, "expired_at", None) or getattr(e, "invalid_at", None))}
                    for i, e in enumerate(related_edges)
                ],
                "invalidation_candidates": [
                    {"idx": len(related_edges) + i, "fact": e.fact,
                     "src": e.source_node_uuid, "tgt": e.target_node_uuid,
                     "expired": bool(getattr(e, "expired_at", None) or getattr(e, "invalid_at", None))}
                    for i, e in enumerate(existing_edges)
                ],
                "chicago_in_candidates": any(
                    "chicago" in (e.fact or "").lower()
                    for e in list(related_edges) + list(existing_edges)
                ),
            }

            # Intercept LLM call to capture contradiction response
            _orig_gen = llm_client.generate_response

            async def _capture_gen(messages, response_model=None, **gen_kwargs):
                resp = await _orig_gen(messages, response_model=response_model, **gen_kwargs)
                prompt_name = gen_kwargs.get("prompt_name", "")
                if "dedupe_edges" in prompt_name:
                    trace_entry["llm_response"] = resp
                    # Capture the prompt content for analysis
                    for msg in messages:
                        content = msg.content if hasattr(msg, "content") else msg.get("content", "")
                        if "INVALIDATION" in content:
                            trace_entry["prompt_snippet"] = content[:500]
                            break
                return resp

            llm_client.generate_response = _capture_gen
            try:
                resolved, invalidated, duplicates = await _orig_resolve_edge(
                    llm_client, extracted_edge, related_edges, existing_edges,
                    episode, edge_type_candidates,
                )
            finally:
                llm_client.generate_response = _orig_gen

            trace_entry["invalidated_facts"] = [
                {"fact": e.fact, "src": e.source_node_uuid, "tgt": e.target_node_uuid}
                for e in invalidated
            ]
            trace_entry["resolved_fact"] = resolved.fact
            trace_entry["resolved_expired"] = bool(
                getattr(resolved, "expired_at", None) or getattr(resolved, "invalid_at", None)
            )
            captured_invalidation.append(trace_entry)
            return resolved, invalidated, duplicates
        else:
            return await _orig_resolve_edge(
                llm_client, extracted_edge, related_edges, existing_edges,
                episode, edge_type_candidates,
            )

    _eo_mod.resolve_extracted_edge = _instrumented_resolve_edge

    try:
        for i, ep_text in enumerate(episodes_text):
            r = await built.ingest_service.ingest_episode(
                episode=ep_text, session=session,
                source="grounding-trace", namespace=namespace,
            )
            if r.status is not IngestStatus.INGESTED:
                trace_log.warning("Episode %d status: %s", i, r.status.value)

        # Populate result from captured data
        if "raw_entities" in captured_raw:
            result.raw_extracted_entities = captured_raw["raw_entities"]
        if "raw_edge_endpoints" in captured_raw:
            result.raw_extracted_edges = captured_raw["raw_edge_endpoints"]
        if "returned_nodes" in captured_raw:
            returned = set(captured_raw["returned_nodes"])
            raw = set(result.raw_extracted_entities)
            result.orphaned_entities = list(raw - returned)

        if captured_dedup:
            result.dedup_decisions = [
                {"extracted": ext_name, "resolved_to": res_name,
                 "was_merged": ext_uuid != res_uuid}
                for (ext_uuid, ext_name), (res_uuid, res_name)
                in zip(
                    captured_dedup.get("extracted", []),
                    captured_dedup.get("resolved", []),
                )
            ]
            result.uuid_map = captured_dedup.get("uuid_map", {})

        result.edge_traces = captured_edge_remap
        result.invalidation_traces = captured_invalidation

        # Query final graph state
        entities = built.neo4j.execute(
            "MATCH (n:Entity {group_id: $ns}) WHERE n.name <> 'Admission granted: remote-api claimed user' "
            "RETURN n.name AS name ORDER BY n.name",
            params={"ns": namespace},
        )
        result.final_entities = [e["name"] for e in entities]

        edges = built.neo4j.execute(
            "MATCH (a:Entity {group_id: $ns})-[r:RELATES_TO]->(b:Entity) "
            "RETURN a.name AS src, b.name AS tgt, r.fact AS fact, "
            "r.invalid_at AS inv, r.expired_at AS exp",
            params={"ns": namespace},
        )
        result.final_edges = [
            {"src": e["src"], "tgt": e["tgt"], "fact": e["fact"],
             "expired": bool(e.get("inv") or e.get("exp"))}
            for e in edges
        ]

        # Evaluate verdict
        result.suburb_entity_exists = any("suburb" in e.lower() for e in result.final_entities)
        result.suburb_edge_correct = any(
            "suburb" in e["fact"].lower() and "suburb" in e["tgt"].lower() and not e["expired"]
            for e in result.final_edges
        )
        result.chicago_edge_expired = all(
            e["expired"] for e in result.final_edges
            if e["tgt"].lower() == "chicago"
        )

        if result.suburb_entity_exists and result.suburb_edge_correct and result.chicago_edge_expired:
            result.verdict = "PASS"
        elif not any("suburb" in e.lower() for e in result.raw_extracted_entities):
            result.verdict = "FAIL_A_NOT_EXTRACTED"
        elif any("suburb" in e.lower() for e in result.orphaned_entities):
            result.verdict = "FAIL_A_ORPHANED"
        elif not any("suburb" in e.get("target", "").lower() for e in result.raw_extracted_edges):
            result.verdict = "FAIL_A_WRONG_EDGE_TARGET"
        elif any(d.get("was_merged") and "suburb" in d.get("extracted", "").lower()
                 for d in result.dedup_decisions):
            result.verdict = "FAIL_B_DEDUP_MERGED"
        elif any(e.get("target_changed") for e in result.edge_traces):
            result.verdict = "FAIL_B_UUID_REMAP"
        elif not result.suburb_entity_exists:
            result.verdict = "FAIL_UNKNOWN_NO_ENTITY"
        elif not result.suburb_edge_correct:
            result.verdict = "FAIL_UNKNOWN_BAD_EDGE"
        elif not result.chicago_edge_expired:
            # Suburb entity + edge correct, but Chicago edge not expired.
            # Classify based on invalidation trace data.
            inv = result.invalidation_traces
            suburbs_inv = [t for t in inv if "suburb" in t.get("new_fact", "").lower()]
            if not suburbs_inv:
                result.verdict = "FAIL_C_NO_INVALIDATION_TRACE"
            elif not any(t.get("chicago_in_candidates") for t in suburbs_inv):
                result.verdict = "FAIL_C_CHICAGO_NOT_CANDIDATE"
            elif any(t.get("llm_response", {}).get("contradicted_facts") for t in suburbs_inv):
                result.verdict = "FAIL_C_CONTRADICTION_FOUND_BUT_NOT_EXPIRED"
            else:
                result.verdict = "FAIL_C_LLM_MISSED_CONTRADICTION"
        else:
            result.verdict = "FAIL_OTHER"

    finally:
        # Cleanup
        if namespace.startswith("grounding-trace-"):
            built.neo4j.execute(
                "MATCH (n) WHERE n.group_id = $ns DETACH DELETE n",
                params={"ns": namespace},
            )
        # Restore patched functions
        _ce_mod.extract_nodes_and_edges = _orig_ce
        _g_mod.resolve_extracted_nodes = _orig_resolve
        _bu_mod.resolve_edge_pointers = _orig_resolve_ptrs
        _eo_mod.resolve_extracted_edge = _orig_resolve_edge

        await built.ingest_service.shutdown()
        await built.recall_service.shutdown()
        await built.graphiti_client.client.close()
        built.neo4j.close()

    return result


def _get_node_name(uuid: str, nodes: list) -> str:
    for n in nodes:
        if n.uuid == uuid:
            return n.name
    return f"<unknown:{uuid[:8]}>"


async def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    trace_log.info("Model: %s", os.environ["OPENAI_CHAT_MODEL"])
    trace_log.info("Trials: %d (each in fresh namespace)", trials)

    results: list[TrialResult] = []
    for i in range(trials):
        trace_log.info("--- Trial %d/%d ---", i + 1, trials)
        r = await run_trial(i + 1, trials)
        results.append(r)

        # Print per-trial summary
        raw_edge_targets = [e.get("target", "") for e in r.raw_extracted_edges]
        trace_log.info(
            "  raw_entities=%s  raw_edge_targets=%s  orphaned=%s  verdict=%s",
            r.raw_extracted_entities,
            raw_edge_targets,
            r.orphaned_entities,
            r.verdict,
        )
        if r.verdict != "PASS":
            trace_log.info("  final_entities=%s", r.final_entities)
            trace_log.info("  final_edges=%s", r.final_edges)
            if r.edge_traces:
                trace_log.info("  edge_remap=%s", r.edge_traces)
            # Invalidation trace detail for FAIL_C verdicts
            if r.verdict.startswith("FAIL_C") and r.invalidation_traces:
                for inv in r.invalidation_traces:
                    if "suburb" in inv.get("new_fact", "").lower():
                        trace_log.info("  inv_new_fact=%s", inv["new_fact"])
                        trace_log.info("  inv_chicago_in_candidates=%s", inv.get("chicago_in_candidates"))
                        trace_log.info("  inv_candidates=%d related + %d invalidation",
                                       len(inv.get("related_edges", [])),
                                       len(inv.get("invalidation_candidates", [])))
                        for c in inv.get("invalidation_candidates", []):
                            trace_log.info("    candidate[%d]: %s", c["idx"], c["fact"][:80])
                        llm_resp = inv.get("llm_response", {})
                        trace_log.info("  inv_llm_contradicted=%s  inv_llm_duplicates=%s",
                                       llm_resp.get("contradicted_facts", []),
                                       llm_resp.get("duplicate_facts", []))
                        trace_log.info("  inv_invalidated=%s",
                                       [e["fact"][:60] for e in inv.get("invalidated_facts", [])])

    # Aggregate
    verdicts = {}
    for r in results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1

    trace_log.info("")
    trace_log.info("=" * 60)
    trace_log.info("AGGREGATE (%d trials, model=%s, temperature=%s)",
                   trials, results[0].model if results else "?",
                   results[0].temperature if results else "?")
    trace_log.info("=" * 60)
    for verdict, count in sorted(verdicts.items()):
        pct = count / trials * 100
        trace_log.info("  %-30s %3d/%d (%.0f%%)", verdict, count, trials, pct)

    # Dump full results to file
    output_path = MENHIR_ROOT / "results" / "suburbs_grounding_trace.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(r) for r in results], indent=2, default=str),
        encoding="utf-8",
    )
    trace_log.info("Full results: %s", output_path)


if __name__ == "__main__":
    asyncio.run(main())
