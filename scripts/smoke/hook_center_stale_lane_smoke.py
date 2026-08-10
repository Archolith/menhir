#!/usr/bin/env python3
"""Hook Center Stale Anchor Lane — real-DB smoke harness (v1).

Proves the full stale-file-anchor lane end-to-end against a REAL Neo4j backend:

    file/tool event
    -> file marked dirty
    -> stale file-anchored memory detected
    -> recall labels stale memory
    -> formatter/context warns agent to inspect current file
    -> stale verification receipt records inspection outcome
    -> receipt enriches stale recall only when path-aware and post-dirty

Core invariant proved: a WRONG current-state view is worse than a miss. A receipt
never marks a stale memory fresh; a wrong-path / pre-dirty / malformed receipt never
reassures.

This is a VALIDATION slice, not a feature slice. It adds no lifecycle behavior
(no auto-refresh, no dirty clearing, no down-ranking, no deletion/expiration).

How it exercises the real backend
----------------------------------
* HTTP endpoints (tool-events / dirty / stale / verification receipts) run against a
  throwaway Menhir server (self-served in a subprocess by default) that mounts the
  REAL FastAPI router over a REAL Neo4j-backed graph adapter.
* Recall labeling, the MCP formatter advisory, the context-builder warning, and the
  receipt-matching / enrichment logic run in-process through the REAL
  ``RecallService.recall()`` / ``ContextBuilderService.build_context()`` /
  ``menhir.mcp.formatters`` against the same throwaway Neo4j. Only the embedding-
  dependent graphiti vector search is seeded (it decides *which* memory is under
  test, not the behavior under test) — everything downstream is the shipped code
  path against real Cypher.

Safety
------
* Never uploads file contents. Never captures transcripts.
* Uses a clearly disposable project namespace.
* Cleanup only deletes data under the smoke project namespace; never clears dirty
  flags globally.

Usage
-----
    python scripts/smoke/hook_center_stale_lane_smoke.py \
        --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass --json

    # against an already-running server + DB (no self-serve):
    python scripts/smoke/hook_center_stale_lane_smoke.py \
        --no-self-serve --url http://127.0.0.1:8099 \
        --neo4j-uri bolt://localhost:7688 --neo4j-password smokepass

Environment fallbacks
---------------------
    MENHIR_URL / MENHIR_TOOL_EVENTS_URL   base server URL
    MENHIR_AGENT_KEY                      bearer token (agent tier)
    MENHIR_READONLY_KEY                   bearer token (readonly tier)
    NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Make `import menhir` work when run as a script from anywhere in the repo.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

RESULT_PASS = "PASS"
RESULT_PASS_WITH_SKIPS = "PASS_WITH_SKIPS"
RESULT_FAIL = "FAIL"

#: how long main() waits for the (self-served) server to answer; overridable by tests.
SERVER_READY_TIMEOUT_S = 30.0

DEFAULT_PROJECT = "smoke-hook-center-stale-lane"
DEFAULT_PATH = "src/smoke_target.py"
DEFAULT_MEMORY_UUID = "smoke-memory-001"
CONTROL_UUID = "smoke-memory-control-001"
WRONG_PATH = "src/other_file.py"
ANCHORED_AT = "2026-07-01T00:00:00Z"
QUERY = "smoke target behavior stale anchor"
# A unique token embedded in the fixture memory body. It appears when the memory is
# rendered into context, but never in the generated stale-warning prose — so the
# atomicity check can distinguish "memory present" from "warning present".
MEMORY_SENTINEL = "smoke-lane-memory-body-sentinel-7c1f"
EVENT_HASH = "smoke-synthetic-hash"

CHECK_KEYS = (
    "tool_event_accepted",
    "dirty_file_visible",
    "stale_anchor_visible",
    "recall_stale_label",
    "formatter_stale_advisory",
    "context_warning_atomic",
    "verification_receipt_recorded",
    "post_dirty_receipt_enriches",
    "wrong_path_receipt_ignored",
    "pre_dirty_receipt_ignored",
    "malformed_timestamp_conservative",
    "outdated_receipt_recommends_no_lifecycle_mutation",
)


# ---------------------------------------------------------------------------
# HTTP client (stdlib only) — unit-testable by monkeypatching urllib
# ---------------------------------------------------------------------------


class SmokeHTTPError(Exception):
    """Raised on a transport/protocol failure that should fail the smoke."""


def _resolve_base_url(explicit: str | None) -> str:
    base = explicit or os.environ.get("MENHIR_URL") or os.environ.get("MENHIR_TOOL_EVENTS_URL")
    return (base or "http://127.0.0.1:8099").rstrip("/")


def _headers(agent_key: str | None, readonly_key: str | None, *, write: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = (agent_key if write else (readonly_key or agent_key)) or ""
    key = key.strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _request(url: str, *, method: str, headers: dict[str, str], payload: dict | None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if hasattr(exc, "read") else ""
        status = exc.code
    except Exception as exc:  # transport failure -> caller decides
        raise SmokeHTTPError(f"{method} {url} failed: {exc}") from exc
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SmokeHTTPError(f"{method} {url} returned non-JSON body: {raw[:200]!r}") from exc
    return status, body


class HTTPClient:
    def __init__(self, base: str, agent_key: str | None, readonly_key: str | None) -> None:
        self.base = base
        self.agent_key = agent_key
        self.readonly_key = readonly_key

    def post_tool_event(self, project: str, path: str) -> dict:
        payload = {
            "event_type": "file_changed",
            "source_client": "smoke",
            "source_kind": "smoke",
            "project": project,
            "path": path,
            "operation": "edit",
            # NOTE: no file content, ever — only a synthetic provenance hash.
            "after_hash": EVENT_HASH,
            "metadata": {"smoke": True},
        }
        status, body = _request(f"{self.base}/api/tool-events", method="POST",
                                headers=_headers(self.agent_key, self.readonly_key, write=True),
                                payload=payload)
        if status != 200:
            raise SmokeHTTPError(f"POST /api/tool-events -> {status}: {body}")
        return body

    def get_dirty(self, project: str) -> dict:
        status, body = _request(f"{self.base}/api/tool-events/dirty?project={project}", method="GET",
                                headers=_headers(self.agent_key, self.readonly_key, write=False),
                                payload=None)
        if status != 200:
            raise SmokeHTTPError(f"GET /api/tool-events/dirty -> {status}: {body}")
        return body

    def get_stale(self, project: str) -> dict:
        status, body = _request(f"{self.base}/api/tool-events/stale?project={project}", method="GET",
                                headers=_headers(self.agent_key, self.readonly_key, write=False),
                                payload=None)
        if status != 200:
            raise SmokeHTTPError(f"GET /api/tool-events/stale -> {status}: {body}")
        return body

    def post_verification(self, payload: dict) -> tuple[int, dict]:
        return _request(f"{self.base}/api/tool-events/stale-verifications", method="POST",
                        headers=_headers(self.agent_key, self.readonly_key, write=True),
                        payload=payload)

    def get_verifications(self, memory_uuid: str) -> dict:
        status, body = _request(
            f"{self.base}/api/tool-events/stale-verifications?memory_uuid={memory_uuid}",
            method="GET",
            headers=_headers(self.agent_key, self.readonly_key, write=False),
            payload=None,
        )
        if status != 200:
            raise SmokeHTTPError(f"GET /api/tool-events/stale-verifications -> {status}: {body}")
        return body

    def wait_ready(self, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                status, _ = _request(f"{self.base}/api/tool-events/dirty", method="GET",
                                     headers=_headers(self.agent_key, self.readonly_key, write=False),
                                     payload=None)
                if status == 200:
                    return True
            except SmokeHTTPError:
                pass
            time.sleep(0.5)
        return False


# ---------------------------------------------------------------------------
# In-process backend — REAL RecallService / ContextBuilder / formatter against
# a REAL throwaway Neo4j. Only graphiti vector search is seeded.
# ---------------------------------------------------------------------------


class _SeededGraphiti:
    """Stub for the embedding-dependent vector search only. It decides which memory
    is a candidate; every downstream label/advisory/enrichment step is the real code
    path against real Cypher."""

    def __init__(self, hits: list[tuple[str, str, float]]) -> None:
        self._hits = list(hits)

    async def search_scored(self, query: str, *, num_results: int = 50, group_ids=None):
        return list(self._hits)


class Neo4jBackend:
    """Real Neo4j-backed operations for fixtures + in-process recall/context/formatter."""

    def __init__(self, uri: str, user: str, password: str, database: str) -> None:
        from menhir.infrastructure.neo4j import Neo4jRepository
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        self._neo = Neo4jRepository(uri, database, user, password)
        self.adapter = MemoryGraphAdapter(self._neo)

    # -- lifecycle ---------------------------------------------------------

    def ping(self) -> bool:
        return self._neo.ping()

    def close(self) -> None:
        self._neo.close()

    def any_smoke_data(self, project: str, uuids: list[str]) -> bool:
        # Scope entirely by the smoke_project marker (set on every fixture node) +
        # the verification receipt's project — never by UUID, so a custom
        # --memory-uuid can never widen the blast radius.
        rows = self._neo.execute(
            """
            OPTIONAL MATCH (n:Entity {smoke_project: $project})
            OPTIONAL MATCH (v:StaleAnchorVerification {project: $project})
            RETURN count(DISTINCT n) + count(DISTINCT v) AS total
            """,
            params={"project": project},
        )
        return bool(rows and int(rows[0].get("total") or 0) > 0)

    def clean(self, project: str, uuids: list[str]) -> None:
        """Delete ONLY smoke-project-scoped data. Every fixture node carries a
        ``smoke_project`` marker, so cleanup deletes strictly by that marker (and the
        receipt's project) — never by bare UUID. Never touches other projects and
        never clears dirty flags globally."""
        self._neo.execute(
            "MATCH (v:StaleAnchorVerification {project: $project}) DETACH DELETE v",
            params={"project": project},
        )
        self._neo.execute(
            "MATCH (n:Entity {smoke_project: $project}) DETACH DELETE n",
            params={"project": project},
        )

    # -- fixture -----------------------------------------------------------

    def create_fixture(self, project: str, path: str, memory_uuid: str,
                       control_uuid: str, anchored_at: str) -> None:
        """SMOKE FIXTURE ONLY — not app behavior.

        Creates the smallest real graph that yields a stale file anchor:
          (:Entity {structure_role:'file'}) <-[:ANCHORED_TO {created_at}]- (:Entity memory)
        plus a non-stale control memory. dirty_at is NOT set here — it is set by the
        real POST /api/tool-events path so the stale signal is produced by the server."""
        self._neo.execute(
            """
            // SMOKE FIXTURE ONLY — not app behavior
            // Every node carries smoke_project so teardown can delete strictly by marker.
            CREATE (f:Entity {
                uuid: randomUUID(), structure_role: 'file',
                structure_path: $path, structure_project: $project, name: $path,
                smoke_project: $project
            })
            CREATE (sem:Entity {
                uuid: $memory_uuid, name: 'smoke stale-anchored memory ' + $sentinel,
                scope: 'PERSISTENT', type: 'SEMANTIC', freshness: 'ACTIVE',
                content: 'Smoke memory ' + $sentinel + ' anchored to ' + $path + ' describing its behavior.',
                summary: 'Smoke memory ' + $sentinel + ' anchored to a file that will change after anchoring.',
                namespace: 'default', smoke_project: $project, created_at: datetime($anchored_at)
            })
            CREATE (sem)-[:ANCHORED_TO {created_at: datetime($anchored_at)}]->(f)
            CREATE (ctrl:Entity {
                uuid: $control_uuid, name: 'smoke control memory',
                scope: 'PERSISTENT', type: 'SEMANTIC', freshness: 'ACTIVE',
                content: 'Smoke control memory, not anchored to any dirty file.',
                summary: 'Smoke control memory (never stale).',
                namespace: 'default', smoke_project: $project, created_at: datetime($anchored_at)
            })
            RETURN sem.uuid AS uuid
            """,
            params={"project": project, "path": path, "memory_uuid": memory_uuid,
                    "control_uuid": control_uuid, "anchored_at": anchored_at,
                    "sentinel": MEMORY_SENTINEL},
        )

    def file_event_metadata(self, project: str, path: str) -> dict:
        """Read the dirty-marking provenance written by POST /api/tool-events so the
        smoke can assert operation + hash were stored (the /dirty endpoint surfaces
        operation but not the hash)."""
        rows = self._neo.execute(
            """
            MATCH (f:Entity {structure_role: 'file', structure_project: $project, structure_path: $path})
            RETURN f.last_event_op AS operation, f.last_event_after_hash AS after_hash
            """,
            params={"project": project, "path": path},
        )
        return dict(rows[0]) if rows else {}

    # -- in-process real recall / context / formatter ----------------------

    def _recall_service(self, project: str, hits: list[tuple[str, str, float]]):
        from menhir.services.recall_service import RecallService
        from menhir.services.scoring_service import ScoringService

        return RecallService(
            graphiti_client=_SeededGraphiti(hits),
            graph_adapter=self.adapter,
            scoring_service=ScoringService(),
        )

    async def recall_items(self, project: str, hits: list[tuple[str, str, float]]) -> list[dict]:
        """Run the REAL recall path and return, per result, the raw recall stale label
        plus the REAL MCP formatter output."""
        from menhir.mcp.formatters import _compact_scored_item

        svc = self._recall_service(project, hits)
        result = await svc.recall(QUERY, file_context_project=project, update_access=False)
        items = []
        for sm in result.results:
            items.append({
                "uuid": sm.uuid,
                "stale_anchor_info": sm.stale_anchor_info,      # recall-labeled truth
                "formatted": _compact_scored_item(sm, compact=False),  # real formatter
            })
        return items

    async def build_context(self, project: str, hits: list[tuple[str, str, float]],
                            max_tokens: int) -> str:
        from menhir.services.context_builder import ContextBuilderService

        svc = ContextBuilderService(recall_service=self._recall_service(project, hits),
                                    graph_adapter=self.adapter)
        result = await svc.build_context(QUERY, max_tokens=max_tokens)
        return result.context

    # -- lifecycle assertions for the no-mutation checks -------------------

    def memory_exists(self, memory_uuid: str) -> bool:
        rows = self._neo.execute(
            "MATCH (n:Entity {uuid: $uuid}) RETURN count(n) AS c",
            params={"uuid": memory_uuid},
        )
        return bool(rows and int(rows[0].get("c") or 0) > 0)

    def dirty_flag_set(self, project: str, path: str) -> bool:
        rows = self._neo.execute(
            """
            MATCH (f:Entity {structure_role: 'file', structure_project: $project, structure_path: $path})
            RETURN f.structure_dirty AS dirty
            """,
            params={"project": project, "path": path},
        )
        return bool(rows and rows[0].get("dirty") is True)

    def stale_count(self, project: str) -> int:
        return len(self.adapter.stale_anchored_memories(project=project, limit=200))


# ---------------------------------------------------------------------------
# Throwaway server (self-serve): mount the REAL router over the REAL adapter.
# No full runtime bootstrap, no embedder, no scheduler.
# ---------------------------------------------------------------------------


def _quiet_neo4j_logs() -> None:
    """Silence the neo4j driver's benign 'UnknownPropertyKey' server notifications —
    the throwaway DB has no indexes/props yet, and the smoke's own output is the signal."""
    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


def _serve(args: argparse.Namespace) -> int:
    from types import SimpleNamespace
    _quiet_neo4j_logs()
    import uvicorn
    from fastapi import FastAPI
    from menhir.api.routes import router
    from menhir.infrastructure.neo4j import Neo4jRepository
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    neo = Neo4jRepository(args.neo4j_uri, args.neo4j_database, args.neo4j_user, args.neo4j_password)
    adapter = MemoryGraphAdapter(neo)
    app = FastAPI()
    app.include_router(router)
    # Routes only need runtime_ctx.built.graph_adapter (auth disabled: no keys configured).
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=adapter),
        capabilities=None,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _spawn_server(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable, os.path.abspath(__file__), "serve",
        "--host", "127.0.0.1", "--port", str(args.port),
        "--neo4j-uri", args.neo4j_uri, "--neo4j-user", args.neo4j_user,
        "--neo4j-password", args.neo4j_password, "--neo4j-database", args.neo4j_database,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _shift_iso(base_iso: str, *, minutes: int = 0, days: int = 0) -> str:
    """Return ``base_iso`` shifted by the given delta, formatted as UTC ISO-8601 with a
    trailing Z. Derived from the DB's actual ``dirty_at`` (not the smoke process clock),
    so verified_at ordering holds regardless of clock skew between smoke and Neo4j."""
    try:
        dt = datetime.fromisoformat(base_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutes, days=days)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _find(items: list[dict], uuid: str) -> dict | None:
    for it in items:
        if it["uuid"] == uuid:
            return it
    return None


def run_smoke(config: argparse.Namespace, http: HTTPClient, backend, out) -> dict:
    """Drive the full lane. Returns the JSON summary dict. `out` is a diagnostic sink
    (callable taking a str) that writes to stderr / stays out of JSON stdout."""
    project = config.project
    path = config.path
    memory_uuid = config.memory_uuid
    uuids = [memory_uuid, CONTROL_UUID]
    hits = [(memory_uuid, "smoke stale-anchored memory", 0.85),
            (CONTROL_UUID, "smoke control memory", 0.80)]

    checks: dict[str, bool] = {}
    skipped: dict[str, str] = {}
    limitations: list[str] = []

    def run_recall_sync(fn):
        import asyncio
        return asyncio.run(fn)

    # --- clean slate ---
    if config.require_clean_start and backend.any_smoke_data(project, uuids):
        raise SmokeHTTPError(
            f"--require-clean-start: pre-existing smoke data for project {project!r}")
    backend.clean(project, uuids)
    backend.create_fixture(project, path, memory_uuid, CONTROL_UUID, ANCHORED_AT)
    out(f"fixture created: project={project} path={path} memory_uuid={memory_uuid}")

    # --- 1. tool event accepted (marks file dirty) ---
    resp = http.post_tool_event(project, path)
    checks["tool_event_accepted"] = bool(resp.get("accepted")) and bool(resp.get("marked_dirty"))
    out(f"[1] tool_event_accepted: accepted={resp.get('accepted')} marked_dirty={resp.get('marked_dirty')}")

    # --- 2. dirty file visible + dirty metadata written (path, dirty_at, operation, hash) ---
    dirty = http.get_dirty(project)
    dirty_files = dirty.get("dirty_files", [])
    dfile = next((d for d in dirty_files if d.get("path") == path), None)
    meta = backend.file_event_metadata(project, path)
    checks["dirty_file_visible"] = (
        bool(dfile) and bool(dfile.get("dirty_at"))
        and dfile.get("operation") == "edit"            # operation surfaced by the endpoint
        and meta.get("operation") == "edit"             # operation persisted on the node
        and meta.get("after_hash") == EVENT_HASH        # provenance hash persisted (not exposed by /dirty)
    )
    out(f"[2] dirty_file_visible: {bool(dfile)} dirty_at={dfile.get('dirty_at') if dfile else None} "
        f"op={dfile.get('operation') if dfile else None} hash={meta.get('after_hash')}")

    # --- 3. stale anchor visible (endpoint) ---
    stale = http.get_stale(project)
    anchors = stale.get("stale_anchors", [])
    sa = next((a for a in anchors if a.get("memory_uuid") == memory_uuid), None)
    checks["stale_anchor_visible"] = bool(sa) and sa.get("path") == path and bool(sa.get("dirty_at")) \
        and bool(sa.get("anchored_at"))
    # Derive verification timestamps from the DB's real dirty_at (not the smoke clock),
    # so pre/post-dirty ordering holds regardless of clock skew.
    dirty_at_iso = (sa or {}).get("dirty_at") or (dfile or {}).get("dirty_at") \
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out(f"[3] stale_anchor_visible: {bool(sa)} path={sa.get('path') if sa else None}")

    # --- 4. recall gets stale label (real RecallService.recall) ---
    if config.skip_recall:
        skipped["recall_stale_label"] = "--skip-recall passed"
        skipped["formatter_stale_advisory"] = "--skip-recall passed"
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        stale_item = _find(items, memory_uuid)
        ctrl_item = _find(items, CONTROL_UUID)
        info = (stale_item or {}).get("stale_anchor_info") or {}
        ctrl_info = (ctrl_item or {}).get("stale_anchor_info") or {}
        checks["recall_stale_label"] = (
            bool(stale_item) and info.get("stale_anchor") is True
            and info.get("stale_reason") == "file_changed_after_anchor"
            and info.get("path") == path
            and bool(info.get("dirty_at")) and bool(info.get("anchored_at"))
            and bool(ctrl_item) and ctrl_info.get("stale_anchor") is False
        )
        out(f"[4] recall_stale_label: stale={info.get('stale_anchor')} "
            f"control_stale={ctrl_info.get('stale_anchor')}")

        # --- 5. formatter advisory (real menhir.mcp.formatters) ---
        fmt = (stale_item or {}).get("formatted") or {}
        ctrl_fmt = (ctrl_item or {}).get("formatted") or {}
        checks["formatter_stale_advisory"] = (
            fmt.get("stale_anchor") is True
            and fmt.get("stale_action") == "verify_current_file_before_relying"
            and "Inspect the current file" in str(fmt.get("stale_advisory") or "")
            and "stale_action" not in ctrl_fmt and "stale_advisory" not in ctrl_fmt
        )
        out(f"[5] formatter_stale_advisory: action={fmt.get('stale_action')}")

    # --- 6. context builder warning is atomic ---
    if config.skip_context_builder:
        skipped["context_warning_atomic"] = "--skip-context-builder passed"
    elif config.skip_recall:
        skipped["context_warning_atomic"] = "context depends on recall (--skip-recall passed)"
    else:
        big = run_recall_sync(backend.build_context(project, hits, 5000))
        tiny = run_recall_sync(backend.build_context(project, hits, 1))
        # Large budget: the stale memory AND its warning both appear.
        both_present = ("Stale file anchor" in big and path in big
                        and "inspect the current file" in big.lower()
                        and MEMORY_SENTINEL in big)
        # Atomicity: at a budget too tight for memory + warning, NEITHER appears — not the
        # memory without its warning (that would be the "wrong current-state view" failure).
        neither_present = ("Stale file anchor" not in tiny and MEMORY_SENTINEL not in tiny)
        checks["context_warning_atomic"] = both_present and neither_present
        out(f"[6] context_warning_atomic: both_present={both_present} neither_present={neither_present}")

    # --- 9 + 10 (before any valid post-dirty receipt): wrong-path + pre-dirty ignored ---
    st, wp_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": WRONG_PATH,
        "outcome": "still_valid", "verified_at": _shift_iso(dirty_at_iso, minutes=5),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    if st != 200:
        raise SmokeHTTPError(f"wrong-path receipt POST -> {st}: {wp_body}")
    st, pd_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "still_valid", "verified_at": _shift_iso(dirty_at_iso, days=-1),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    if st != 200:
        raise SmokeHTTPError(f"pre-dirty receipt POST -> {st}: {pd_body}")

    if config.skip_recall:
        skipped["wrong_path_receipt_ignored"] = "--skip-recall passed"
        skipped["pre_dirty_receipt_ignored"] = "--skip-recall passed"
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        info = (_find(items, memory_uuid) or {}).get("stale_anchor_info") or {}
        no_enrich = info.get("stale_anchor") is True and info.get("stale_verification") is None
        # At this point only wrong-path + pre-dirty receipts exist; neither may enrich.
        checks["wrong_path_receipt_ignored"] = no_enrich
        checks["pre_dirty_receipt_ignored"] = no_enrich
        out(f"[9/10] wrong_path + pre_dirty ignored: no_enrich={no_enrich} "
            f"verification={info.get('stale_verification')}")

    # --- 7 + 8: valid post-dirty still_valid receipt records + enriches ---
    st, v_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "still_valid", "verified_at": _shift_iso(dirty_at_iso, minutes=5),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    recorded = st == 200 and bool(v_body.get("accepted"))
    listed = http.get_verifications(memory_uuid)
    has_listed = any(v.get("path") == path and v.get("outcome") == "still_valid"
                     for v in listed.get("verifications", []))
    checks["verification_receipt_recorded"] = recorded and has_listed
    out(f"[7] verification_receipt_recorded: recorded={recorded} listed={has_listed}")

    if config.skip_recall:
        skipped["post_dirty_receipt_enriches"] = "--skip-recall passed"
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        info = (_find(items, memory_uuid) or {}).get("stale_anchor_info") or {}
        ver = info.get("stale_verification") or {}
        checks["post_dirty_receipt_enriches"] = (
            info.get("stale_anchor") is True            # still stale, never marked fresh
            and ver.get("outcome") == "still_valid"
            and ver.get("verified_by") == "smoke-agent"
            and ver.get("basis") == "inspected_current_file"
        )
        out(f"[8] post_dirty_receipt_enriches: stale={info.get('stale_anchor')} "
            f"outcome={ver.get('outcome')}")

    # --- 11. malformed timestamp is rejected (never stored / never reassures) ---
    st, mal_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "still_valid", "verified_at": "not-a-timestamp",
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    checks["malformed_timestamp_conservative"] = st == 400
    out(f"[11] malformed_timestamp_conservative: status={st}")

    # --- 12. outdated receipt recommends update/supersede, mutates no lifecycle ---
    # Strictly later than the still_valid receipt so it is the latest post-dirty one.
    st, o_body = http.post_verification({
        "memory_uuid": memory_uuid, "project": project, "path": path,
        "outcome": "outdated", "verified_at": _shift_iso(dirty_at_iso, minutes=10),
        "verified_by": "smoke-agent", "basis": "inspected_current_file",
    })
    outdated_recorded = st == 200 and bool(o_body.get("accepted"))
    if config.skip_recall:
        # Still assert the no-lifecycle-mutation invariants (they do not need recall).
        no_mutation = (backend.memory_exists(memory_uuid)
                       and backend.dirty_flag_set(project, path)
                       and backend.stale_count(project) >= 1)
        checks["outdated_receipt_recommends_no_lifecycle_mutation"] = outdated_recorded and no_mutation
        out(f"[12] outdated (recall skipped): recorded={outdated_recorded} no_mutation={no_mutation}")
    else:
        items = run_recall_sync(backend.recall_items(project, hits))
        st_item = _find(items, memory_uuid) or {}
        info = st_item.get("stale_anchor_info") or {}
        fmt = st_item.get("formatted") or {}
        no_mutation = (backend.memory_exists(memory_uuid)
                       and backend.dirty_flag_set(project, path)
                       and backend.stale_count(project) >= 1)
        checks["outdated_receipt_recommends_no_lifecycle_mutation"] = (
            outdated_recorded
            and info.get("stale_anchor") is True                        # still stale
            and (info.get("stale_verification") or {}).get("outcome") == "outdated"
            and fmt.get("stale_action") == "do_not_rely_update_or_supersede"
            and no_mutation                                             # no delete/expire/clear
        )
        out(f"[12] outdated: action={fmt.get('stale_action')} no_mutation={no_mutation}")

    # --- compute result ---
    for key in CHECK_KEYS:
        if key not in checks and key not in skipped:
            skipped[key] = "not evaluated"

    failed = [k for k in CHECK_KEYS if checks.get(k) is False]
    if failed:
        result = RESULT_FAIL
    elif skipped:
        result = RESULT_PASS_WITH_SKIPS
    else:
        result = RESULT_PASS

    summary = {
        "result": result,
        "project": project,
        "path": path,
        "memory_uuid": memory_uuid,
        "checks": {k: checks[k] for k in CHECK_KEYS if k in checks},
        "safety": {
            "throwaway_project_used": True,
            "no_file_content_uploaded": True,
            "no_transcript_captured": True,
            "no_phase_3_changes": True,
            "no_turn_evidence_changes": True,
            "no_dirty_clearing": True,
            "no_auto_refresh": True,
        },
        "limitations": limitations,
    }
    if skipped:
        summary["skipped"] = skipped
    if failed:
        summary["failed"] = failed
    return summary


# ---------------------------------------------------------------------------
# Seams (monkeypatched by unit tests)
# ---------------------------------------------------------------------------


def _make_backend(config: argparse.Namespace):
    return Neo4jBackend(config.neo4j_uri, config.neo4j_user,
                        config.neo4j_password, config.neo4j_database)


def _maybe_spawn_server(config: argparse.Namespace):
    if config.no_self_serve:
        return None
    return _spawn_server(config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Hook Center stale-anchor lane real-DB smoke")
    sub = p.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the throwaway server (internal)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8099)
    serve.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7688"))
    serve.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    serve.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "smokepass"))
    serve.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))

    p.add_argument("--url", default=None, help="base server URL (default self-served)")
    p.add_argument("--port", type=int, default=8099, help="self-serve port")
    p.add_argument("--no-self-serve", action="store_true",
                   help="use an already-running server at --url")
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--path", default=DEFAULT_PATH)
    p.add_argument("--memory-uuid", default=DEFAULT_MEMORY_UUID)
    p.add_argument("--agent-key", default=os.environ.get("MENHIR_AGENT_KEY"))
    p.add_argument("--readonly-key", default=os.environ.get("MENHIR_READONLY_KEY"))
    p.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7688"))
    p.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "smokepass"))
    p.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    p.add_argument("--json", action="store_true", help="emit JSON only on stdout")
    p.add_argument("--keep-data", action="store_true", help="do not delete smoke data")
    p.add_argument("--skip-context-builder", action="store_true")
    p.add_argument("--skip-recall", action="store_true")
    p.add_argument("--require-clean-start", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)

    _quiet_neo4j_logs()
    use_json = args.json

    def out(msg: str = "") -> None:
        # Diagnostics never pollute JSON stdout.
        print(msg, file=sys.stderr if use_json else sys.stdout)

    if not args.url:
        args.url = f"http://127.0.0.1:{args.port}"
    base = _resolve_base_url(args.url)
    http = HTTPClient(base, args.agent_key, args.readonly_key)

    server = None
    backend = None
    summary: dict = {}
    try:
        server = _maybe_spawn_server(args)
        if not http.wait_ready(SERVER_READY_TIMEOUT_S):
            raise SmokeHTTPError(f"server not reachable at {base}")
        out(f"server ready: {base}")

        backend = _make_backend(args)
        if not backend.ping():
            raise SmokeHTTPError(f"neo4j not reachable at {args.neo4j_uri}")

        summary = run_smoke(args, http, backend, out)

        if not args.keep_data:
            backend.clean(args.project, [args.memory_uuid, CONTROL_UUID])
            out("smoke data cleaned")
    except SmokeHTTPError as exc:
        summary = {
            "result": RESULT_FAIL, "project": args.project, "path": args.path,
            "memory_uuid": args.memory_uuid, "error": str(exc),
            "checks": {}, "limitations": [f"aborted: {exc}"],
        }
        out(f"FAIL: {exc}")
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except Exception:
                server.kill()

    if use_json:
        print(json.dumps(summary))
    else:
        out("")
        out(f"Result: {summary.get('result')}")
        for k in CHECK_KEYS:
            if k in summary.get("checks", {}):
                out(f"  {'PASS' if summary['checks'][k] else 'FAIL'}  {k}")
        for k, reason in (summary.get("skipped") or {}).items():
            out(f"  SKIP  {k}: {reason}")

    return 0 if summary.get("result") != RESULT_FAIL else 1


