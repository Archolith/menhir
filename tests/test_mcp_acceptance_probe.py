from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[1] / "deploy" / "lib" / "mcp_acceptance_probe.py"
SPEC = importlib.util.spec_from_file_location("mcp_acceptance_probe", MODULE)
PROBE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PROBE)


def test_production_policy_requires_exact_read_only_probe() -> None:
    policy = {
        "clients": {
            "menhir-deploy-probe": {
                "label": "menhir-deploy-probe",
                "scopes": ["menhir:read"],
                "maximum_tier": "readonly",
                "namespace": "",
                "allowed_tools": ["recall_memories"],
                "denied_tools": ["add_memory"],
            }
        }
    }
    PROBE._require_probe_policy(policy)
    policy["clients"]["menhir-deploy-probe"]["allowed_tools"] = ["add_memory"]
    with pytest.raises(RuntimeError, match="exact read-only"):
        PROBE._require_probe_policy(policy)


def test_minted_probe_token_is_never_written_or_printed() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert '"exp": now + 60' in source
    assert "token.write" not in source
    assert "write_text(token" not in source
    assert "print(token" not in source


def test_production_mode_is_release_owned() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert "production <base-url> <release-json> <policy-json>" in source
    assert 'docker", "inspect", "menhir-prod-neo4j"' not in source
    assert "production database changed during acceptance" in source
