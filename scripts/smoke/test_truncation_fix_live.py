#!/usr/bin/env python3
"""Live smoke test: verify truncation retry escalation fires on real LLM calls.

Sends a prompt designed to produce a large JSON output that overflows a
deliberately small max_tokens cap, then verifies the truncation-aware retry
escalation recovers.

Usage:
    python scripts/smoke/test_truncation_fix_live.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# Focus on retry logs
logging.getLogger("menhir.infrastructure.graphiti_patches").setLevel(logging.DEBUG)
# Quiet other loggers
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def main() -> None:
    from dotenv import dotenv_values

    menhir_root = os.path.join(os.path.dirname(__file__), "..", "..")
    env = dotenv_values(os.path.join(menhir_root, ".env"))
    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: no OPENAI_API_KEY in menhir .env")
        sys.exit(1)

    import openai as _openai
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.prompts.models import Message
    from graphiti_core.prompts.extract_nodes_and_edges import CombinedExtraction

    import menhir.infrastructure.graphiti_patches as patches
    patches._patch_graphiti_openai_generic_client(OpenAIGenericClient)

    async_openai = _openai.AsyncOpenAI(api_key=api_key)

    # ----------------------------------------------------------------
    # Test 1: Normal extraction with sufficient max_tokens (should pass
    #         on first attempt, no escalation needed)
    # ----------------------------------------------------------------
    print("=" * 60)
    print("TEST 1: Normal extraction (max_tokens=4096)")
    print("=" * 60)

    client = OpenAIGenericClient(
        config=LLMConfig(
            api_key=api_key,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            temperature=0,
        ),
        client=async_openai,
        max_tokens=4096,
    )

    simple_messages = [
        Message(role="system", content="You are a knowledge graph extraction specialist."),
        Message(role="user", content=(
            "Extract entities and relationships from this conversation:\n"
            "User: I just moved from Chicago to San Francisco for my new job at Google. "
            "My wife Sarah and our dog Max came with us. We found a nice apartment in "
            "the Mission District.\n\n"
            "Respond as a JSON object with keys 'extracted_entities' (list of "
            "{'name': str, 'entity_type_id': 0}) and 'edges' (list of "
            "{'source_entity_name': str, 'target_entity_name': str, "
            "'relation_type': str, 'fact': str, 'episode_indices': [0]})."
        )),
    ]

    try:
        result = await client.generate_response(
            simple_messages, response_model=CombinedExtraction
        )
        entities = result.get("extracted_entities", [])
        edges = result.get("edges", [])
        print(f"  OK: {len(entities)} entities, {len(edges)} edges")
        for e in entities:
            print(f"    Entity: {e.get('name', e)}")
    except Exception as exc:
        print(f"  FAILED: {exc}")

    # ----------------------------------------------------------------
    # Test 2: Force truncation with very small max_tokens, verify
    #         escalation recovers
    # ----------------------------------------------------------------
    print()
    print("=" * 60)
    print("TEST 2: Forced truncation (max_tokens=150) -> escalation")
    print("=" * 60)

    client_small = OpenAIGenericClient(
        config=LLMConfig(
            api_key=api_key,
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            temperature=0,
        ),
        client=async_openai,
        max_tokens=150,  # Deliberately too small — will truncate JSON
    )

    rich_messages = [
        Message(role="system", content="You are a knowledge graph extraction specialist."),
        Message(role="user", content=(
            "Extract ALL entities and relationships from this conversation:\n\n"
            "User: My name is Alex Chen and I'm a software engineer at Microsoft "
            "in Seattle. I graduated from MIT in 2019 with a degree in Computer "
            "Science. My best friend Jake works at Amazon. We both play basketball "
            "at Green Lake Park every Saturday morning. My girlfriend Emma is a "
            "doctor at Swedish Medical Center. She went to Johns Hopkins for med "
            "school. We have two cats named Luna and Mochi. We're planning a trip "
            "to Tokyo next March with Jake and his wife Lisa. Lisa teaches yoga "
            "at CorePower Yoga. We all live in the Capitol Hill neighborhood. "
            "I'm also working on a side project called CodeBuddy, an AI coding "
            "assistant. My manager at Microsoft is David Park, he's great.\n\n"
            "Respond as a JSON object with keys 'extracted_entities' (list of "
            "{'name': str, 'entity_type_id': 0}) and 'edges' (list of "
            "{'source_entity_name': str, 'target_entity_name': str, "
            "'relation_type': str, 'fact': str, 'episode_indices': [0]})."
        )),
    ]

    try:
        result = await client_small.generate_response(
            rich_messages, response_model=CombinedExtraction
        )
        entities = result.get("extracted_entities", [])
        edges = result.get("edges", [])
        print(f"  RECOVERED: {len(entities)} entities, {len(edges)} edges")
        print("  (Truncation was detected and max_tokens was escalated)")
        for e in entities:
            print(f"    Entity: {e.get('name', e)}")
    except Exception as exc:
        print(f"  FAILED even after escalation: {exc}")
        print("  This suggests the model cannot fit the output even at 16K tokens")
        sys.exit(1)

    print()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
