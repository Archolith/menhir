from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "deploy" / "LIVE_VPS_PLAYBOOK.md"
POLICY = ROOT / "deploy" / "client-policy.production.json"
ENV_EXAMPLE = ROOT / "deploy" / "production.env.example"
PRODUCTION = ROOT / "deploy" / "PRODUCTION.md"


def _policy_digest() -> tuple[str, str]:
    payload = json.loads(POLICY.read_text(encoding="utf-8"))
    declared = payload.pop("canonical_digest")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return declared, hashlib.sha256(canonical).hexdigest()


def test_production_env_uses_current_canonical_policy_digest() -> None:
    declared, actual = _policy_digest()
    assert declared == actual

    match = re.search(
        r"^MENHIR_CLIENT_POLICY_DIGEST=([0-9a-f]{64})$",
        ENV_EXAMPLE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None
    assert match.group(1) == declared


def test_playbook_preserves_canonical_repositories_and_network_authority() -> None:
    source = PLAYBOOK.read_text(encoding="utf-8")
    for required in (
        "`Archolith/menhir` | `main`",
        "`Archolith/archolith_oauth` | `main`",
        "`ctharvey/yawn.deploy` | `main`",
        "`ctharvey/yawn.vps` | `master`",
        "`yawn.vps/main` is stale",
        "generic `vps_deploy`/`remote-deploy.sh` path is not authorized",
        "fixed command",
        "172.30.0.1:8000",
        "Caddy at `172.30.0.2` is the only admitted peer",
        "https://memory.ctharvey.me/ops/mcp",
        "issuer `https://memory.ctharvey.me`",
        "`https://memory.ctharvey.me/ops/mcp`",
        "`https://memory.ctharvey.me/ops`",
        "Never publish",
    ):
        assert required in source

    assert "blocked before first bootstrap" not in source
    assert "currently dispatches through the Windows-only" not in source
    production = PRODUCTION.read_text(encoding="utf-8")
    assert "--subnet 172.30.0.0/24" in production
    assert "--gateway 172.30.0.1" in production


def test_playbook_orders_the_release_lifecycle() -> None:
    source = PLAYBOOK.read_text(encoding="utf-8")
    assert "`menhir_release_run()`" in source
    assert "release-run.json" in source
    for stage in (
        "capture legacy writer + backup + retire writer",
        "decrypt and validate backup",
        "restore rehearsal",
        "readonly candidate",
        "candidate acceptance",
        "transactional Caddy route",
        "promotion under a second writer-census check",
        "public production acceptance",
    ):
        assert stage in source


def test_entrypoint_docs_link_to_the_canonical_playbook() -> None:
    expected = "LIVE_VPS_PLAYBOOK.md"
    assert expected in (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert expected in (ROOT / "deploy" / "PRODUCTION.md").read_text(encoding="utf-8")
    assert "../../deploy/LIVE_VPS_PLAYBOOK.md" in (
        ROOT / ".agent" / "workflows" / "operations_runbook.md"
    ).read_text(encoding="utf-8")


def test_playbook_requires_digest_bound_security_review_for_every_release() -> None:
    source = PLAYBOOK.read_text(encoding="utf-8")
    for required in (
        "--review-request",
        "--security-review",
        "different identity from `release_author`",
        "`APPROVED`",
        "zero unresolved critical and high findings",
        "mandatory for every release",
        "cannot be reused",
    ):
        assert required in source
