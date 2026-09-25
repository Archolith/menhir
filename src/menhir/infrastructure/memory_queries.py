"""Read-path query repository extracted from MemoryGraphAdapter.

Houses memory listing, lookup, flag/delete, and recall-scoring queries.
All methods preserve the exact signatures and return types of the
original MemoryGraphAdapter methods they replace.

The implementation lives in ``memory_queries_*`` sibling modules, composed into
``MemoryQueryRepository`` below; every moved public symbol is re-exported here
so the original import path keeps working unchanged.
"""

from __future__ import annotations

import logging
import re

from menhir.infrastructure.memory_queries_admission import (
    ADMISSION_LINKED,
    ADMISSION_NEVER_LINKED,
    ADMISSION_NO_TURNS,
    admission_provenance_state,
)
from menhir.infrastructure.memory_queries_erasure import MemoryQueryErasureMixin
from menhir.infrastructure.memory_queries_flagging import MemoryQueryFlaggingMixin
from menhir.infrastructure.memory_queries_listing import MemoryQueryListingMixin
from menhir.infrastructure.memory_queries_namespace_erasure import (
    MemoryQueryNamespaceErasureMixin,
)
from menhir.infrastructure.memory_queries_recall import MemoryQueryRecallMixin
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)

_OPAQUE_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_DIGEST_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _validated_evidence_tombstone_params(
    *, evidence_digest: str | None, digest_key_id: str | None
) -> dict[str, str] | None:
    """Validate an already-keyed opaque erasure identity without deriving one locally.

    These repositories currently receive only the raw evidence identifier and have no HMAC
    material or caller-supplied digest/key-id fields.  Returning ``None`` when both values are
    absent keeps current public signatures compatible; supplying only one value, or a value that
    is not an opaque token, fails closed.  A future service boundary may call this helper after it
    computes the digest with managed key material.  This helper intentionally never accepts or
    hashes the raw erased identifier.
    """
    digest = str(evidence_digest or "").strip()
    key_id = str(digest_key_id or "").strip()
    if not digest and not key_id:
        return None
    if not digest or not key_id:
        raise ValueError("evidence tombstones require both an opaque digest and digest key id")
    if _OPAQUE_DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("evidence tombstone digest must be an opaque base64url/hex-like token")
    if _DIGEST_KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise ValueError("evidence tombstone digest key id is invalid")
    return {"evidence_digest": digest, "digest_key_id": key_id}


class MemoryQueryRepository(
    MemoryQueryListingMixin,
    MemoryQueryRecallMixin,
    MemoryQueryFlaggingMixin,
    MemoryQueryErasureMixin,
    MemoryQueryNamespaceErasureMixin,
):
    """Encapsulates memory read/query operations and simple mutations (flag, delete)."""

    def __init__(self, neo4j: Neo4jRepository) -> None:
        self.neo4j = neo4j
