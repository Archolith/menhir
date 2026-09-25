"""Facade package re-exporting the decomposed release-flow building blocks.

``deploy/release_flow.py`` imports every name below from this package so the
original ``release_flow`` module path keeps exposing the same surface.
"""

from .classification import (
    _candidate_deployment_class as _candidate_deployment_class,
    _deployment_class as _deployment_class,
    _git as _git,
    _verify_fragment_coverage as _verify_fragment_coverage,
)
from .core import (
    APP_ONLY_SOURCE_PATHS as APP_ONLY_SOURCE_PATHS,
    BUNDLE_NAME as BUNDLE_NAME,
    CLASS_ORDER as CLASS_ORDER,
    COMMIT_RE as COMMIT_RE,
    FROZEN_ARTIFACT_KEYS as FROZEN_ARTIFACT_KEYS,
    KIND as KIND,
    LEGACY_STATE_KEYS as LEGACY_STATE_KEYS,
    MAINTENANCE_PATHS as MAINTENANCE_PATHS,
    NOTES_JSON_NAME as NOTES_JSON_NAME,
    NOTES_MARKDOWN_NAME as NOTES_MARKDOWN_NAME,
    PHASES as PHASES,
    PRE_INGRESS_STATE_KEYS as PRE_INGRESS_STATE_KEYS,
    PUBLICATION_KIND as PUBLICATION_KIND,
    PUBLICATION_NONCE_RE as PUBLICATION_NONCE_RE,
    PUBLICATION_RECEIPT_NAME as PUBLICATION_RECEIPT_NAME,
    RELEASE_AUTHORITY_BINDINGS as RELEASE_AUTHORITY_BINDINGS,
    RELEASE_ID_PARTS_RE as RELEASE_ID_PARTS_RE,
    RELEASE_ID_RE as RELEASE_ID_RE,
    RELEASE_NAME as RELEASE_NAME,
    REPOSITORIES as REPOSITORIES,
    REVIEW_REQUEST_NAME as REVIEW_REQUEST_NAME,
    SCHEMA as SCHEMA,
    SECURITY_CONFIG_PATHS as SECURITY_CONFIG_PATHS,
    SHA256_RE as SHA256_RE,
    SPEC_NAME as SPEC_NAME,
    STATE_KEYS as STATE_KEYS,
    STATE_NAME as STATE_NAME,
    VERSION_RE as VERSION_RE,
    ReleaseFlowError as ReleaseFlowError,
)
from .fragments import (
    _fragment_value as _fragment_value,
    _snapshot_fragments as _snapshot_fragments,
    _validate_fragment_bindings as _validate_fragment_bindings,
)
from .fsio import (
    _atomic_json as _atomic_json,
    _atomic_text as _atomic_text,
    _json_sha256 as _json_sha256,
    _json_text as _json_text,
    _load_json as _load_json,
    _regular_directory as _regular_directory,
    _regular_file as _regular_file,
    _remove_managed_path as _remove_managed_path,
    _remove_private_tree as _remove_private_tree,
    _sha256 as _sha256,
    _tree_sha256 as _tree_sha256,
    _unique_pairs as _unique_pairs,
    _workspace as _workspace,
)
from .identity import (
    _verify_next_release_id as _verify_next_release_id,
    next_release_id as next_release_id,
)
from .publication import (
    _publication_paths as _publication_paths,
    _publication_receipt as _publication_receipt,
    _verify_archive as _verify_archive,
    _verify_publication as _verify_publication,
)
from .publishing import (
    publish_flow as publish_flow,
)
from .verification import (
    _load_state as _load_state,
    _state_path as _state_path,
    _verify_release_authority_bindings as _verify_release_authority_bindings,
    _verify_staged_files as _verify_staged_files,
)
