"""Facade package re-exporting the decomposed install-bundle building blocks.

``deploy/build_install_bundle.py`` imports every name below from this
package so the original ``build_install_bundle`` module path keeps
exposing the same surface."""

from .authority import (  # noqa: E402
    _git_blob as _git_blob,
    _run_git as _run_git,
    _validate_repository as _validate_repository,
    _validate_spec_relationship as _validate_spec_relationship,
)
from .bundle import (  # noqa: E402
    _bundle_census as _bundle_census,
    _manifest_bytes as _manifest_bytes,
    _normalize_timestamps as _normalize_timestamps,
    _output_fence as _output_fence,
    _remove_tree as _remove_tree,
    _validate_bundle as _validate_bundle,
    _write_file as _write_file,
)
from .core import (  # noqa: E402
    ALLOWED_GIT_MODES as ALLOWED_GIT_MODES,
    DuplicateKeyError as DuplicateKeyError,
    EVIDENCE_DIGESTS as EVIDENCE_DIGESTS,
    INSTALLER_NAME as INSTALLER_NAME,
    INSTALLER_SOURCE_NAME as INSTALLER_SOURCE_NAME,
    MANIFEST_NAME as MANIFEST_NAME,
    OID_RE as OID_RE,
    PUBLICATION_EVIDENCE as PUBLICATION_EVIDENCE,
    RELEASE_DESTINATION as RELEASE_DESTINATION,
    RENDERED_DESTINATIONS as RENDERED_DESTINATIONS,
    REPOSITORIES as REPOSITORIES,
    SHA256_RE as SHA256_RE,
    SPEC_KEYS as SPEC_KEYS,
    SPEC_KEYS_INHERITED as SPEC_KEYS_INHERITED,
    SPEC_KEYS_WITH_PROVENANCE as SPEC_KEYS_WITH_PROVENANCE,
    STAGING_RUNNER_DESTINATION as STAGING_RUNNER_DESTINATION,
)
from .fsio import (  # noqa: E402
    _canonical_destination as _canonical_destination,
    _canonical_source_path as _canonical_source_path,
    _destination_mode as _destination_mode,
    _directory as _directory,
    _exact_keys as _exact_keys,
    _load_installed_destinations as _load_installed_destinations,
    _regular_file as _regular_file,
    _sha256_bytes as _sha256_bytes,
    _sha256_file as _sha256_file,
    _strict_json as _strict_json,
    _unique_object as _unique_object,
)
