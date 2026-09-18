"""Menhir MVP E2E-6 — Beacon generation and consumption, Beacon-owned.

Reproduces the E2E-6 checklist from the local-stdio MVP release plan against
the same indexed fixture repository, entirely through Beacon-owned
generation: Menhir dumps evidence, ``beacon build`` generates the artifact,
and Beacon's own CLI + stdio MCP server consume it. Menhir is out of the
loop the moment generation returns.

Requires MENHIR_TEST_BEACON_PYTHON (absolute path to a Python interpreter
whose Beacon CLI supports build+validate); there is no stub or skip fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from menhir.services.beacon_generation import generate_beacon
from tests.test_beacon_evidence import _reader


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
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(
        "# Fixture project for the evidence dump.\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "architecture.md").write_text(
        "# Architecture\nLayered design.\n", encoding="utf-8"
    )
    return tmp_path


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


def test_e2e6_beacon_owned_generation_and_consumption(
    beacon_python: str, tmp_path: Path
) -> None:
    repo = _fixture_repo(tmp_path)
    reader = _reader(root=str(repo))
    description = "Fixture project for the evidence dump."

    # 1. generate through the Beacon-owned build path; 2. artifact exists.
    outcome = generate_beacon(reader, "fixture", repo, beacon_python=beacon_python)
    manifest = repo / "beacon.generated.yaml"
    assert outcome.created and manifest.is_file()
    first_bytes = manifest.read_bytes()

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

    served = asyncio.run(_drive_stdio())

    # 9. returned claims trace to the generated manifest / source material.
    overview_text = served["overview"].get("summary") or json.dumps(served["overview"])
    assert description in str(overview_text)
    concept_payload = served["concept"]
    concept_text = str(concept_payload.get("definition") or json.dumps(concept_payload))
    assert "Indexed repository structure" in concept_text  # projected from evidence
    assert "file: 3" in concept_text  # the exact indexed count, cited verbatim
    onboarding_text = str(
        served["onboarding"].get("orientation") or json.dumps(served["onboarding"])
    )
    assert description in onboarding_text
    assert served["concept_name"] in str(
        served["search"].get("answer") or ""
    ) or served["search"].get("results")

    # 10. regenerate without source changes: deterministic bytes.
    outcome = generate_beacon(
        reader,
        "fixture",
        repo,
        beacon_python=beacon_python,
        refresh=True,
        expected_sha256=hashlib.sha256(first_bytes).hexdigest(),
    )
    assert manifest.read_bytes() == first_bytes
    assert outcome.sha256 == hashlib.sha256(first_bytes).hexdigest()

    # 11. change one source fact, refresh Menhir, regenerate: only the
    # expected claim changes (the manifest stays line-aligned apart from it).
    changed = _reader(
        root=str(repo),
        overview={
            "description": "A changed evidence dump description.",
            "stack": "python",
            "entities": {"file": 3},
            "edges": {"IMPORTS": 2},
            "coverage": {"known": True},
            "contains_repos": [],
        },
    )
    generate_beacon(
        changed,
        "fixture",
        repo,
        beacon_python=beacon_python,
        refresh=True,
        expected_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    after_lines = manifest.read_text(encoding="utf-8").splitlines()
    before_lines = first_bytes.decode("utf-8").splitlines()
    assert len(before_lines) == len(after_lines)
    changed_indexes = [
        index for index, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b
    ]
    assert changed_indexes
    for index in changed_indexes:
        assert description in before_lines[index]
        assert "A changed evidence dump description." in after_lines[index]

    # 12. Menhir is not running anywhere in this test; the artifact already
    # validated, inspected, and served through Beacon alone.
