"""E2E-6 — Beacon generation and consumption.

Implemented ahead of lanes 2-5/7-8 because #120's generator has already landed on main
(``23f22c8b``: ``cli/beacon.py``, ``services/beacon_generation.py``, ``beacon_compat.py``,
``beacon_publication.py``). Issue #120's body still says "no shipped generator
implementation" -- it was written hours before the merge. What is actually outstanding
for #120 is this lane's evidence, not the feature.

THE OVERWRITE POLICY, AS IMPLEMENTED
------------------------------------
Gate A lists "finalize #120's command/tool name and safe overwrite/refresh policy" as an
open decision. The code already answers it, and this lane pins that answer so it cannot
drift before the freeze:

  - Command: ``menhir beacon generate <project> --repo <root> --beacon-python <python>``.
    A CLI operator command, deliberately NOT an MCP tool -- it writes a repository file
    and shells out to a separate interpreter, so it stays off the agent-facing surface.
  - Output: ``beacon.generated.yaml``, a **sidecar**. A hand-authored ``beacon.yaml`` is
    never written to, which is how "do not silently overwrite a hand-authored manifest"
    is satisfied structurally rather than by a check that could be forgotten.
  - Refresh: ``--refresh --expected-sha256 <digest>``, i.e. compare-and-set against the
    existing generated file. A refresh with a stale digest must be refused;
    ``beacon_publication.py:128`` raises ``refusing to overwrite existing output``.

BEACON IS A SEPARATE INTERPRETER
--------------------------------
``--beacon-python`` exists so Beacon's dependencies stay out of Menhir's environment.
The lane therefore needs a second venv, pointed at by ``MENHIR_E2E_BEACON_PYTHON``, whose
Beacon satisfies the **build contract** (``beacon build --menhir-evidence`` and
``beacon validate``), not any particular product version: since the 2026-09-18 ownership
switch Menhir dumps an evidence document and Beacon's own pipeline builds the manifest.
Install the exact revision CI pins (``.github/workflows/tests.yml``, the same one the
online E2E-6 in ``tests/test_beacon_e2e6.py`` runs against)::

    pip install "git+https://github.com/Archolith/beacon.git@1cc3352b90004f3b76f1c5ed49ee4235c606a52f"

The PyPI ``archolith-beacon 0.1.0`` does NOT satisfy the contract (no ``build``), and the
compat gate refuses it. Without a usable interpreter the Beacon-side criteria cannot run,
and the lane says so rather than quietly asserting less.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.e2e._harness.client import stdio_session, wait_for_project_indexed
from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.fixture_repo import build_fixture_repo
from tests.e2e._harness.pending import declare_pending
from tests.e2e._harness.stack import run_menhir_cli

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

CRITERIA = [
    "generate_beacon_output",
    "sidecar_written_without_touching_hand_authored_manifest",
    "beacon_validate_zero_errors",
    "beacon_inspect_normalized_snapshot",
    "beacon_stdio_server_starts_from_generated_manifest",
    "beacon_project_overview_returns_identity",
    "beacon_agent_onboarding_returns_grounded_guidance",
    "generated_concept_or_guardrail_is_queryable",
    "claims_traceable_to_source_material",
    "regenerate_unchanged_controlled_diff",
    "one_source_fact_changed_changes_only_expected_portion",
    "stale_digest_refresh_is_refused",
    "invalid_source_state_does_not_clobber_existing_manifest",
]

HAND_AUTHORED_NAME = "beacon.yaml"
SIDECAR_NAME = "beacon.generated.yaml"

#: The one grounded sentence the generator may publish as the project's description.
#: Two product rules decide the fixture shape. The scanner classifies ONLY
#: `.agent/{README,architecture,data_models,endpoints,CHANGELOG}.md` as documents (Beacon
#: v2 design, A0: bounded agent-orientation docs) -- a root README.md is a plain file, and
#: Beacon refuses to publish a manifest with no canonical document. And since #125 the
#: generator REFUSES an ungrounded description rather than inventing one; it reads
#: `.agent/README.md` first. The shared "shop" fixture has no `.agent/`, so this lane
#: builds its own copy and plants that one file.
ORIENTATION = "The shop service answers order lookups through one HTTP endpoint."
ORIENTATION_CHANGED = "The shop service answers order lookups and now also cancels orders."

#: Fields a description change is allowed to move. Anything else moving on refresh means
#: the "one fact changed" criterion failed: unrelated knowledge was regenerated.
DESCRIPTION_FIELDS = {
    ("project", "description"),
    ("project", "tagline"),
    ("purpose", "one_sentence"),
}

FILE_COUNT = re.compile(r"^\s*file:\s*(\d+)\s*$", re.M)
SHA_LINE = re.compile(r"^sha256:\s*([0-9a-f]{64})\s*$", re.M)
FINGERPRINT_LINE = re.compile(r"^scan_fingerprint:\s*(\S+)\s*$", re.M)


def _beacon_python() -> Path | None:
    raw = os.getenv("MENHIR_E2E_BEACON_PYTHON", "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.exists() else None


def _text(result: object) -> str:
    content = getattr(result, "content", None) or []
    parts = [getattr(item, "text", "") for item in content]
    return "\n".join(part for part in parts if part)


def _payload(result: object) -> dict:
    """Beacon's tools answer JSON in a text block; fall back to the raw text."""
    text = _text(result)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


