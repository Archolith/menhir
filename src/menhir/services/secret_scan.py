"""Read-only, count-only credential scan of one namespace (#577).

Reports how many stored nodes hold a recognised credential, by memory type and kind. It
never returns, logs or prints a secret value, and it writes nothing. A clean result means
"nothing recognised", not "nothing sensitive".
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from menhir.domain.namespace import normalize_namespace, tenant_scope_cypher, tenant_scope_params
from menhir.domain.secret_scrub import detect_secrets

Execute = Callable[[str, dict[str, Any]], list[dict[str, Any]]]

BATCH = 500
_TEXT_FIELDS = ("content", "summary", "name", "text", "diff")

# Read-only. Pages by uuid so a long scan holds no cursor and a concurrent write cannot skip rows.
_SCAN_QUERY = f"""
MATCH (n)
WHERE (n:Entity OR n:Episodic OR n:TurnEvidence OR n:Todo)
  AND n.uuid > $after
  AND {tenant_scope_cypher("n")}
RETURN n.uuid AS uuid, labels(n) AS labels, n.type AS type,
       n.content AS content, n.summary AS summary, n.name AS name,
       n.text AS text, n.diff AS diff
ORDER BY n.uuid
LIMIT $batch
"""


@dataclass
class ScanReport:
    namespace: str
    nodes_scanned: int = 0
    nodes_with_hits: int = 0
    by_type_and_kind: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "nodes_scanned": self.nodes_scanned,
            "nodes_with_hits": self.nodes_with_hits,
            "matches": [
                {"memory_type": mtype, "kind": kind, "nodes": count}
                for (mtype, kind), count in sorted(self.by_type_and_kind.items())
            ],
        }


def _memory_type(row: dict[str, Any]) -> str:
    if row.get("type"):
        return str(row["type"])
    labels = [label for label in (row.get("labels") or []) if label != "Entity"]
    return str(labels[0]) if labels else "Entity"


def scan_namespace(execute: Execute, namespace: str | None) -> ScanReport:
    ns = normalize_namespace(namespace)
    report = ScanReport(namespace=ns)
    after = ""
    while True:
        rows = execute(
            _SCAN_QUERY,
            {"after": after, **tenant_scope_params(ns), "batch": BATCH},
        )
        if not rows:
            return report
        for row in rows:
            report.nodes_scanned += 1
            kinds: set[str] = set()
            for name in _TEXT_FIELDS:
                value = row.get(name)
                if isinstance(value, str) and value:
                    kinds.update(hit.kind for hit in detect_secrets(value))
            if kinds:
                report.nodes_with_hits += 1
                mtype = _memory_type(row)
                for kind in kinds:
                    report.by_type_and_kind[(mtype, kind)] += 1
        after = str(rows[-1]["uuid"])
        if len(rows) < BATCH:
            return report
