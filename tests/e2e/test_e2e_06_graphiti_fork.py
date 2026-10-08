"""E2E-6 wheel path: the public Graphiti fork works in an installed Menhir.

The release-container half of the approved E2E-6 contract remains a separate,
outstanding release gate. This test makes no image and publishes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from tests.e2e._harness.client import stdio_session
from tests.e2e._harness.config import E2EConfig, child_environment
from tests.e2e._harness.evidence import LaneEvidence, repo_commit, tree_is_clean
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending
from tests.e2e._harness.providers import REFUND_ORIGINAL_AMOUNT
from tests.e2e._harness.stack import InstalledMenhir, REPO_ROOT, graph_query

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(2400),
    pytest.mark.provider("deterministic"),
]

WHEEL_CRITERIA = [
    "public_fork_wheel_hash_matches_lock",
    "installed_fork_matches_public_wheel_without_upstream_collision",
    "native_fork_hooks_wired_from_installed_menhir",
    "fixture_episode_enriches_through_stdio",
    "fixture_episode_recalls_through_stdio",
]
CONTAINER_CRITERIA = [
    "release_container_builds_from_same_locked_dependencies",
    "release_container_exposes_same_fork_version_and_hooks",
]

FORK_VERSION = "0.30.2.post3"
FORK_WHEEL_SHA256 = "89604350e5653e4d55dd1c89de0ce63f51e558a68c8d35601dcde0d583607a7a"
EPISODE_ID = re.compile(r"episode_id:\s*([0-9a-f-]{36})")

# Run by the installed interpreter from the disposable state directory. Compare every
# installed graphiti_core Python file against the independently hash-verified PyPI wheel,
# then construct Menhir's real Graphiti client and inspect its native hook wiring.
_INSTALLED_FORK_PROBE = r"""
import asyncio
import hashlib
import importlib.metadata as md
import inspect
import json
import sys
import zipfile
from pathlib import Path

from graphiti_core import Graphiti
from menhir.config.settings_model import MemorySettings
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_resolution_policy import menhir_resolution_hooks_installed

dist = md.distribution("archolith-graphiti-core")
try:
    md.distribution("graphiti-core")
except md.PackageNotFoundError:
    upstream_absent = True
else:
    upstream_absent = False

with zipfile.ZipFile(sys.argv[1]) as wheel:
    source_files = [
        name for name in wheel.namelist()
        if name.startswith("graphiti_core/") and name.endswith(".py")
    ]
    mismatches = []
    for name in source_files:
        installed = Path(dist.locate_file(name)).resolve()
        if not installed.is_file() or hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(wheel.read(name)).digest():
            mismatches.append(name)

async def probe_hooks():
    client = GraphitiClient.from_settings(MemorySettings.from_env())
    try:
        return menhir_resolution_hooks_installed(client.client)
    finally:
        await client.close()