def _run_beacon(python: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv, harness-owned interpreter and fixture
        [str(python), "-m", "beacon", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _generate(config: E2EConfig, project: str, repo: Path, python: Path, env: dict[str, str], *extra: str):
    return run_menhir_cli(
        config,
        "beacon",
        "generate",
        project,
        "--repo",
        str(repo),
        "--beacon-python",
        str(python),
        *extra,
        feature_env=env,
    )


def _flatten(node: object, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    if isinstance(node, dict):
        out: dict[tuple[str, ...], object] = {}
        for key, value in node.items():
            out.update(_flatten(value, (*prefix, str(key))))
        return out
    return {prefix: node}


def _changed_paths(before: bytes, after: bytes) -> set[tuple[str, ...]]:
    a = _flatten(yaml.safe_load(before.decode("utf-8")))
    b = _flatten(yaml.safe_load(after.decode("utf-8")))
    return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}


async def test_e2e_06_beacon_generation_and_consumption(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    beacon_python = _beacon_python()
    if beacon_python is None:
        declare_pending(
            lane_evidence,
            CRITERIA,
            note=(
                "MENHIR_E2E_BEACON_PYTHON is unset or missing. `menhir beacon generate` "
                "requires a separate interpreter whose Beacon supports build+validate "
                "(the SHA pinned in .github/workflows/tests.yml); without it no "
                "Beacon-side criterion can be proven."
            ),
        )

    # Own fixture copy, with the grounded description the generator requires.
    fixture = build_fixture_repo(e2e_config.fixtures_dir / "shop-beacon")
    repo = Path(fixture.path)
    (repo / ".agent").mkdir(exist_ok=True)
    (repo / ".agent" / "README.md").write_text(f"# shop\n\n{ORIENTATION}\n", encoding="utf-8")
    lane_evidence.record_stack(**fixture.as_evidence(), beacon_python=str(beacon_python))

    # A hand-authored manifest is planted BEFORE generation. The sidecar policy is only
    # meaningful if something exists that a careless implementation would overwrite.
    hand_authored = repo / HAND_AUTHORED_NAME
    hand_authored.write_text(
        "# Hand-authored. The generator must never write to this file.\nname: shop\n",
        encoding="utf-8",
    )
    hand_authored_digest = hashlib.sha256(hand_authored.read_bytes()).hexdigest()
    lane_evidence.record_stack(hand_authored_digest=hand_authored_digest)

    suffix = uuid4().hex[:8]
    project = f"shop-beacon-{suffix}"
    namespace = f"e2e6-{suffix}"
    child_env = {**feature_env, "MENHIR_NAMESPACE": namespace}
    sidecar = repo / SIDECAR_NAME

    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        # --- ingest through the MCP surface (E2E-3's step, not duplicated in-process) --
        ingest = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "ingest_project",
                    "arguments": {
                        "path": str(repo),
                        "name": project,
                        "namespace": namespace,
                        "identity_action": "new",
                    },
                },
            )
        )
        assert ingest.startswith(f"Scanned {project}"), ingest[:600]
        await wait_for_project_indexed(client, project, symbol_path="src/shop/api.py")
        overview = _text(
            await client.call_tool(
                "query_structure", {"query_type": "overview", "project": project}
            )
        )
        lane_evidence.attach("overview.txt", overview)
        count_match = FILE_COUNT.search(overview)
        assert count_match, f"overview reports no file count:\n{overview[:600]}"
        indexed_files = int(count_match.group(1))

        # --- 1. generate ---------------------------------------------------------------
        generated = _generate(e2e_config, project, repo, beacon_python, child_env)
        lane_evidence.attach("generate.txt", generated.stdout + "\n--- stderr ---\n" + generated.stderr)
        created = generated.returncode == 0 and "created " in generated.stdout and sidecar.is_file()
        lane_evidence.record(
            "generate_beacon_output",
            passed=created,
            detail={"exit": generated.returncode, "stdout": generated.stdout[:400], "stderr": generated.stderr[:400]},
        )
        assert created, f"generate failed (exit {generated.returncode}):\n{generated.stderr[:800]}"
        first_bytes = sidecar.read_bytes()
        sha_match = SHA_LINE.search(generated.stdout)
        fp_match = FINGERPRINT_LINE.search(generated.stdout)
        assert sha_match and fp_match, generated.stdout
        first_sha = sha_match.group(1)
        fingerprint = fp_match.group(1)
        assert first_sha == hashlib.sha256(first_bytes).hexdigest(), "CLI digest != file digest"

        # --- 2. sidecar policy ----------------------------------------------------------
        untouched = hashlib.sha256(hand_authored.read_bytes()).hexdigest() == hand_authored_digest
        lane_evidence.record(
            "sidecar_written_without_touching_hand_authored_manifest",
            passed=untouched and sidecar.name == SIDECAR_NAME,
            detail={"hand_authored": HAND_AUTHORED_NAME, "sidecar": SIDECAR_NAME},
        )
        assert untouched, "the generator wrote to the hand-authored beacon.yaml"

        # --- 3/4. Beacon's own tooling accepts the artifact ------------------------------
        validated = _run_beacon(beacon_python, ["validate", str(sidecar)], repo)
        lane_evidence.attach("validate.txt", validated.stdout + validated.stderr)
        zero_errors = validated.returncode == 0 and "0 errors" in validated.stdout
        lane_evidence.record(
            "beacon_validate_zero_errors",
            passed=zero_errors,
            detail={"exit": validated.returncode, "stdout": validated.stdout[:300]},
        )
        assert zero_errors, validated.stdout[:800] + validated.stderr[:400]

        inspected = _run_beacon(beacon_python, ["inspect", str(sidecar)], repo)
        lane_evidence.attach("inspect.txt", inspected.stdout + inspected.stderr)
        inspect_ok = inspected.returncode == 0 and "5 tools responded" in inspected.stdout
        lane_evidence.record(
            "beacon_inspect_normalized_snapshot",
            passed=inspect_ok,
            detail={"exit": inspected.returncode, "stdout": inspected.stdout[:300]},
        )
        assert inspect_ok, inspected.stdout[:800] + inspected.stderr[:400]

        # --- 5-9. Beacon's own stdio server, driven with the stock MCP SDK ---------------
        served = await _drive_beacon_server(beacon_python, sidecar, repo, lane_evidence)
        tools = served["tools"]
        server_up = {"beacon_project_overview", "beacon_agent_onboarding", "beacon_explain_concept"} <= set(tools)
        lane_evidence.record(
            "beacon_stdio_server_starts_from_generated_manifest",
            passed=server_up,
            detail={"tools": sorted(tools)},
        )
        assert server_up, tools

        overview_payload = served["overview"]
        overview_text = json.dumps(overview_payload)
        identity_ok = project in overview_text and ORIENTATION in overview_text
        lane_evidence.record(
            "beacon_project_overview_returns_identity",
            passed=identity_ok,
            detail={"project": project, "overview": overview_text[:500]},
        )
        assert identity_ok, overview_text[:800]

        onboarding_text = json.dumps(served["onboarding"])
        onboarding_ok = ORIENTATION in onboarding_text
        lane_evidence.record(
            "beacon_agent_onboarding_returns_grounded_guidance",
            passed=onboarding_ok,
            detail={"onboarding": onboarding_text[:500]},
        )
        assert onboarding_ok, onboarding_text[:800]

        concept_text = json.dumps(served["concept"])
        concept_ok = bool(served["concept_name"]) and len(concept_text) > 20 and "error" not in served["concept"]
        lane_evidence.record(
            "generated_concept_or_guardrail_is_queryable",
            passed=concept_ok,
            detail={"concept": served["concept_name"], "payload": concept_text[:400]},
        )
        assert concept_ok, concept_text[:800]

        # The concept is projected from Menhir's evidence: it must cite the structure it
        # came from, with the same file count MCP reported, and the manifest must carry
        # the scan fingerprint the CLI printed. Numbers, not adjectives.
        cites_structure = "Indexed repository structure" in concept_text
        cites_count = f"file: {indexed_files}" in concept_text
        carries_fingerprint = fingerprint.encode("utf-8") in first_bytes
        lane_evidence.record(
            "claims_traceable_to_source_material",
            passed=cites_structure and cites_count and carries_fingerprint,
            detail={
                "indexed_files": indexed_files,
                "cites_structure": cites_structure,
                "cites_count": cites_count,
                "manifest_carries_fingerprint": carries_fingerprint,
                "fingerprint": fingerprint,
            },
        )
        assert cites_structure and cites_count, concept_text[:800]
        assert carries_fingerprint, "sidecar does not cite the scan fingerprint"

        # --- 10. unchanged inputs -> identical bytes -----------------------------------
        refreshed = _generate(
            e2e_config, project, repo, beacon_python, child_env, "--refresh", "--expected-sha256", first_sha
        )
        lane_evidence.attach("refresh-unchanged.txt", refreshed.stdout + refreshed.stderr)
        identical = refreshed.returncode == 0 and sidecar.read_bytes() == first_bytes
        lane_evidence.record(
            "regenerate_unchanged_controlled_diff",
            passed=identical,
            detail={"exit": refreshed.returncode, "byte_identical": sidecar.read_bytes() == first_bytes},
        )
        assert identical, refreshed.stdout[:400] + refreshed.stderr[:400]

        # --- 12. stale digest -> refused, bytes untouched ------------------------------
        stale = _generate(
            e2e_config, project, repo, beacon_python, child_env, "--refresh", "--expected-sha256", "0" * 64
        )
        lane_evidence.attach("refresh-stale.txt", stale.stdout + stale.stderr)
        stale_refused = stale.returncode != 0 and "refused" in stale.stderr and sidecar.read_bytes() == first_bytes
        lane_evidence.record(
            "stale_digest_refresh_is_refused",
            passed=stale_refused,
            detail={"exit": stale.returncode, "stderr": stale.stderr[:300]},
        )
        assert stale_refused, f"stale refresh exit={stale.returncode}: {stale.stderr[:400]}"

        # --- 13. checkout drifted from the graph -> refused, bytes untouched -----------
        drifted = repo / "src" / "shop" / "storage.py"
        drifted_bytes = drifted.read_bytes()
        drifted.unlink()
        try:
            drift = _generate(
                e2e_config, project, repo, beacon_python, child_env, "--refresh", "--expected-sha256", first_sha
            )
        finally:
            drifted.write_bytes(drifted_bytes)
        lane_evidence.attach("refresh-drifted.txt", drift.stdout + drift.stderr)
        drift_refused = drift.returncode != 0 and "refused" in drift.stderr and sidecar.read_bytes() == first_bytes
        lane_evidence.record(
            "invalid_source_state_does_not_clobber_existing_manifest",
            passed=drift_refused,
            detail={"removed": "src/shop/storage.py", "exit": drift.returncode, "stderr": drift.stderr[:300]},
        )
        assert drift_refused, f"drifted refresh exit={drift.returncode}: {drift.stderr[:400]}"

        # --- 11. one grounded fact changes -> only the expected fields move ------------
        # (edit the orientation doc; the fixture's git HEAD is untouched so the only
        # legitimate movers are the description fields and the cited scan fingerprint)
        (repo / ".agent" / "README.md").write_text(f"# shop\n\n{ORIENTATION_CHANGED}\n", encoding="utf-8")
        rescan = _text(
            await client.call_tool(
                "call_tool",
                {"name": "ingest_project", "arguments": {"path": str(repo), "name": project, "namespace": namespace}},
            )
        )
        assert rescan.startswith(f"Scanned {project}"), rescan[:600]
        await wait_for_project_indexed(client, project, symbol_path="src/shop/api.py")
        changed = _generate(
            e2e_config, project, repo, beacon_python, child_env, "--refresh", "--expected-sha256", first_sha
        )
        lane_evidence.attach("refresh-changed.txt", changed.stdout + changed.stderr)
        assert changed.returncode == 0, changed.stderr[:600]
        after_bytes = sidecar.read_bytes()
        # A rescan always mints a new scan fingerprint, and the structure concept cites it
        # ("Menhir structure scan (<fingerprint>)"). That citation moving is provenance
        # doing its job, not unrelated knowledge changing -- so both fingerprints are
        # neutralised before the diff, and anything ELSE in core_concepts moving still fails.
        new_fp = FINGERPRINT_LINE.search(changed.stdout)
        assert new_fp, changed.stdout
        placeholder = b"<scan-fingerprint>"
        moved = _changed_paths(
            first_bytes.replace(fingerprint.encode("utf-8"), placeholder),
            after_bytes.replace(new_fp.group(1).encode("utf-8"), placeholder),
        )
        only_expected = bool(moved) and moved <= DESCRIPTION_FIELDS
        new_orientation_published = ORIENTATION_CHANGED.encode("utf-8") in after_bytes
        lane_evidence.record(
            "one_source_fact_changed_changes_only_expected_portion",
            passed=only_expected and new_orientation_published,
            detail={"changed_paths": sorted(".".join(p) for p in moved), "allowed": sorted(".".join(p) for p in DESCRIPTION_FIELDS)},
        )
        assert new_orientation_published, "refresh did not publish the changed description"
        assert only_expected, f"unexpected fields moved: {sorted('.'.join(p) for p in moved)}"

    lane_evidence.close(status="PASS")


async def _drive_beacon_server(python: Path, manifest: Path, repo: Path, evidence: LaneEvidence) -> dict:
    """Talk to Beacon's own MCP server over stdio with the stock SDK -- the same client
    shape an agent host uses, not Beacon's in-process test client."""
    parameters = StdioServerParameters(
        command=str(python), args=["-m", "beacon", "serve", "-m", str(manifest)], cwd=str(repo)
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            overview = _payload(await session.call_tool("beacon_project_overview", {}))
            onboarding = _payload(
                await session.call_tool("beacon_agent_onboarding", {"task_hint": "architecture"})
            )
            components = overview.get("core_components") or []
            concept_name = components[0].split(" (")[0] if components else "Project structure"
            concept = _payload(await session.call_tool("beacon_explain_concept", {"concept": concept_name}))
    served = {
        "tools": tools,
        "overview": overview,
        "onboarding": onboarding,
        "concept": concept,
        "concept_name": concept_name,
    }
    evidence.attach("beacon-server.json", json.dumps(served, indent=2, default=str))
    return served
