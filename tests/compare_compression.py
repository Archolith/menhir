"""Side-by-side comparison of LLM vs truncation compression on fake memories."""

import asyncio

from menhir.infrastructure.llm import LLMAdapter
from menhir.services.lifecycle_service import LifecycleService

MEMORIES = [
    # 1. Ultra-short preference
    "User prefers dark mode.",

    # 2. Short factual
    "Alice works at Google on the Search team. She started in January 2025 and lives in Mountain View.",

    # 3. Medium conversational episode
    (
        "Had a long call with Marcus today about the migration from MongoDB to Neo4j. "
        "He's worried about the learning curve for Cypher queries but I convinced him "
        "that the relationship traversal performance is worth it. We agreed to start "
        "with a proof of concept using the user-follows-user subgraph. He'll set up "
        "the Docker instance by Thursday and I'll write the initial schema migration "
        "script. We also briefly discussed whether to use Graphiti or build our own "
        "abstraction layer — leaning toward Graphiti since it handles embeddings natively."
    ),

    # 4. Long multi-topic session
    (
        "Morning standup covered three topics. First, the deployment pipeline is broken "
        "again — Jenkins keeps timing out on the integration test stage because the "
        "test database isn't being cleaned up between runs. Sarah volunteered to fix "
        "the teardown script. Second, the new onboarding flow redesign got approval "
        "from the product team. The key changes are: replacing the 5-step wizard with "
        "a single-page progressive disclosure form, adding SSO support for Google and "
        "Microsoft accounts, and removing the mandatory phone verification step that "
        "was causing a 23% drop-off rate. Third, we need to decide on the caching "
        "strategy for the recommendation engine. Options are Redis with a 15-minute TTL, "
        "a local in-memory LRU cache per service instance, or a hybrid approach where "
        "Redis handles the global top-1000 items and local caches handle per-user "
        "personalization. Ben is running benchmarks this week. The team also discussed "
        "the upcoming team offsite in Portland — dates are March 20-22, venue is the "
        "Sentinel Hotel, and we need to submit travel requests by Friday."
    ),

    # 5. Dense technical procedural memory
    (
        "To deploy the cth.mcp.memory MCP server to production: 1) Ensure Neo4j is running "
        "on the target host with APOC plugin enabled (version 5.x required, 4.x will "
        "fail on temporal duration queries). 2) Set environment variables: NEO4J_URI, "
        "NEO4J_USER, NEO4J_PASSWORD, LLAMA_BASE_URL, LLAMA_API_KEY, "
        "LLAMA_CHAT_MODEL (default: qwen3-8b), LLAMA_EMBED_MODEL (default: "
        "nomic-embed-text-v1.5). 3) Run schema bootstrap via prepare_memory_runtime() "
        "which creates indexes on Entity(uuid), Episodic(uuid), and composite indexes "
        "on scope+freshness+last_accessed for decay candidate queries. 4) Verify "
        "Graphiti indices with build_indices_and_constraints(). 5) Start the MCP "
        "server with uvicorn on port 8765. The server exposes 4 tools (add_memory, "
        "recall_memories, flag_memory, delete_memory) and 6 resources (overview, "
        "recent, flagged, conflicts, config, health). 6) After first startup, run "
        "sync_edge_counts() to populate edge_count fields — this is now automatic "
        "in prepare_memory_runtime() but may need manual invocation if upgrading from "
        "pre-M4 versions. 7) Configure the background worker polling interval via "
        "ENRICHMENT_POLL_SECONDS (default 5). The worker processes PENDING episodes "
        "through Graphiti enrichment and stamps policy metadata. 8) For decay, set "
        "RUN_DECAY_ON_STARTUP=true or trigger manually via LifecycleService.apply_decay(). "
        "Decay runs: edge count sync, sharpness recalculation via vector similarity, "
        "ACTIVE->COMPRESSED transition (30d + sharpness<0.3 + edges<5), COMPRESSED->GONE "
        "with edge bridging (90d + sharpness<0.1 + edges<3), and orphan subgraph cleanup."
    ),

    # 6. Emotional/narrative memory with many details
    (
        "Really frustrating debugging session today. Spent almost four hours tracking "
        "down why the recall scoring was returning completely wrong results. The "
        "prominence component was always zero, which made every query effectively "
        "ignore graph connectivity. Turned out the root cause was embarrassingly "
        "simple: gamma was hardcoded to 0.0 in all preset weights because edge_count "
        "hadn't been synchronized yet when the feature was first built. The original "
        "developer left a comment saying 'zeroed until edge_count sync lands' but "
        "nobody remembered to restore it after sync_edge_counts() was implemented "
        "in M4 Phase 1. The fix was literally changing five 0.0 values to 0.1 in "
        "the PRESET_WEIGHTS dictionary. I wrote a note in the design doc about this "
        "so it doesn't happen again. On the bright side, recall quality improved "
        "noticeably once prominence was active — queries like 'what do I know about "
        "Alice' now correctly boost nodes that are well-connected in the graph rather "
        "than treating isolated facts equally. Marcus noticed the improvement "
        "immediately during his testing session. Feeling relieved but also annoyed "
        "at how much time was wasted on something so trivial."
    ),
]

LABELS = [
    "Ultra-short preference",
    "Short factual",
    "Medium conversational",
    "Long multi-topic session",
    "Dense technical procedural",
    "Emotional narrative",
]


async def main():
    adapter = LLMAdapter(
        base_url="http://localhost:1234/v1",
        api_key="local",
        chat_model="qwen3-8b",
        embed_model="nomic-embed-text-v1.5",
    )

    print("=" * 90)
    print("MEMORY COMPRESSION COMPARISON: LLM vs Truncation")
    print("=" * 90)

    for i, (label, memory) in enumerate(zip(LABELS, MEMORIES), 1):
        truncated = LifecycleService._compress_content(memory)
        llm_result = await adapter.compress_content(memory)

        print(f"\n{'-' * 90}")
        print(f"  #{i} - {label}  ({len(memory)} chars)")
        print(f"{'-' * 90}")
        print(f"\n  ORIGINAL:\n  {memory[:300]}{'...' if len(memory) > 300 else ''}")
        print(f"\n  TRUNCATION ({len(truncated)} chars):\n  {truncated}")
        print(f"\n  LLM ({len(llm_result) if llm_result else 0} chars):\n  {llm_result}")

        # Verdict
        if llm_result:
            ratio = len(llm_result) / len(memory) * 100
            print(f"\n  -> LLM compression ratio: {ratio:.0f}% of original")
        else:
            print("\n  -> LLM returned nothing")

    print(f"\n{'=' * 90}")


if __name__ == "__main__":
    asyncio.run(main())
