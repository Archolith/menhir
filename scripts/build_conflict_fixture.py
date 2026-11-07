"""Refresh name cosines and the embedder stamp in tests/fixtures/conflict_pairs.json.

The fixture stores each pair's name cosine, not its vectors, so tests need no network. Run this
after changing pairs or the embedder, then review the diff: a moved cosine can move the floor.
Embeds each unique name once with the configured embedder.

    ENV_FILE=/path/to/.env uv run python scripts/build_conflict_fixture.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from menhir.config import MemorySettings
from menhir.env_file import load_menhir_env
from menhir.infrastructure.view_embedder import make_view_embedder, view_embedder_version
from menhir.services.correlation_service_signals import pair_cosine

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "conflict_pairs.json"


def render(embedder: str | None, pairs: list[dict]) -> str:
    """One pair per line, so a re-embed shows up as a readable diff."""
    rows = ",\n".join(f"  {json.dumps(pair)}" for pair in pairs)
    return f'{{\n "embedder": {json.dumps(embedder)},\n "pairs": [\n{rows}\n ]\n}}\n'


def main() -> int:
    load_menhir_env()
    settings = MemorySettings.from_env()
    embed = make_view_embedder(settings)
    if embed is None:
        print("no embedder configured", file=sys.stderr)
        return 1
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    vectors: dict[str, list[float]] = {}
    for pair in fixture["pairs"]:
        for name in (pair["a"]["name"], pair["b"]["name"]):
            if name not in vectors:
                vector = embed(name)
                if vector is None:
                    print(f"embedding failed for {name!r}", file=sys.stderr)
                    return 1
                vectors[name] = vector
        cosine = pair_cosine(vectors[pair["a"]["name"]], vectors[pair["b"]["name"]])
        pair["name_cosine"] = None if cosine is None else round(cosine, 4)
    FIXTURE.write_text(render(view_embedder_version(settings), fixture["pairs"]), encoding="utf-8")
    print(f"wrote {len(fixture['pairs'])} pairs, {len(vectors)} names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
