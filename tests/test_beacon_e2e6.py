"""Menhir MVP E2E-6 — real ingest to Beacon generation and consumption.

Reproduces the E2E-6 checklist from the local-stdio MVP release plan against
a fixture repository that is REALLY ingested first: ``execute_project_ingest``
drives the production ``RuntimeProvider.scan_and_write_project`` path
(path guard, identity settlement, ``ProjectScanner``, graph write) against the
disposable ``test_neo4j_repo`` instance, and Beacon generation reads the same
graph back through the same reader class the ``menhir beacon generate`` CLI
uses. Only the semantic-episode queue is stubbed (the online lane has no LLM
and no generation step consumes semantics); the scanner, identity logic,
graph writer, graph reader, and Beacon boundary are all production code.

Requires MENHIR_TEST_BEACON_PYTHON (absolute path to a Python interpreter
whose Beacon CLI supports build+validate); there is no stub or skip fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from menhir.core.backend_runtime import RuntimeProvider
from menhir.core.backend_shared import _drain_background_errors
from menhir.domain.project_id_file import mint_identity
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.structure_queries import StructureGraphWriter
from menhir.services.beacon_generation import BeaconGenerationError, generate_beacon
from menhir.services.project_ingest import execute_project_ingest

pytestmark = [pytest.mark.online]

#: Distinctive fixture sentences. The orientation sentence becomes the indexed
#: project description (the scanner reads .agent/README.md first); the
#: architecture fact must survive as its own document entity. The changed-fact
#: phase swaps ONLY the orientation sentence.
ORIENTATION = "Conduit archives device telemetry behind a single ingest relay."
ORIENTATION_CHANGED = "Conduit replays archived telemetry through a refresh-safe gateway."
ARCHITECTURE_FACT = "Conduit's architecture layers the relay above a typed archive store."


@pytest.fixture
def beacon_python() -> str:
    raw = os.environ.get("MENHIR_TEST_BEACON_PYTHON", "").strip()
    if not raw:
        pytest.fail(
            "MENHIR_TEST_BEACON_PYTHON is not set: E2E-6 launches Beacon's real "
            "stdio server. Set it to an absolute interpreter path with Beacon's "
            "build+validate contract available."
        )
    interpreter = Path(raw)
    if not interpreter.is_absolute() or not interpreter.is_file():
        pytest.fail(
            f"MENHIR_TEST_BEACON_PYTHON must be an absolute interpreter path, got {raw!r}"
        )
    return str(interpreter)


def _fixture_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "conduit-fixture"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "relay.py").write_text(
        "def relay(packet: str) -> str:\n"
        "    \"\"\"Forward one telemetry packet to the archive.\"\"\"\n"
        "    return packet.strip()\n",
        encoding="utf-8",
    )
    agent = tmp_path / ".agent"
    agent.mkdir()
    (agent / "README.md").write_text(
        f"# Conduit\n\n{ORIENTATION}\n", encoding="utf-8"
    )
    (agent / "architecture.md").write_text(
        f"# Architecture\n\n{ARCHITECTURE_FACT}\n", encoding="utf-8"
    )
    # An established checkout carries the identity plumbing: the ignore rule
    # menhir expects repos to have, and the identity file minted through
    # menhir's own writer. Pre-minting keeps BOTH scans over an identical
    # tree, so the changed-fact phase's manifest delta is exactly the
    # orientation claim plus the cited fingerprint -- not identity-file
    # publication noise.
    (agent / ".gitignore").write_text(
        "# menhir project identity (CF-257): per-checkout, never committed\nproject-id\n",
        encoding="utf-8",
    )
    mint_identity(tmp_path)
    return tmp_path


class _NoSemanticsRuntime(RuntimeProvider):
    """Production ingest path with ONLY the semantic-queue step stubbed.

    The online CI lane has no LLM, and nothing in this E2E consumes semantic
    memory, so queueing an enrichment episode is outside the asserted boundary.
    Everything else — path guard, identity settlement, scanner, graph writer —
    is the inherited production implementation.
    """

    async def queue_episode(
        self, text: str, *, user_id: str, session_id: str, source: str
    ) -> dict:
        return {}


async def _ingest_and_await_write(
    provider: RuntimeProvider,
    *,
    root: Path,
    project: str,
    session_id: str,
    force: bool,
) -> None:
    """Ingest through the real application path and wait for the graph write.

    ``scan_and_write_project`` schedules the write as a named background task
    and reports success BEFORE the graph holds anything, and its ``_do_write``
    swallows exceptions into the session's background-error bucket — so the
    awaited task plus the drained bucket plus the caller's graph assertions
    are the only proof the write actually landed.
    """
    outcome = await execute_project_ingest(
        provider,
        path=str(root),
        name=project,
        force=force,
        session_id=session_id,
        user_id="e2e6-user",
    )
    assert outcome.error is None, outcome.error
    assert outcome.background is True, "expected the background graph-write path"
    assert not outcome.skipped
    # No await has yielded to the loop past task creation yet, but if the write
    # somehow finished already the graph assertions below still guard the gap.
    write_tasks = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == f"menhir-ingest-{project}"
    ]
    assert len(write_tasks) <= 1
    for task in write_tasks:
        await asyncio.wait_for(task, timeout=120)
    assert _drain_background_errors(session_id) == []


def _assert_graph_fresh(
    adapter: MemoryGraphAdapter, project: str, repo: Path, expected_description: str
) -> dict:
    """Assert the disposable graph holds a complete, current index of *repo*."""
    stored_root = adapter.get_project_root_path(project)
    assert stored_root, "graph has no root path for the ingested project"
    assert Path(stored_root).resolve() == repo.resolve()

    fingerprint = adapter.get_scan_fingerprint(project)
    assert fingerprint, "graph has no scan fingerprint for the ingested project"

    overview = adapter.query_structure(project, "overview")
    # Coverage rides on the overview result (query_overview runs
    # get_project_coverage as its own production round trip).
    coverage = overview.get("coverage") or {}
    assert coverage.get("known"), f"coverage unknown after ingest: {coverage}"
    assert not coverage.get("partial_index"), f"index partial after ingest: {coverage}"

    assert expected_description in str(overview.get("description"))
    assert overview.get("stack") == "python", str(overview.get("stack"))
    assert sum(overview.get("entities", {}).values()) > 0
    assert sum(overview.get("edges", {}).values()) > 0
    # Counts are whatever the real scanner indexed (files include the
    # document-role agents docs); the point is they are non-empty and that
    # Beacon later cites the EXACT same numbers, not that a fixture guess
    # matches the scanner's classification.
    assert overview["entities"].get("document", 0) >= 2  # the two .agent docs
    assert overview["entities"].get("file", 0) >= 1  # src/relay.py at minimum

    documents = {row.get("path") for row in adapter.query_documents(project)}
    assert ".agent/README.md" in documents
    assert ".agent/architecture.md" in documents
    return overview


def _run_beacon(
    beacon_python: str, args: list[str], cwd: Path
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603 - fixed argv, local fixture only
        [beacon_python, "-m", "beacon", *args],
        capture_output=True,
        check=False,
        cwd=str(cwd),
    )


def _tool_payload(result: object) -> dict:
    """Structured payload from a fastmcp CallToolResult (Beacon tools return JSON)."""
    candidates: list[object] = []
    data = getattr(result, "data", None)
    if data is not None:
        candidates.append(data)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            candidates.append(text)
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_e2e6_beacon_owned_generation_and_consumption(
    beacon_python: str, test_neo4j_repo, tmp_path: Path
) -> None:
    repo = _fixture_repo(tmp_path)
    project = f"e2e6-conduit-{uuid.uuid4().hex[:8]}"
    # The adapter is the production graph seam the ingest writes through; the
    # reader for generation is the production reader class over the SAME
    # disposable repository (exactly what `menhir beacon generate` passes).
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    reader = StructureGraphWriter(neo4j=test_neo4j_repo)
    provider = _NoSemanticsRuntime(
        built=SimpleNamespace(graph_adapter=adapter), process_session=None
    )

    # 0. real ingest through execute_project_ingest -> scan_and_write_project,
    # then verify the graph is fresh BEFORE any Beacon contact. The fixture
    # carries its identity file, so settlement takes the common
    # established-checkout path and no operator decision is needed.
    await _ingest_and_await_write(
        provider,
        root=repo,
        project=project,
        session_id=f"{project}-initial",
        force=False,
    )
    overview = _assert_graph_fresh(adapter, project, repo, ORIENTATION)
    first_fingerprint = adapter.get_scan_fingerprint(project)

    # 1. generate through the Beacon-owned build path; 2. artifact exists.
    outcome = generate_beacon(reader, project, repo, beacon_python=beacon_python)
    manifest = repo / "beacon.generated.yaml"
    assert outcome.created and manifest.is_file()
    first_bytes = manifest.read_bytes()
    # The .agent docs are scanner-written and carry no document_type; they must publish the
    # documented default role, never the stringified None (PR #125 F1).
    canonical_roles = [
        doc["role"] for doc in yaml.safe_load(first_bytes.decode("utf-8"))["canonical_docs"]
    ]
    assert canonical_roles and set(canonical_roles) == {"generic"}, canonical_roles

    # 3. beacon validate: zero validation errors.
    validated = _run_beacon(beacon_python, ["validate", str(manifest)], repo)
    assert validated.returncode == 0, validated.stderr.decode("utf-8", errors="replace")
    assert b"0 errors" in validated.stdout

    # 4. beacon inspect succeeds.
    inspected = _run_beacon(beacon_python, ["inspect", str(manifest)], repo)
    assert inspected.returncode == 0, inspected.stderr.decode("utf-8", errors="replace")
    assert b"5 tools responded" in inspected.stdout

    # 5-8. launch Beacon's own stdio server on the generated manifest and
    # query overview, task-scoped onboarding, and a generated concept.
    async def _drive_stdio() -> dict:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=beacon_python,
            args=["-m", "beacon", "serve", "-m", str(manifest)],
            cwd=str(repo),
        )
        async with Client(transport) as client:
            overview = _tool_payload(
                await client.call_tool("beacon_project_overview", {})
            )
            onboarding = _tool_payload(
                await client.call_tool(
                    "beacon_agent_onboarding", {"task_hint": "architecture"}
                )
            )
            concepts = overview.get("core_components") or []
            concept_name = (
                concepts[0].split(" (")[0] if concepts else "Project structure"
            )
            concept = _tool_payload(
                await client.call_tool(
                    "beacon_explain_concept", {"concept": concept_name}
                )
            )
            search = _tool_payload(
                await client.call_tool("beacon_search", {"query": concept_name})
            )
            guardrails = _tool_payload(await client.call_tool("beacon_guardrails", {}))
        return {
            "overview": overview,
            "onboarding": onboarding,
            "concept": concept,
            "search": search,
            "guardrails": guardrails,
            "concept_name": concept_name,
        }

    served = await _drive_stdio()

    # 9. returned claims trace to the indexed graph material.
    overview_text = served["overview"].get("summary") or json.dumps(served["overview"])
    assert ORIENTATION in str(overview_text)
    concept_payload = served["concept"]
    concept_text = str(concept_payload.get("definition") or json.dumps(concept_payload))
    assert "Indexed repository structure" in concept_text  # projected from evidence
    assert f"file: {overview['entities']['file']}" in concept_text  # exact indexed count
    onboarding_text = str(
        served["onboarding"].get("orientation") or json.dumps(served["onboarding"])
    )
    assert ORIENTATION in onboarding_text
    assert served["concept_name"] in str(
        served["search"].get("answer") or ""
    ) or served["search"].get("results")

    # 10. regenerate without source or graph changes: deterministic bytes.
    outcome = generate_beacon(
        reader,
        project,
        repo,
        beacon_python=beacon_python,
        refresh=True,
        expected_sha256=hashlib.sha256(first_bytes).hexdigest(),
    )
    assert manifest.read_bytes() == first_bytes
    assert outcome.sha256 == hashlib.sha256(first_bytes).hexdigest()

    # 11. A source edit without re-ingest makes the graph stale. Generation
    # refuses before Beacon/publication runs and preserves the existing bytes.
    (repo / ".agent" / "README.md").write_text(
        f"# Conduit\n\n{ORIENTATION_CHANGED}\n", encoding="utf-8"
    )
    before_stale_attempt = manifest.read_bytes()
    with pytest.raises(BeaconGenerationError, match="stale"):
        generate_beacon(
            reader,
            project,
            repo,
            beacon_python=beacon_python,
            refresh=True,
            expected_sha256=hashlib.sha256(before_stale_attempt).hexdigest(),
        )
    assert manifest.read_bytes() == before_stale_attempt

    # 12. First complete ingest -> refresh cycle after the source edit.
    await _ingest_and_await_write(
        provider,
        root=repo,
        project=project,
        session_id=f"{project}-refresh",
        force=True,
    )
    second_fingerprint = adapter.get_scan_fingerprint(project)
    assert second_fingerprint != first_fingerprint
    refreshed_overview = adapter.query_structure(project, "overview")
    assert ORIENTATION_CHANGED in str(refreshed_overview.get("description"))
    assert ORIENTATION not in str(refreshed_overview.get("description"))

    generate_beacon(
        reader,
        project,
        repo,
        beacon_python=beacon_python,
        refresh=True,
        expected_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    after_bytes = manifest.read_bytes()
    assert after_bytes != first_bytes, "refresh produced identical bytes after a source change"

    # Normalized semantic comparison (issue #120 refresh contract): the only
    # allowed knowledge delta is the orientation claim and the scan
    # fingerprint Beacon cites alongside it.
    def _normalize(value):
        if isinstance(value, str):
            for token in (ORIENTATION, ORIENTATION_CHANGED):
                value = value.replace(token, "<orientation>")
            for token in (first_fingerprint, second_fingerprint):
                value = value.replace(token, "<fingerprint>")
            return value
        if isinstance(value, dict):
            return {key: _normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_normalize(item) for item in value]
        return value

    before_doc = yaml.safe_load(first_bytes.decode("utf-8"))
    after_doc = yaml.safe_load(after_bytes.decode("utf-8"))
    assert _normalize(before_doc) == _normalize(after_doc), (
        "refresh changed knowledge beyond the edited orientation claim"
    )
    joined_after = after_bytes.decode("utf-8")
    assert ORIENTATION_CHANGED in joined_after
    assert ORIENTATION not in joined_after

    # 13. Second complete ingest -> refresh cycle with no source change. The
    # generated artifact's replaced mtime must not perturb the scan, so both
    # the graph fingerprint and manifest bytes converge to a fixed point.
    await _ingest_and_await_write(
        provider,
        root=repo,
        project=project,
        session_id=f"{project}-convergence",
        force=True,
    )
    third_fingerprint = adapter.get_scan_fingerprint(project)
    assert third_fingerprint == second_fingerprint
    converged = generate_beacon(
        reader,
        project,
        repo,
        beacon_python=beacon_python,
        refresh=True,
        expected_sha256=hashlib.sha256(after_bytes).hexdigest(),
    )
    assert manifest.read_bytes() == after_bytes
    assert converged.sha256 == hashlib.sha256(after_bytes).hexdigest()

    # 14. Menhir's application ingest fed the graph; the artifact validated,
    # inspected, and served through Beacon alone.
