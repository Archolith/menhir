"""D0 retrieval entropy in production — the View-reachability probe.

Retrieval entropy measures how far recall walks (greedy, by rank) before it reaches a
*query-sufficient* target. In the LME benchmark, sufficiency came from dataset labels
(`has_answer` episodes); production menhir has no labels. The deterministic substitute
(sufficiency source (a) of the D0 handoff): **for every View menhir maintains, the
sufficient target for that View's query class is — by construction — the View itself.**

So the probe recalls each current View's own canonical surface (its `name`, the exact
text that was embedded for it) and records the rank at which the View comes back, plus
the footprint (memories and estimated tokens walked to reach it). Zero labels, zero LLM,
continuous. A rising rank means the recall path is burying current state — exactly the
class of ranking/supersession/stamping regressions that unit tests can't see.

Caveats (carried from the D0 experiment, Finding 2):
- This is a REACHABILITY metric — "is the state fact privileged in retrieval" — not an
  accuracy metric. It does not check the value is correct or current; that is the
  belief-gate's axis. Never report it as accuracy.
- The sufficiency source here is the View registry itself. That is what keeps the number
  deterministic and un-Goodhart-able; do not swap in an LLM judgment.

Probes read without reinforcing: every recall runs with ``update_access=False`` so the
measurement does not bump ``last_accessed``/edge weights on exactly the nodes it measures.
"""

from __future__ import annotations

import asyncio
from statistics import median
from typing import Any

from menhir.domain.recall import QueryPreset, ScoredMemory


def estimate_footprint_tokens(text: str) -> int:
    """Deterministic ~chars/4 token estimate for footprint accounting.

    Intentionally NOT tiktoken: the probe is a regression metric, so its units must be
    stable across environments and runs (same heuristic family as the D0 instrument).
    """
    return max(1, (len(text) + 3) // 4)


def walk_to_target(
    results: list[ScoredMemory], target_uuid: str
) -> tuple[int | None, int, int]:
    """Greedy rank walk: (rank, memories_walked, tokens_walked) to reach *target_uuid*.

    Rank is 1-based. When the target is not in *results* (censored at top_k) rank is
    None and memories/tokens cover the full walk that failed to reach it.
    """
    tokens = 0
    for i, r in enumerate(results, start=1):
        tokens += estimate_footprint_tokens(r.content or r.name)
        if r.uuid == target_uuid:
            return i, i, tokens
    return None, len(results), tokens


async def probe_view_reachability(
    *,
    recall_service: Any,
    graph_adapter: Any,
    namespace: str | None = None,
    kind: str | None = None,
    top_k: int = 20,
    max_views: int = 50,
) -> dict[str, Any]:
    """DELIVERED entropy for every current View: the rank + footprint of its self-recall.

    For each current View (optionally filtered by *kind* / *namespace*), recall the
    View's canonical surface inside the View's own namespace and locate the View in the
    ranked results. The healthy state is rank 1 / ~one surface of tokens (the Arm A
    ceiling: rank 1, 1 memory, ~21 tokens); ``reached=False`` means the View did not
    appear in the top ``top_k`` at all — the strongest regression signal.
    """
    views = await asyncio.to_thread(
        graph_adapter.list_views, kind=kind, namespace=namespace, limit=max_views
    )

    rows: list[dict[str, Any]] = []
    for view in views:
        surface = str(view.get("name") or "")
        uuid = str(view.get("uuid") or "")
        # Views store their silo in group_id; "" is the default/shared namespace,
        # which recall expresses as None (no filter). Probing inside the View's own
        # namespace reproduces the retrieval conditions of a real query against it.
        view_ns = str(view.get("namespace") or "") or None
        if not surface or not uuid:
            continue
        result = await recall_service.recall(
            surface,
            preset=QueryPreset.KNOWLEDGE,
            limit=top_k,
            namespace=view_ns,
            update_access=False,
        )
        rank, memories, tokens = walk_to_target(result.results, uuid)
        rows.append(
            {
                "uuid": uuid,
                "view_kind": view.get("kind"),
                "subject": view.get("subject"),
                "namespace": view_ns or "",
                "surface": surface,
                "reached": rank is not None,
                "rank": rank,
                "memories_to_view": memories,
                "tokens_to_view": tokens,
            }
        )

    reached = [r for r in rows if r["reached"]]
    summary = {
        "views_probed": len(rows),
        "reached": len(reached),
        "unreached": len(rows) - len(reached),
        "top_k": top_k,
        "median_rank": median(r["rank"] for r in reached) if reached else None,
        "median_tokens_to_view": (
            median(r["tokens_to_view"] for r in reached) if reached else None
        ),
        "note": (
            "Deterministic reachability metric (D0 sufficiency source: the Views themselves). "
            "Measures whether current state is privileged in retrieval — not answer accuracy."
        ),
    }
    return {"summary": summary, "views": rows}
