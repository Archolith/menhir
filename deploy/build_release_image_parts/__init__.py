"""Facade package re-exporting the decomposed release-image building blocks.

``deploy/build_release_image.py`` imports every name below from this package so
the original ``build_release_image`` module path keeps exposing the same surface.
"""

from .base import (
    CANONICAL_SOURCE_REPOSITORY as CANONICAL_SOURCE_REPOSITORY,
    BuildImageError as BuildImageError,
    DIGEST_RE as DIGEST_RE,
    EVIDENCE_FILES as EVIDENCE_FILES,
    LABELS as LABELS,
    MAX_EVIDENCE_BYTES as MAX_EVIDENCE_BYTES,
    PUSH_DIGEST_RE as PUSH_DIGEST_RE,
    REMOTE_DIGEST_RE as REMOTE_DIGEST_RE,
    SCANNER_REPOSITORIES as SCANNER_REPOSITORIES,
    SEVERITIES as SEVERITIES,
    SHA256_RE as SHA256_RE,
    VERSION_RE as VERSION_RE,
    sha256_bytes as sha256_bytes,
    sha256_file as sha256_file,
)
from .cli import (
    _validate_common_args as _validate_common_args,
    parser as parser,
)
from .context import (
    _repository_relative as _repository_relative,
    build_command as build_command,
    committed_build_context as committed_build_context,
    git_source_date_epoch as git_source_date_epoch,
    inspect_image as inspect_image,
    oauth_wheel_sha256 as oauth_wheel_sha256,
)
from .evidenceio import (
    _read_bytes as _read_bytes,
    _read_json_bytes as _read_json_bytes,
    _require_sha256 as _require_sha256,
    _verify_artifact_file as _verify_artifact_file,
    _write_evidence as _write_evidence,
    atomic_json as atomic_json,
)
from .registry import (
    _push_digest as _push_digest,
    remote_manifest_digest as remote_manifest_digest,
)
from .scanners import (
    _parse_report as _parse_report,
    _scanner_descriptor as _scanner_descriptor,
    _scanner_source_image_id as _scanner_source_image_id,
    _validate_scanner_image as _validate_scanner_image,
)