def _cli() -> int:
    """Self-serve via the unified launcher unless an external target is given.

    Default: bring up a throwaway Neo4j-backed Menhir through the shared launcher
    (free port + instance-id handshake + disposable Docker Neo4j, torn down after)
    and run the smoke against it in ``--no-self-serve`` mode — writing graph
    fixtures directly to the same throwaway Neo4j. Requires Docker (or
    MENHIR_TEST_NEO4J_URI). Explicit ``--no-self-serve``/``--url`` or MENHIR_URL /
    MENHIR_TOOL_EVENTS_URL skips the launch and targets the running server.
    """
    argv = sys.argv[1:]
    external = (
        "--no-self-serve" in argv or "--url" in argv
        or os.environ.get("MENHIR_URL") or os.environ.get("MENHIR_TOOL_EVENTS_URL")
    )
    if external:
        return main()

    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
    from dev.test_server import launch

    with launch("no-auth", backend="neo4j") as srv:
        return main([
            "--no-self-serve", "--url", srv.base_url,
            "--neo4j-uri", srv.neo4j_uri, "--neo4j-user", srv.neo4j_user,
            "--neo4j-password", srv.neo4j_password, "--neo4j-database", "neo4j",
            *argv,
        ])


if __name__ == "__main__":
    raise SystemExit(_cli())
