"""Shared fixtures for infrastructure tests.

``fake_identity_graph`` models :ProjectIdentity closely enough to exercise the binding protocol
offline, INCLUDING the ``(bound_host, root_key)`` uniqueness constraint that is the actual
enforcement. A fake that stored rows without enforcing the constraint would let every transfer
test pass while the property under test was absent -- the fake would be re-implementing the bug.

It is still a fake, and the constraint it enforces is a Python re-statement of a Neo4j rule. The
authority for that rule is `test_cf257_identity_binding_online.py`, which runs the same protocol
against a real instance with the real constraints; these offline tests are for speed and for the
branches that are awkward to provoke against a live database.

The fake raises on any statement it does not recognise. That is deliberate: a fake that silently
returns ``[]`` for an unrecognised query turns a changed statement into a passing test.
"""

from __future__ import annotations

import re

import pytest


class ConstraintViolated(RuntimeError):
    """Stands in for Neo4j's ConstraintValidationFailed."""

    code = "Neo.ClientError.Schema.ConstraintValidationFailed"


class FakeIdentityGraph:
    """An in-memory :ProjectIdentity store with the two uniqueness constraints enforced."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.unrecognised: list[str] = []
        self.executed: list[tuple[str, dict]] = []
        self.fence_writers: list[str] = []
        self.frozen = False

    # -- constraint ---------------------------------------------------------
    def _check_root_constraint(self) -> None:
        seen: dict[tuple[str, str], str] = {}
        for pid, node in self.nodes.items():
            host, key = node.get("bound_host"), node.get("root_key")
            if host is None or key is None:  # NULL escapes a composite uniqueness constraint
                continue
            if (host, key) in seen:
                raise ConstraintViolated(
                    f"Node already exists with label `ProjectIdentity` and properties "
                    f"bound_host = {host!r}, root_key = {key!r} "
                    f"({seen[(host, key)]} vs {pid})"
                )
            seen[(host, key)] = pid

    def _commit(self, snapshot: dict[str, dict]) -> None:
        try:
            self._check_root_constraint()
        except ConstraintViolated:
            self.nodes = snapshot  # a rejected statement rolls back entirely
            raise

    def _snapshot(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self.nodes.items()}

    # -- dispatch -----------------------------------------------------------
    def execute(self, cypher, params=None, **_kw):
        params = params or {}
        text = re.sub(r"\s+", " ", cypher).strip()
        self.executed.append((text, dict(params)))
        pid = params.get("project_id")

        if text.startswith("CREATE CONSTRAINT"):
            return []

        if text.startswith("MATCH (p:Entity {structure_role: 'project'})"):
            # The candidate lookup. An aid to a human decision, never a gate -- these tests seed
            # the binding directly, so there is no candidate to offer.
            return []

        if text.startswith("MERGE (p:ProjectIdentity {project_id: $project_id}) ON CREATE SET"):
            before = self._snapshot()
            node = self.nodes.get(pid)
            if node is None:
                node = self.nodes[pid] = {
                    "canonical_root_path": params["root_path"],
                    "state": "bound",
                    "bound_host": params["host"],
                    "root_key": params["root_key"],
                    "claim_generation": 1,
                }
                self._commit(before)
            return [
                {
                    "bound_root": node["canonical_root_path"],
                    "state": node.get("state", "bound"),
                    "bound_host": node.get("bound_host"),
                    "root_key": node.get("root_key"),
                    "claim_generation": int(node.get("claim_generation") or 0),
                }
            ]

        if "SET p.state = 'conflicted'" in text:
            self.nodes[pid]["state"] = "conflicted"
            self.nodes[pid]["conflicting_root_path"] = params.get("root_path")
            return []

        if text.startswith("MATCH (p:ProjectIdentity) WHERE coalesce(p.state, 'bound') = 'bound'"):
            # binding_for_root and _active_rivals share this read.
            out = []
            for node_id, node in self.nodes.items():
                if node.get("state", "bound") != "bound":
                    continue
                if node.get("bound_host") != params.get("host"):
                    continue
                if pid is not None and node_id == pid:
                    continue
                out.append(
                    {
                        "id": node_id,
                        "root": node.get("canonical_root_path"),
                        "root_key": node.get("root_key"),
                    }
                )
            return out

        if "SET p.bound_host = $host, p.root_key = $root_key" in text:
            before = self._snapshot()
            node = self.nodes[pid]
            if node.get("state", "bound") == "bound":
                node["bound_host"] = params["host"]
                node["root_key"] = params["root_key"]
                node["canonical_root_path"] = params["root_path"]
            self._commit(before)
            return []

        if text.startswith("MATCH (n:ProjectIdentity) WHERE n.project_id IN $lock_ids"):
            # The transfer. Modelled with the same TWO gates the statement has, in the same order:
            # a writer registered against any locked identity blocks everything downstream, and
            # only then does retirement or claiming happen. A fake that skipped the writer gate
            # would pass every stale-transfer test while the property was absent.
            before = self._snapshot()
            lock_ids = params.get("lock_ids") or []
            held = sum(
                len(self.nodes.get(i, {}).get("active_writers") or []) for i in lock_ids
            )
            if held:
                # `WHERE held = 0` filtered the row out: nothing after it ran.
                return []
            for rival in params.get("rival_ids") or []:
                node = self.nodes.get(rival)
                if node is None:
                    continue
                node["previous_root_key"] = node.get("root_key")
                node["root_key"] = None
                node["state"] = "superseded"
                node["superseded_by"] = pid
            node = self.nodes.setdefault(pid, {})
            node["previous_root_path"] = node.get("canonical_root_path")
            node["canonical_root_path"] = params["root_path"]
            node["state"] = "bound"
            node["bound_host"] = params["host"]
            node["root_key"] = params["root_key"]
            node["claim_generation"] = int(node.get("claim_generation") or 0) + 1
            self._commit(before)
            return [
                {
                    "retired": len(params.get("rival_ids") or []),
                    "claim_generation": node["claim_generation"],
                }
            ]

        if text.startswith("MATCH (p:ProjectIdentity {project_id: $project_id}) SET p.last_admission_probe"):
            # Writer admission: the claim gate and the registration, one step (see the fence fake).
            node = self.nodes.get(params.get("project_id"))
            if node is None:
                return []
            if (
                node.get("state", "bound") != "bound"
                or node.get("bound_host") != params.get("host")
                or node.get("root_key") != params.get("root_key")
                or int(node.get("claim_generation") or 0) != int(params.get("generation"))
                or self.frozen
            ):
                return []
            node.setdefault("active_writers", []).append(params["entry"])
            self.fence_writers.append(params["entry"])
            return [{"active": len(self.fence_writers)}]

        if text.startswith(
            "MATCH (p:ProjectIdentity {project_id: $project_id}) SET p.active_writers"
        ):
            prefix = params["prefix"]
            node = self.nodes.get(pid)
            if node is None:
                return []
            node["active_writers"] = [
                w for w in node.get("active_writers", []) if not w.startswith(prefix)
            ]
            self.fence_writers = [
                w for w in self.fence_writers if not w.startswith(prefix)
            ]
            return []

        if "RETURN coalesce(f.frozen, false) AS frozen" in text:
            return [
                {"frozen": self.frozen, "reason": None, "writers": list(self.fence_writers)}
            ]

        if text.startswith("MATCH (p:ProjectIdentity) WHERE p.project_id IN $ids"):
            return [
                {"id": i, "writers": list(self.nodes.get(i, {}).get("active_writers") or [])}
                for i in (params.get("ids") or [])
                if i in self.nodes
            ]

        if "WHERE coalesce(p.state, 'bound') = 'conflicted' RETURN p.project_id AS id" in text:
            node = self.nodes.get(pid)
            return [{"id": pid}] if node and node.get("state") == "conflicted" else []

        if "RETURN p.canonical_root_path AS root, coalesce(p.state, 'bound') AS state" in text:
            node = self.nodes.get(pid)
            if not node:
                return []
            return [
                {
                    "root": node.get("canonical_root_path"),
                    "state": node.get("state", "bound"),
                }
            ]

        if "SET p.state = 'bound', p.conflicting_root_path = null" in text:
            node = self.nodes.get(pid)
            if node:
                node["state"] = "bound"
                node["conflicting_root_path"] = None
            return []

        self.unrecognised.append(text)
        raise AssertionError(f"unexpected statement: {text[:120]}")


@pytest.fixture
def fake_identity_graph():
    return FakeIdentityGraph()
