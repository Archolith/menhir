"""Facade package re-exporting the decomposed source-fence authoring blocks.

``deploy/source-fence-author.py`` imports every name below from this package so
the original ``source-fence-author`` module path keeps exposing the same surface.
"""

from .base import (
    EVIDENCE_MAX_AGE as EVIDENCE_MAX_AGE,
    MAX_JSON_BYTES as MAX_JSON_BYTES,
    MAX_KEY_BYTES as MAX_KEY_BYTES,
    MAX_TOKEN_BYTES as MAX_TOKEN_BYTES,
    NETWORK_TIMEOUT_SECONDS as NETWORK_TIMEOUT_SECONDS,
    RECEIPT_VALIDITY as RECEIPT_VALIDITY,
    SourceFenceError as SourceFenceError,
    _B64URL_RE as _B64URL_RE,
    _CHALLENGE_KEYS as _CHALLENGE_KEYS,
    _CHALLENGE_RE as _CHALLENGE_RE,
    _DNS_RE as _DNS_RE,
    _EVIDENCE_COMMON_KEYS as _EVIDENCE_COMMON_KEYS,
    _REFUSAL as _REFUSAL,
)
from .fsio import (
    _decode_b64url as _decode_b64url,
    _inspect_regular as _inspect_regular,
    _read_regular as _read_regular,
    _require_root_owned_nonwritable as _require_root_owned_nonwritable,
    _strict_json_bytes as _strict_json_bytes,
)
from .probe import (
    ProbeRequest as ProbeRequest,
    ProbeResponse as ProbeResponse,
    TLSFiles as TLSFiles,
    Transport as Transport,
    _RejectRedirects as _RejectRedirects,
    _call as _call,
    _canonical_https_origin as _canonical_https_origin,
    _default_transport as _default_transport,
    _header as _header,
    _read_response_json as _read_response_json,
    _verify_challenge_response as _verify_challenge_response,
    _verify_mutation_refusal as _verify_mutation_refusal,
)
from .timeline import (
    _assert_fresh as _assert_fresh,
    _parse_utc as _parse_utc,
    _utc_now as _utc_now,
)
