"""MCP tool: delete_memories -- erase up to 100 ids, each exactly as delete_memory does (#476)."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from menhir.mcp.contracts import ToolScope
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.tools.ingest.delete_memory import OUTSIDE_NAMESPACE, describe_erase

MAX_IDS = 100
#: Stop starting new erases after this long, so the caller gets a per-id report (with the rest
#: marked not_run) instead of the 120 s tool timeout cutting the call off mid-batch.
TIME_BUDGET_SECONDS = 90.0
NOT_RUN = "not_run"
ERROR = "error"
WOULD_ERASE = "would_erase"


async def delete_memories(
    node_uuids: list[str], namespace: str = "", dry_run: bool = True
) -> str:
    """Erase up to 100 memories by id, each exactly as delete_memory erases one.

    Accepts the same ids as delete_memory (memory, entity, TurnEvidence turn and typed-assertion
    ids); list_namespace_memories returns them. Previews by default: pass dry_run=false to erase.

    Args:
        node_uuids: 1-100 canonical uuids. Any invalid id refuses the whole call; nothing runs.
        namespace: Restrict to one silo; an id proven to be in another silo is refused. A pinned
            client has this forced.
        dry_run: Report what each id resolves to without erasing (default true).

    Returns:
        JSON with one result per distinct id (reason + message) and totals by reason.
    """

    return await DeleteMemoriesTool().execute(
        node_uuids=node_uuids, namespace=namespace, dry_run=dry_run
    )


def _refusal(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "erased": 0, **extra}


class DeleteMemoriesTool(BaseJsonTool):
    name = "delete_memories"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Delete Memories"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False
    description = (
        "Erase up to 100 memories by id, each as delete_memory does. Previews unless dry_run=false."
    )

    async def endpoint(
        self, node_uuids: list[str], namespace: str = "", dry_run: bool = True
    ) -> str:
        from menhir.domain.erase_target import normalize_erase_target

        raw = list(node_uuids or [])
        if not raw:
            return self.render_json(_refusal("no ids given; nothing was erased"))
        if len(raw) > MAX_IDS:
            return self.render_json(_refusal(
                f"{len(raw)} ids exceeds the limit of {MAX_IDS} per call; nothing was erased",
                limit=MAX_IDS,
            ))
        invalid = [repr(v) for v in raw if normalize_erase_target(v) is None]
        if invalid:
            return self.render_json(_refusal(
                "invalid ids (expected the full 36-character uuid form); nothing was erased",
                invalid=invalid,
            ))
        targets: list[str] = []
        for value in raw:
            target = normalize_erase_target(value)
            if target not in targets:
                targets.append(target)

        backend = self.get_backend()
        ns = namespace.strip() or None
        deadline = time.monotonic() + TIME_BUDGET_SECONDS
        results: list[dict[str, Any]] = []
        for index, target in enumerate(targets):
            if not dry_run and time.monotonic() > deadline:
                results.extend(
                    {
                        "id": rest,
                        "reason": NOT_RUN,
                        "message": "Not attempted: this call's time budget ran out. Nothing was "
                        "erased for this id; re-run with the remaining ids.",
                    }
                    for rest in targets[index:]
                )
                break
            try:
                # Ownership guard, per id, exactly as delete_memory: refuse only an id proven to
                # be in another silo; absent-from-graph proceeds (graph_already_absent).
                in_namespace = None
                if ns is not None:
                    in_namespace = await backend.fetch_memory_by_uuid(target, namespace=ns)
                    if in_namespace is None and await backend.fetch_memory_by_uuid(
                        target
                    ) is not None:
                        outcome = {"reason": OUTSIDE_NAMESPACE, "namespace": ns}
                        results.append({
                            "id": target,
                            "reason": OUTSIDE_NAMESPACE,
                            "message": describe_erase(target, outcome),
                        })
                        continue
                if dry_run:
                    node = in_namespace or await backend.fetch_memory_by_uuid(target)
                    results.append(self._preview(target, node))
                    continue
                outcome = dict(await backend.erase_memory(target, namespace=ns) or {})
                results.append({
                    "id": target,
                    "reason": outcome.get("reason"),
                    "message": describe_erase(target, outcome),
                })
            except Exception as exc:  # one id's failure must not hide the others' results
                results.append({
                    "id": target,
                    "reason": ERROR,
                    "message": f"Erasure of {target} raised {type(exc).__name__}: {exc}. It may "
                    "not have completed; retry this id.",
                })

        return self.render_json({
            "dry_run": dry_run,
            "namespace": ns,
            "requested": len(raw),
            "distinct": len(targets),
            "totals": dict(Counter(str(r["reason"]) for r in results)),
            "results": results,
        })

    @staticmethod
    def _preview(target: str, node: dict[str, Any] | None) -> dict[str, Any]:
        if node is None:
            return {
                "id": target,
                "reason": WOULD_ERASE,
                "found_as": "not_a_memory_node",
                "message": f"{target} is not a memory or entity node: it may be a TurnEvidence "
                "turn or typed-assertion id, or absent. A real run attempts the erase and "
                "reports what it found.",
            }
        return {
            "id": target,
            "reason": WOULD_ERASE,
            "found_as": "memory",
            "namespace": node.get("namespace"),
            "message": f"Would erase memory {target} (namespace {node.get('namespace')!r}).",
        }
