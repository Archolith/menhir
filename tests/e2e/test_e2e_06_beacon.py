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
The lane therefore needs a second venv with Beacon 0.1.0 installed, pointed at by
``MENHIR_E2E_BEACON_PYTHON``. Without it the Beacon-side criteria cannot run, and the
lane says so rather than quietly asserting less.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tests.e2e._harness.config import E2EConfig
from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending

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

GENERATED_NAME = "beacon.generated.yaml"
HAND_AUTHORED_NAME = "beacon.yaml"


def _beacon_python() -> Path | None:
    raw = os.getenv("MENHIR_E2E_BEACON_PYTHON", "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.exists() else None


async def test_e2e_06_beacon_generation_and_consumption(
    e2e_config: E2EConfig,
    e2e_installed,
    running_stack,
    e2e_fixture_repo,
    feature_combo: FeatureCombo,
    feature_env: dict[str, str],
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(**e2e_fixture_repo.as_evidence())

    beacon_python = _beacon_python()
    if beacon_python is None:
        declare_pending(
            lane_evidence,
            CRITERIA,
            note=(
                "MENHIR_E2E_BEACON_PYTHON is unset or missing. `menhir beacon generate` "
                "requires a separate interpreter with Beacon 0.1.0 installed; without it "
                "no Beacon-side criterion can be proven."
            ),
        )

    # A hand-authored manifest is planted BEFORE generation. The sidecar policy is only
    # meaningful if something exists that a careless implementation would overwrite, and
    # a lane that tests the policy against an empty directory tests nothing.
    hand_authored = Path(e2e_fixture_repo.path) / HAND_AUTHORED_NAME
    hand_authored.write_text(
        "# Hand-authored. The generator must never write to this file.\nname: shop\n",
        encoding="utf-8",
    )
    hand_authored_digest = hashlib.sha256(hand_authored.read_bytes()).hexdigest()
    lane_evidence.record_stack(hand_authored_digest=hand_authored_digest)

    # TODO(#120 evidence): the fixture project must be ingested through the MCP surface
    # before generation -- `menhir beacon generate` reads Menhir-held project knowledge,
    # so generating against an unindexed project proves only that it fails closed.
    # E2E-3's ingest step is the prerequisite; share it rather than duplicating it.
    declare_pending(
        lane_evidence,
        CRITERIA,
        note=(
            "Beacon interpreter present. Remaining work: ingest the fixture through MCP "
            "(shared with E2E-3), then run generate/validate/inspect/serve. Overwrite "
            "policy and CAS refresh are already pinned in this module's docstring."
        ),
    )

    # --- Below is the shape the implementation should take. Kept as reference, not run.
    #
    # result = subprocess.run(
    #     [
    #         str(e2e_installed.venv_script("menhir")), "beacon", "generate", project_name,
    #         "--repo", str(e2e_fixture_repo.path),
    #         "--beacon-python", str(beacon_python),
    #     ],
    #     cwd=str(e2e_config.state_dir),
    #     env=child_environment(e2e_config, **feature_env),
    #     capture_output=True, text=True, timeout=600,
    # )
    # generated = Path(e2e_fixture_repo.path) / GENERATED_NAME
    # lane_evidence.record("generate_beacon_output", passed=generated.is_file())
    # lane_evidence.record(
    #     "sidecar_written_without_touching_hand_authored_manifest",
    #     passed=hashlib.sha256(hand_authored.read_bytes()).hexdigest() == hand_authored_digest,
    # )
    # validate = subprocess.run([str(beacon_python), "-m", "beacon", "validate", str(generated)], ...)
    # lane_evidence.record("beacon_validate_zero_errors", passed=validate.returncode == 0)
