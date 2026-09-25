"""Path, pattern, and schema constants for the app-only deployment runner."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

ROOT = Path("/srv/menhir/production")
STATUS = Path("/var/lib/menhir-production")
UPLOAD_ROOT = Path("/home/thron/.menhir-app-only-upload")
COMPOSE = ROOT / "deploy/docker-compose.production.yml"
LIVE_RELEASE = ROOT / "release/release.json"
LIVE_ENV = ROOT / "release/production.env"
LIVE_POLICY = ROOT / "policy/client-policy.json"
SCHEMA = ROOT / "bin/menhir_schema.py"
SCAFFOLD = Path("/srv/menhir/scaffold/bin/menhir_scaffold.py")
ADMISSION_LOCK = Path("/run/lock/menhir-production-admission.lock")
MUTATION_LOCK = Path("/run/lock/menhir-production.lock")
ACTIVE = STATUS / "app-only-active.json"
LAST = STATUS / "app-only-last.json"
SECURITY_CONFIG_ACTIVE = STATUS / "security-config-active.json"
MAINTENANCE_ACTIVE = STATUS / "release-run.json"
PROBE_CLIENT_ID = "menhir-deploy-probe"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}")
BUNDLE_ID = re.compile(r"[a-f0-9]{32}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
ALLOWED_ENV_CHANGES = {"MENHIR_IMAGE", "MENHIR_RELEASE_COMMIT", "MENHIR_RELEASE_ID"}
APP_ONLY_SOURCE_PATTERNS = tuple(re.compile(value) for value in (
    r"^src/menhir/explorer/static/[^/]+$",
    r"^src/menhir/explorer/templates/[^/]+$",
))
ALLOWED_RELEASE_SCALARS = (
    ("release_id",),
    ("repos", "menhir"),
    ("images", "menhir"),
    ("provenance_sha256",),
    ("sbom_sha256",),
    ("scan_evidence_sha256",),
    ("wheel_manifest_sha256",),
    ("dockerfile_wheel_manifest_sha256",),
    ("rendered", "production_env_sha256"),
)
STAGING_RECEIPT_NAME = "staging-receipt.json"
APPROVAL_NAME = "promotion-approval.json"
STAGING_RECEIPT_MAX_AGE = dt.timedelta(hours=24)
STAGING_KEYS = {
    "schema", "kind", "result", "release_id", "release_sha256",
    "bundle_sha256", "deployment_class", "ingress_mode", "images",
    "runner_sha256", "started_utc", "completed_utc", "test_identities",
    "checks", "production_preflight",
}
APPROVAL_KEYS = {
    "schema", "kind", "release_id", "release_sha256", "bundle_sha256",
    "staging_receipt_sha256", "approved_by", "approved_utc",
    "promotion_wrapper_sha256", "operator_wrapper_sha256", "root_runner_sha256",
}
PREFLIGHT_KEYS = {
    "schema", "kind", "result", "observed_utc", "deployment_class",
    "candidate_release_id", "ingress_mode", "checks", "canonical_sha256",
}
STAGING_CHECKS = {
    "artifact_identity", "production_memory_limits", "production_network_shape",
    "oauth_policy_shape", "ingress_request_handling", "isolated_disposable_data",
    "non_production_credentials", "production_authority_absent", "oauth_discovery",
    "oauth_authorization_code_pkce", "mcp_initialize", "mcp_tools_list", "mcp_recall",
    "synthetic_write_allowed", "denied_operation_refused", "restart_persistence",
    "automatic_rollback",
}
