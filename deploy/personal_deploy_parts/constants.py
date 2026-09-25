"""Module-level constants for the personal deployment coordinator.

``SCRIPT_DIR`` and ``DEFAULT_PROMOTION_WRAPPER`` anchor to the ``deploy``
directory (this package's parent) exactly as they did when they lived in
``deploy/personal_deploy.py``.
"""

from __future__ import annotations

import re
import shutil
from datetime import timedelta
from pathlib import Path

STATE_NAME = "personal-deploy.json"
STAGING_RECEIPT_NAME = "staging-receipt.json"
APPROVAL_NAME = "promotion-approval.json"
PROMOTION_RECEIPT_NAME = "promotion-receipt.json"
ROOT_TRANSACTION_RECEIPT_NAME = "root-transaction-receipt.json"
RELEASE_STATE_NAME = "release-flow.json"
RELEASE_NAME = "release.json"
BUNDLE_NAME = "install-bundle"
DEFAULT_PROMOTION_WRAPPER = Path(__file__).resolve().parent.parent / "personal_promote.ps1"
SCRIPT_DIR = Path(__file__).resolve().parent.parent
OPERATOR_ROOT = Path.home() / "IdeaProjects" / "scripts"
POWERSHELL = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"

KIND = "menhir-personal-deployment"
SCHEMA = 1
PHASES = ("selected", "staged", "approved", "promoting", "promoted")
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ATTEMPT_RE = re.compile(r"^[0-9a-f]{32}$")
DEPLOYMENT_CLASSES = frozenset({"app-only", "security-config", "maintenance"})
MAX_STAGING_AGE = timedelta(hours=24)

STATE_KEYS = frozenset({
    "schema", "kind", "phase", "workspace", "release_workspace",
    "release_id", "release_sha256", "bundle_sha256", "deployment_class",
    "ingress_mode", "menhir_image", "neo4j_image", "staging_receipt_sha256",
    "approval_sha256", "promotion_receipt_sha256", "promotion_attempt_id",
    "promotion_started_utc", "promotion_wrapper_sha256",
    "operator_wrapper_sha256", "root_runner_sha256",
})
STAGING_CHECKS = frozenset({
    "artifact_identity",
    "production_memory_limits",
    "production_network_shape",
    "oauth_policy_shape",
    "ingress_request_handling",
    "isolated_disposable_data",
    "non_production_credentials",
    "production_authority_absent",
    "oauth_discovery",
    "oauth_authorization_code_pkce",
    "mcp_initialize",
    "mcp_tools_list",
    "mcp_recall",
    "synthetic_write_allowed",
    "denied_operation_refused",
    "restart_persistence",
    "automatic_rollback",
})
STAGING_KEYS = frozenset({
    "schema", "kind", "result", "release_id", "release_sha256",
    "bundle_sha256", "deployment_class", "ingress_mode", "images", "runner_sha256",
    "started_utc", "completed_utc", "test_identities", "checks",
    "production_preflight",
})
PREFLIGHT_KEYS = frozenset({
    "schema", "kind", "result", "observed_utc", "deployment_class",
    "candidate_release_id", "ingress_mode", "checks", "canonical_sha256",
})
PREFLIGHT_CHECK_KEYS = frozenset({
    "live_services", "network_roles", "release_journal", "headroom",
    "maintenance_route",
})
APPROVAL_KEYS = frozenset({
    "schema", "kind", "release_id", "release_sha256", "bundle_sha256",
    "staging_receipt_sha256", "approved_by", "approved_utc",
    "promotion_wrapper_sha256", "operator_wrapper_sha256", "root_runner_sha256",
})
PROMOTION_KEYS = frozenset({
    "schema", "kind", "result", "release_id", "release_sha256",
    "bundle_sha256", "staging_receipt_sha256", "approval_sha256",
    "deployment_class", "ingress_mode", "started_utc", "completed_utc",
    "elapsed_seconds", "promotion_wrapper_sha256", "operator_wrapper_sha256",
    "root_runner_sha256", "promotion_attempt_id", "transaction_kind",
    "transaction_receipt_sha256",
})