hook_parameters = {
    "single_episode_extraction_hook", "identity_gate_hook",
    "candidate_filter_hook", "node_pre_resolution_hook",
}
print(json.dumps({
    "fork_version": dist.version,
    "upstream_absent": upstream_absent,
    "source_files": len(source_files),
    "mismatches": mismatches,
    "native_hook_parameters": hook_parameters <= set(inspect.signature(Graphiti).parameters),
    "menhir_hooks_wired": asyncio.run(probe_hooks()),
    "installed_root": str(Path(dist.locate_file("graphiti_core/__init__.py")).resolve()),
}))
"""


def _text(result: object) -> str:
    return "\n".join(
        item.text
        for item in (getattr(result, "content", None) or [])
        if getattr(item, "text", None)
    )


def _verified_public_wheel(config: E2EConfig) -> Path:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["name"] == "archolith-graphiti-core")
    assert package["version"] == FORK_VERSION
    assert package["source"] == {"registry": "https://pypi.org/simple"}
    wheels = package["wheels"]
    assert len(wheels) == 1, "the fork contract must identify one public wheel"
    wheel = wheels[0]
    assert wheel["hash"] == f"sha256:{FORK_WHEEL_SHA256}"
    url = wheel["url"]
    assert urlsplit(url).hostname == "files.pythonhosted.org"
    assert (
        Path(urlsplit(url).path).name
        == f"archolith_graphiti_core-{FORK_VERSION}-py3-none-any.whl"
    )

    with urllib.request.urlopen(url, timeout=120) as response:
        content = response.read()
    actual = hashlib.sha256(content).hexdigest()
    assert actual == FORK_WHEEL_SHA256, f"public fork wheel hash mismatch: {actual}"
    path = config.work_root / "fork-wheel" / Path(urlsplit(url).path).name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


async def test_e2e_06_packaged_graphiti_fork(
    e2e_config: E2EConfig,
    e2e_installed: InstalledMenhir,
    running_stack,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    provider_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    assert running_stack.ready_payload, "the installed backend did not become ready"
    lane_evidence.record_stack(provider="deterministic", features=feature_combo.label)
    wheel = _verified_public_wheel(e2e_config)
    lane_evidence.record(
        "public_fork_wheel_hash_matches_lock",
        passed=True,
        detail={"wheel": wheel.name, "sha256": FORK_WHEEL_SHA256},
    )

    probe = subprocess.run(
        [str(e2e_installed.venv_python), "-c", _INSTALLED_FORK_PROBE, str(wheel)],
        cwd=e2e_config.state_dir,
        env=child_environment(e2e_config, **feature_env, **provider_env),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, f"installed fork probe failed: {probe.stderr[-1500:]}"
    installed = json.loads(probe.stdout.strip().splitlines()[-1])
    package_ok = (
        installed["fork_version"] == FORK_VERSION
        and installed["upstream_absent"]
        and installed["source_files"] > 0
        and not installed["mismatches"]
        and "site-packages" in Path(installed["installed_root"]).parts
    )
    lane_evidence.record(
        "installed_fork_matches_public_wheel_without_upstream_collision",
        passed=package_ok,
        detail=installed,
    )
    assert package_ok, installed
    hooks_wired = (
        installed["native_hook_parameters"] and installed["menhir_hooks_wired"]
    )
    lane_evidence.record(
        "native_fork_hooks_wired_from_installed_menhir", passed=hooks_wired
    )
    assert hooks_wired, installed

    namespace = f"e2e6-fork-{uuid4().hex}"
    original = (
        "The Atlas Lantern service uses Stripe. Its refund approval threshold is "
        f"{REFUND_ORIGINAL_AMOUNT} dollars."
    )
    child_env = {**feature_env, **provider_env}
    async with stdio_session(
        e2e_config, e2e_installed.venv_python, lane_evidence, feature_env=child_env
    ) as client:
        accepted = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "add_memory_and_track",
                    "arguments": {"text": original, "namespace": namespace},
                },
            )
        )
        match = EPISODE_ID.search(accepted)
        assert match, f"no episode receipt from installed Menhir: {accepted[:500]}"
        episode_id = match.group(1)
        observed = _text(
            await client.call_tool(
                "call_tool",
                {
                    "name": "get_enrichment_status",
                    "arguments": {
                        "episode_uuid": episode_id,
                        "wait": True,
                        "timeout_s": 180.0,
                        "namespace": namespace,
                    },
                },
            )
        )
        linked = graph_query(
            e2e_config,
            "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
            "WHERE e.group_id = $group RETURN count(DISTINCT n) AS count",
            group=namespace,
        )
        linked_count = int(linked[0]["count"]) if linked else 0
        enriched = "status: READY" in observed and linked_count > 0
        lane_evidence.record(
            "fixture_episode_enriches_through_stdio",
            passed=enriched,
            detail={
                "episode_id": episode_id,
                "status": observed[:500],
                "linked_entities": linked_count,
            },
        )
        assert enriched, (
            f"fork-backed episode did not enrich: {observed[:500]} linked={linked_count}"
        )

        recalled = _text(
            await client.call_tool(
                "recall_memories",
                {
                    "query": "Atlas Lantern refund approval threshold",
                    "namespace": namespace,
                },
            )
        )
        recall_ok = "Atlas Lantern" in recalled and REFUND_ORIGINAL_AMOUNT in recalled
        lane_evidence.record(
            "fixture_episode_recalls_through_stdio",
            passed=recall_ok,
            detail=recalled[:500],
        )
        assert recall_ok, recalled[:700]

    assert set(lane_evidence.criteria) == set(WHEEL_CRITERIA)
    lane_evidence.close(status="PASS")


def test_e2e_06_release_container(lane_evidence: LaneEvidence) -> None:
    """Verify an exact-commit no-publish image bundle and run its installed fork."""
    raw = os.getenv("MENHIR_E2E_RELEASE_IMAGE_BUNDLE", "").strip()
    if not raw:
        declare_pending(
            lane_evidence,
            CONTAINER_CRITERIA,
            note="Set MENHIR_E2E_RELEASE_IMAGE_BUNDLE to an exact-commit no-publish validation artifact.",
        )
    bundle = Path(raw).resolve(strict=True)
    assert bundle.is_dir(), bundle
    clean, dirty_paths = tree_is_clean(REPO_ROOT)
    assert clean, f"container evidence requires a clean tree: {dirty_paths[:20]}"
    commit = repo_commit(REPO_ROOT)
    identity_path = bundle / "release-image-identity.json"
    metadata_path = bundle / "release-image-metadata.json"
    archive_path = bundle / "release-image.tar"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert identity["source_commit"] == metadata["source_commit"] == commit
    assert identity["source_repository"] == metadata["source_repository"] == "Archolith/menhir"
    identity_sha256 = hashlib.sha256(identity_path.read_bytes()).hexdigest()

    verify = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "deploy" / "build_release_image.py"),
            "--mode", "verify", "--version", metadata["labels"]["version"],
            "--image", metadata["image_tag"].rsplit(":", 1)[0],
            "--python-base", metadata["python_base"],
            "--repo", str(REPO_ROOT),
            "--source-repository", "Archolith/menhir",
            "--metadata", str(metadata_path), "--identity", str(identity_path),
            "--image-archive", str(archive_path),
            "--expected-identity-sha256", identity_sha256,
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    lane_evidence.attach("container-verify.txt", verify.stdout + "\n" + verify.stderr)
    lane_evidence.record(
        CONTAINER_CRITERIA[0], passed=verify.returncode == 0,
        detail={"source_commit": commit, "identity_sha256": identity_sha256,
                "image_id": metadata["image_id"], "verify_exit": verify.returncode},
    )
    assert verify.returncode == 0, (verify.stdout + verify.stderr)[-1500:]

    probe = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
         "--env", "MENHIR_STARTUP_SCOPE=full", "--env", "MENHIR_API_HOST=127.0.0.1",
         "--entrypoint", "python", metadata["image_tag"], "-c", "\n".join([
             "import asyncio, importlib.metadata as md, inspect, json",
             "from graphiti_core import Graphiti",
             "from menhir.config.settings_model import MemorySettings",
             "from menhir.infrastructure.graphiti_client import GraphitiClient",
             "from menhir.infrastructure.graphiti_resolution_policy import menhir_resolution_hooks_installed",
             "fork = md.distribution('archolith-graphiti-core')",
             "try: md.distribution('graphiti-core'); upstream_absent = False",
             "except md.PackageNotFoundError: upstream_absent = True",
             "async def wired():",
             "    client = GraphitiClient.from_settings(MemorySettings.from_env())",
             "    try: return menhir_resolution_hooks_installed(client.client)",
             "    finally: await client.close()",
             "parameters = {'single_episode_extraction_hook', 'identity_gate_hook',",
             "              'candidate_filter_hook', 'node_pre_resolution_hook'}",
             "print(json.dumps({'fork_version': fork.version, 'upstream_absent': upstream_absent,",
             "    'native_hook_parameters': parameters <= set(inspect.signature(Graphiti).parameters),",
             "    'menhir_hooks_wired': asyncio.run(wired())}))",
         ])],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
    )
    lane_evidence.attach("container-fork-probe.txt", probe.stdout + "\n" + probe.stderr)
    assert probe.returncode == 0, (probe.stdout + probe.stderr)[-1500:]
    actual = json.loads(probe.stdout.strip().splitlines()[-1])
    expected = {"fork_version": FORK_VERSION, "upstream_absent": True,
                "native_hook_parameters": True, "menhir_hooks_wired": True}
    lane_evidence.record(
        CONTAINER_CRITERIA[1], passed=actual == expected,
        detail={"image_id": metadata["image_id"], **actual},
    )
    assert actual == expected, actual
    lane_evidence.record_stack(source_commit=commit, image_id=metadata["image_id"])
    lane_evidence.close(status="PASS")
