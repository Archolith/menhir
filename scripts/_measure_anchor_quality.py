"""Measure REAL anchor quality on the live-menhir clone (dummy 7687), read-only.

Gate (b) calibration: before injecting synthetic anchor noise into the bench fixture,
measure what the real graph's ANCHORED_TO/DEFINES anchors actually look like. The dummy
is a bolt clone of the live menhir graph (_clone_to_dummy.py), so its anchors are the
real production anchors.

No gold labels exist for "is this anchor correct", so we use proxies:
  1. text-anchor agreement  — does the memory's content mention the anchored file's stem
     or any symbol that file DEFINES? Low agreement => likely spurious anchor (the
     dominant real-world noise: memories linked to boilerplate they never discuss).
  2. anchors-per-memory     — fan-out; many unrelated files per memory = noisy.
  3. file multiplicity      — files anchored by many memories (boilerplate: __init__.py,
     main.py, pyproject.toml) carry ~no discriminating signal.
  4. DEFINES fan-out        — symbols per anchored file; high => noisy symbol facets.

Output calibrates the corruption RATE and MODEL (drop/swap/spurious) for the bench
anchor-noise regime. Read-only.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from menhir.infrastructure.neo4j import Neo4jRepository  # noqa: E402

_WORD = re.compile(r"[a-z0-9_]+")


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _mentions(content_tokens: set[str], name: str) -> bool:
    """Does the content reference this file stem / symbol? Split snake/camel into parts."""
    name_l = name.lower()
    if name_l in content_tokens:
        return True
    parts = [p for p in re.split(r"[_\W]+", name_l) if len(p) > 2]
    # camelCase / PascalCase split
    parts += [p.lower() for p in re.findall(r"[A-Z]?[a-z]{3,}", name)]
    return any(p in content_tokens for p in parts)


def main() -> None:
    repo = Neo4jRepository(
        uri="bolt://localhost:7687", database="neo4j", user="neo4j", password="menhirdummy123",
    )
    try:
        rows = repo.execute(
            """
            MATCH (n:Entity)-[:ANCHORED_TO]->(f) WHERE f.structure_path IS NOT NULL
            OPTIONAL MATCH (f)-[:DEFINES]->(s) WHERE s.symbol_kind IS NOT NULL
            WITH n, f, collect(DISTINCT s.name) AS syms
            RETURN n.uuid AS uuid,
                   coalesce(n.content, n.summary, n.name, '') AS content,
                   f.structure_path AS path,
                   syms
            """
        )
        print(f"anchored (memory,file) pairs: {len(rows)}")

        anchors_per_mem: Counter[str] = Counter()
        file_mult: Counter[str] = Counter()
        defines_fanout: dict[str, int] = {}
        agree_file = 0            # anchor whose file stem is mentioned in content
        agree_symbol = 0         # anchor with >=1 DEFINES symbol mentioned in content
        agree_any = 0            # either
        spurious = 0             # neither -> content never references the anchored file/symbols
        empty_content = 0

        for r in rows:
            uuid, path, syms = r["uuid"], r["path"], r["syms"] or []
            content = r["content"] or ""
            anchors_per_mem[uuid] += 1
            file_mult[path] += 1
            defines_fanout[path] = max(defines_fanout.get(path, 0), len(syms))
            if not content.strip():
                empty_content += 1
                continue
            ctok = _tokens(content)
            f_ok = _mentions(ctok, _stem(path))
            s_ok = any(_mentions(ctok, s) for s in syms)
            agree_file += f_ok
            agree_symbol += s_ok
            if f_ok or s_ok:
                agree_any += 1
            else:
                spurious += 1

        n = len(rows)
        judged = n - empty_content
        print("\n--- text-anchor agreement (accuracy proxy) ---")
        print(f"  empty-content anchors (unjudgeable): {empty_content} ({empty_content/n:.0%})")
        print(f"  file-stem mentioned in content:      {agree_file}/{judged} ({agree_file/judged:.0%})")
        print(f"  a DEFINES symbol mentioned:          {agree_symbol}/{judged} ({agree_symbol/judged:.0%})")
        print(f"  EITHER (anchor 'supported' by text): {agree_any}/{judged} ({agree_any/judged:.0%})")
        print(f"  NEITHER (likely-spurious anchor):    {spurious}/{judged} ({spurious/judged:.0%})  <= noise rate")

        print("\n--- anchors per memory ---")
        apm = anchors_per_mem.values()
        print(f"  memories: {len(anchors_per_mem)}  mean anchors/mem: {sum(apm)/len(apm):.1f}  "
              f"max: {max(apm)}")
        dist = Counter(apm)
        for k in sorted(dist)[:8]:
            print(f"    {k} anchor(s): {dist[k]} memories")

        print("\n--- file multiplicity (boilerplate = anchored by many memories) ---")
        for path, c in file_mult.most_common(10):
            print(f"    {c:4d} memories -> {path}  (DEFINES fanout {defines_fanout.get(path,0)})")

        print("\n--- DEFINES fan-out (symbols per anchored file) ---")
        fo = [v for v in defines_fanout.values()]
        big = sum(1 for v in fo if v >= 10)
        print(f"  files with >=10 DEFINES symbols: {big}/{len(fo)} ({big/max(1,len(fo)):.0%}) "
              f"(high fan-out => noisy symbol facets)")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
