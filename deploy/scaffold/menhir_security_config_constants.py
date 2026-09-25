"""Path, pattern, and policy constants for the security-config deployment runner."""

from __future__ import annotations

import re
from pathlib import Path

import menhir_app_only as app

Error = app.AppOnlyError
ROOT = app.ROOT
STATUS = app.STATUS
ADMISSION_LOCK = app.ADMISSION_LOCK
UPLOAD_ROOT = Path("/home/thron/.menhir-security-config-upload")
ACTIVE = STATUS / "security-config-active.json"
LAST = STATUS / "security-config-last.json"
BUNDLE_ID = re.compile(r"[a-f0-9]{32}")

TARGETS = {
    "release.json": app.LIVE_RELEASE,
    "production.env": app.LIVE_ENV,
    "client-policy.json": app.LIVE_POLICY,
}
DESTINATIONS = {
    "release.json": "/srv/menhir/production/release/release.json",
    "production.env": "/srv/menhir/production/release/production.env",
    "client-policy.json": "/srv/menhir/production/policy/client-policy.json",
}
MODES = {
    "release.json": 0o400,
    "production.env": 0o400,
    "client-policy.json": 0o644,
}
ALLOWED_ENV_CHANGES = app.ALLOWED_ENV_CHANGES | {"MENHIR_CLIENT_POLICY_DIGEST"}
ALLOWED_RENDERED_CHANGES = {"production_env_sha256", "policy_sha256"}
ALLOWED_CONFIG_DESTINATIONS = set(DESTINATIONS.values()) - {
    "/srv/menhir/production/release/release.json",
}
