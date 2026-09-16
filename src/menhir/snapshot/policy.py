"""What goes into a snapshot bundle, what is left out, and what blocks the sync entirely.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (product and bundle contract).

Three outcomes, and the difference between the last two is the whole point:

``INCLUDE``
    The file's current working-tree bytes go into the bundle.

``OMIT``
    Deliberately left out and **declared in the manifest**. The server cannot observe the
    difference between "never uploaded" and "deleted upstream" by looking at the extracted tree,
    and structure writes prune, so an undeclared omission would eventually delete real rows.

``REFUSE``
    The sync does not happen. Reserved for content whose upload is itself the harm -- a real
    ``.env``, private key material -- because unlike a wrong graph row, an uploaded secret cannot
    be taken back by fixing the next sync.

**Why exclusions mirror the scanner.** Directories the server-side scanner would skip anyway are
dropped here so the bundle stays small, and the sets are IMPORTED from
:mod:`menhir.infrastructure.project_scanner` rather than restated. If they drifted, the bundle
would either carry megabytes the scan ignores or -- much worse -- omit something the scan expects,
and the P3 shadow-scan parity gate would fail for a reason no one could see in this file.

**Why secret detection is a name denylist, and what that costs.** It cannot see inside a file, so
it will refuse a harmless ``fixtures/dummy.pem`` and will not notice a secret in
``config/settings.py``. That asymmetry is deliberate: the false positive is visible and
overridable in one command, the false negative is invisible either way, and a rules-based
sanitizer is the security-critical component here -- it fails open and the failure is
one-directional.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from enum import Enum

from menhir.infrastructure.project_scanner import (
    _ALWAYS_SKIP_DIRS,
    _ROOT_ONLY_SKIP_DIRS,
)
from menhir.snapshot.protocol import OmissionReason

__all__ = [
    "Decision",
    "PathVerdict",
    "SelectionPolicy",
    "excluded_dir_reason",
    "is_local_receipt",
    "secret_risk_label",
]


class Decision(Enum):
    INCLUDE = "include"
    OMIT = "omit"
    REFUSE = "refuse"


@dataclass(frozen=True)
class PathVerdict:
    decision: Decision
    #: For OMIT, an :class:`~menhir.snapshot.protocol.OmissionReason`. For REFUSE, a short stable
    #: label. Empty for INCLUDE.
    reason: str = ""
    #: Human sentence for the local report. Never sent to the server.
    explanation: str = ""


#: Directories whose contents the server-side scanner skips. Imported, never restated -- see the
#: module docstring.
EXCLUDED_DIR_NAMES = frozenset(_ALWAYS_SKIP_DIRS)
ROOT_ONLY_EXCLUDED_DIR_NAMES = frozenset(_ROOT_ONLY_SKIP_DIRS)

#: Menhir's own per-checkout identity and sync receipts. They are host-local by construction and
#: `.agent/project-id` is gitignored by design, so this normally matches nothing -- it exists for
#: the repository that tracked one anyway, where uploading it would hand the server a second
#: claim on an identity it already owns.
_RECEIPT_PATHS = frozenset({".agent/project-id"})
_RECEIPT_DIRS = frozenset({".menhir"})

#: Real dotenv files. The example/template forms below are explicitly fine -- refusing them would
#: block the sync of nearly every repository for no gain.
_ENV_SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")

_SECRET_BASENAMES = frozenset(
    {
        ".envrc",
        ".netrc",
        "_netrc",
        ".pgpass",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)

_SECRET_EXTENSIONS = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".kdbx", ".asc"}
)


def is_local_receipt(path: str) -> bool:
    """True for Menhir's own identity/sync receipts, which never travel."""
    if path in _RECEIPT_PATHS:
        return True
    head = path.split("/", 1)[0]
    return head in _RECEIPT_DIRS


def excluded_dir_reason(path: str) -> str | None:
    """Return a reason when *path* sits under a directory the scanner skips, else None.

    Root-only names (``results``, ``logs``, ``coverage``) are matched at the first segment only,
    for the reason the scanner records: pruning them at any depth would delete a legitimate
    ``src/<pkg>/logs/`` package from every project that has one.
    """
    segments = path.split("/")
    directories = segments[:-1]
    for segment in directories:
        if segment in EXCLUDED_DIR_NAMES:
            return OmissionReason.EXCLUDED_DIR
    if directories and directories[0] in ROOT_ONLY_EXCLUDED_DIR_NAMES:
        return OmissionReason.EXCLUDED_DIR
    return None


def secret_risk_label(path: str) -> str | None:
    """Return a short label when *path* looks like secret material, else None."""
    basename = posixpath.basename(path)
    lowered = basename.lower()

    # `.env`, `.env.local`, `.env.production` -- but not `.env.example` and friends.
    if lowered.startswith(".env") and not lowered.endswith(_ENV_SAFE_SUFFIXES):
        return "dotenv file (may hold real credentials)"
    if lowered in _SECRET_BASENAMES:
        return "credential file"
    suffix = posixpath.splitext(lowered)[1]
    if suffix in _SECRET_EXTENSIONS:
        return "private key or keystore material"
    return None


@dataclass(frozen=True)
class SelectionPolicy:
    """Applies the rules above, with explicit per-path secret overrides.

    ``allowed_secret_paths`` is supplied per invocation rather than persisted. That is the
    conservative branch of the plan's open decision 3 -- a stored override silently re-authorizes
    every future sync, and re-typing it is cheap next to uploading a key nobody meant to send.
    Making it persistent later is a one-line change; making an already-uploaded secret unsent is
    not.
    """

    max_file_bytes: int
    allowed_secret_paths: frozenset[str] = field(default_factory=frozenset)

    def classify(self, path: str, size: int) -> PathVerdict:
        if is_local_receipt(path):
            return PathVerdict(
                Decision.OMIT, OmissionReason.RECEIPT, "Menhir's own local identity receipt."
            )
        excluded = excluded_dir_reason(path)
        if excluded:
            return PathVerdict(
                Decision.OMIT, excluded, "inside a directory the structure scanner skips."
            )
        secret = secret_risk_label(path)
        if secret and path not in self.allowed_secret_paths:
            return PathVerdict(Decision.REFUSE, "secret_risk", secret)
        if size > self.max_file_bytes:
            return PathVerdict(
                Decision.OMIT,
                OmissionReason.OVERSIZE,
                f"{size} bytes is over the {self.max_file_bytes}-byte per-file limit.",
            )
        return PathVerdict(Decision.INCLUDE)
