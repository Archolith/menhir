"""Fixed paths, patterns, and schemas bound by the Menhir host scaffold."""

from __future__ import annotations

import re
from pathlib import Path

CONTRACT_PATH = Path("/etc/menhir/scaffold-contract.json")
RECEIPT_PATH = Path("/var/lib/menhir-production/scaffold-receipt.json")
STATUS_ROOT = Path("/var/lib/menhir-production")
RELEASE_PATH = Path("/srv/menhir/production/release/release.json")
SCHEMA_PATH = Path("/srv/menhir/production/bin/menhir_schema.py")
ENCRYPTED_BACKUP_ROOT = Path("/srv/menhir/backups/encrypted")
BACKUP_RECEIPT = STATUS_ROOT / "backup-local-receipt.json"
DESKTOP_RECEIPT = STATUS_ROOT / "desktop-archive-receipt.json"
DRILL_RECEIPT = STATUS_ROOT / "scaffold-restore-drill-receipt.json"
REHEARSAL_RECEIPT = STATUS_ROOT / "rehearsal-receipt.json"
RELEASE_RUN = STATUS_ROOT / "release-run.json"
FIRST_MUTATION = STATUS_ROOT / "first-mutation"
ADMISSION_LOCK = Path("/run/lock/menhir-production-admission.lock")
ADMISSION_READY = Path("/run/menhir-maintenance-admission.ready")
ADMISSION_UNIT = "menhir-maintenance-admission.service"
MAINTENANCE_HISTORY = STATUS_ROOT / "maintenance-history"
VERIFY_ARTIFACTS = Path("/srv/menhir/production/bin/verify-artifacts")
HEX64 = re.compile(r"[0-9a-f]{64}")
GENERATION = re.compile(r"generation\.[A-Za-z0-9]+")
SAFE_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,255}")
RELEASE_ID = re.compile(r"menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+")
ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
CONTRACT_KEYS = {
    "schema", "kind", "host", "directories", "files", "identities",
    "groups", "network", "units", "backup_policy", "runtime",
}
RECEIPT_KEYS = {
    "schema", "kind", "contract_sha256", "verifier_sha256",
    "machine_id_sha256", "captured_utc", "static",
}
SCAFFOLD_DRILL_KEYS = {
    "schema", "kind", "generation", "backup_receipt_sha256",
    "checked_utc", "recorded_utc", "method",
}
# A restore drill is proof that a backup was actually loaded and checked. The
# retired "backup-generation-..." method proved nothing of the kind: it restamped
# the backup receipt's own checked_utc, and the audit admits any method in this
# set on generation and age alone, so it could satisfy the restore-drill
# requirement with no restore. Only a real rehearsal counts.
SCAFFOLD_DRILL_METHODS = {
    "release-rehearsal-clean-load-and-consistency-check",
}
MAINTENANCE_STAGES = {
    "start", "backup", "staged", "rehearsal", "candidate", "accepted",
    "routed", "promoted", "complete",
}
MAINTENANCE_STATE_KEYS = {
    "schema", "kind", "release_id", "release_manifest_sha256", "stage",
    "generation", "started_utc", "updated_utc", "completed_utc",
    "runner_sha256", "approval_sha256", "promotion_attempt_id", "approved_utc",
    "promotion_started_utc",
}

# Every marker that belongs to one maintenance transaction. Written during the
# cycle, and archived with its journal when the cycle ends -- by completion or
# by abandonment. Before this list existed, `abandon` archived some of these and
# `complete` archived none, so every finished cycle left markers behind for the
# next one to trip over. `first-mutation` in particular had no clearer at all,
# which made abandon impossible on any host that had ever completed a promote.
MAINTENANCE_MARKERS = (
    "candidate-generation", "candidate-prestart-authority.json",
    "candidate-accept-receipt.json", "candidate-accepted", "restore-selection",
    "same-host-writer-fence-intent.json", "same-host-writer-fence.json",
    "first-mutation",
)
