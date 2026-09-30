"""Backfill content embeddings on Menhir `:Episodic` nodes (source-memory lane).

WHY THIS EXISTS
The lane's ingest step (`services/enrichment_steps.embed_episode_content`) is
forward-fill only and gated by MENHIR_FRONTIER_SOURCE_MEMORIES, so every episode
enriched before the flag was enabled (or while it was off) has no
`content_embedding` and can never surface in the source-memory recall section.
This script pages through `list_episodes_missing_content_embedding` -- the same
structural/visibility predicates the read query uses, so it only embeds episodes
generic recall could ever surface -- and writes embeddings with the same
`set_episode_content_embedding` helper the ingest step uses.

IDEMPOTENT: a written embedding removes the node from the listing, so re-running
writes nothing new. Safe to re-run after a partial batch.

Usage:
  python scripts/backfill_episode_embeddings.py --dry-run
  python scripts/backfill_episode_embeddings.py --namespace default --limit 500
  python scripts/backfill_episode_embeddings.py --namespace lme-x --batch 100

Read-only unless run WITHOUT --dry-run. Embeds via the configured Graphiti
embedder (the same embedder recall uses), truncating content to 8,000 chars.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.observability import build_async_openai_client
from menhir.infrastructure.providers import ProviderConfig

#: Same truncation bound as the ingest step (services/enrichment_steps.py).
_EMBED_MAX_CHARS = 8000


def _load_settings() -> Any:
    from menhir.config import MemorySettings

    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(os.getenv("ENV_FILE") or repo_root / ".env")
    return MemorySettings.from_env()


def _resolve_embedder(settings: Any) -> tuple[Any, str]:
    """Build the OpenAI-compatible embed client + model name (same embedder recall uses)."""
    provider = ProviderConfig.for_graphiti_embedder(settings)
    if not provider.supports_graphiti_openai_contract():
        raise RuntimeError(
            f"Graphiti embed provider is not OpenAI-compatible: {provider.kind.value}"
        )
    if not provider.embed_model:
        raise RuntimeError("Graphiti embed model is blank.")
    client = build_async_openai_client(
        base_url=provider.base_url,
        api_key=provider.api_key,
        settings=settings,
    )
    return client, provider.embed_model


async def _embed_text(client: Any, *, model: str, text: str) -> list[float]:
    response = await client.embeddings.create(model=model, input=[text])
    data = sorted(getattr(response, "data", []), key=lambda item: int(getattr(item, "index", 0)))
    return [float(v) for v in getattr(data[0], "embedding")]


async def _run(args: argparse.Namespace) -> int:
    settings = _load_settings()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    repo = EpisodeRepository(neo4j)
    written = 0
    skipped = 0
    try:
        while True:
            if args.limit is not None and written >= args.limit:
                break
            batch_limit = args.batch
            if args.limit is not None:
                batch_limit = min(batch_limit, args.limit - written)
            rows = repo.list_episodes_missing_content_embedding(
                args.namespace, limit=batch_limit
            )
            if not rows:
                break
            if args.dry_run:
                print(f"[dry-run] would embed {len(rows)} episode(s); e.g.:")
                for row in rows[:5]:
                    preview = str(row.get("content") or "")[:70].replace("\n", " ")
                    print(f"  {row.get('uuid')} {preview!r}")
                written += len(rows)
                break
            client, model = _resolve_embedder(settings)
            for row in rows:
                content = str(row.get("content") or "")[:_EMBED_MAX_CHARS]
                if not content.strip():
                    skipped += 1
                    continue
                embedding = await _embed_text(client, model=model, text=content)
                if repo.set_episode_content_embedding(str(row["uuid"]), embedding, model):
                    written += 1
                else:
                    skipped += 1
            if len(rows) < batch_limit:
                break
    finally:
        neo4j.close()
    mode = "would write" if args.dry_run else "wrote"
    print(f"{mode} {written} embedding(s); skipped {skipped}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", default=None,
                        help="restrict to one namespace silo (default: all)")
    parser.add_argument("--batch", type=int, default=100,
                        help="episodes per page (default: 100)")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many episodes (default: no limit)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be embedded without writing")
    args = parser.parse_args()
    if args.batch < 1:
        parser.error("--batch must be >= 1")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    try:
        return asyncio.run(_run(args))
    except RuntimeError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
