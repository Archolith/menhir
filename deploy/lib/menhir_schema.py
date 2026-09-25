"""Strict, duplicate-key-rejecting schemas for Menhir production release artifacts.

This module is the single source of truth for the shape of the immutable
deployment-authority records Menhir owns:

  * ``MANIFEST.json``   - one backup generation: exact set equality against the
                          generation directory, per-file classification
                          (authority/secret/config/disposable), and binding to
                          ``SHA256SUMS``. Extras and unclassified files are
                          rejected.
  * ``release.json``    - immutable root-owned release authority (blocker 7).
                          Unknown labels are rejected; image pins must be
                          digest-pinned; the Dockerfile wheel-hash manifest is
                          mandatory.
  * ``receipt``         - atomic structured receipts emitted by the backup
                          local backup wrapper, the restore rehearsal, and the
                          candidate-acceptance verifier. Promotion consumes the
                          exact parsed fields and never reads mtime.

Every loader rejects duplicate object keys so a JSON document with two
definitions of the same key can never be accepted as two different things.

This module has no runtime dependencies beyond the standard library, so it is
unit-testable without Docker.
"""

import sys
from pathlib import Path

# This module is consumed as a top-level module (deploy/lib on sys.path), as
# ``lib.menhir_schema`` (deploy on sys.path), via spec_from_file_location, and
# executed directly as a script. Bootstrap the sibling directory so the
# extracted ``menhir_schema_*`` aspect modules resolve in every mode.
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from menhir_schema_evidence import (  # noqa: E402
    prerequisite_observation_payload,
    source_fence_payload,
    validate_prerequisite,
    validate_prerequisite_binding,
    validate_source_fence,
    verify_source_fence,
)
from menhir_schema_manifest import FILE_CLASSES, validate_manifest  # noqa: E402
from menhir_schema_receipts import (  # noqa: E402
    validate_backup_promotion,
    validate_desktop_archive,
    validate_receipt,
    validate_receipt_binding,
)
from menhir_schema_release import (  # noqa: E402
    EXPECTED_REPO_REMOTES,
    REQUIRED_SECURITY_REVIEW_SCOPE,
    validate_release,
)
from menhir_schema_support import (  # noqa: E402
    SCHEMA_VERSION,
    load_strict,
    release_authority_sha256,
)


def main(argv):
    if len(argv) < 3:
        print("usage: menhir_schema.py <validate-manifest|validate-release|"
              "validate-receipt|validate-receipt-binding|validate-prerequisite|"
              "validate-prerequisite-binding|validate-source-fence|"
              "verify-source-fence|validate-backup-promotion|"
              "validate-desktop-archive> "
               "<path> [root] [kind] [release_path]", file=sys.stderr)
        return 2
    command, path = argv[1], argv[2]
    try:
        if command == "validate-manifest":
            validate_manifest(path, argv[3])
        elif command == "validate-release":
            validate_release(path)
        elif command == "validate-receipt":
            validate_receipt(path, argv[3])
        elif command == "validate-receipt-binding":
            validate_receipt_binding(path, argv[3], argv[4], argv[5], argv[6],
                                     argv[7], argv[8])
        elif command == "validate-prerequisite":
            validate_prerequisite(path)
        elif command == "validate-prerequisite-binding":
            validate_prerequisite_binding(path, argv[3])
        elif command == "validate-source-fence":
            validate_source_fence(path)
        elif command == "verify-source-fence":
            if len(argv) < 4:
                raise ValueError("verify-source-fence requires release path")
            verify_source_fence(path, argv[3])
        elif command == "validate-backup-promotion":
            validate_backup_promotion(path)
        elif command == "validate-desktop-archive":
            if len(argv) != 5:
                raise ValueError(
                    "validate-desktop-archive requires backup and release paths"
                )
            validate_desktop_archive(path, argv[3], argv[4])
        else:
            print("unknown command: %s" % command, file=sys.stderr)
            return 2
    except (ValueError, OSError) as exc:
        print("validation failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
