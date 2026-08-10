"""Versioned, lossless merge snapshot codec (remediation plan Phase 4, section 2).

The legacy merge snapshot was a 10-property allowlist with Entity-only peers, one relationship
property (``weight``), stringified temporals, and no survivor state -- so an unmerge could never be
an exact inverse. This codec replaces it with a snapshot that can express one.

Design rules taken directly from the plan:

* **No implicit allowlist.** Every node property is captured via ``properties(n)``. If a property is
  deliberately excluded from restoration it must be named in ``SYSTEM_MANAGED_PROPERTIES`` and
  tested, not silently dropped.
* **Do not stringify Neo4j temporal values.** Dates, times, datetimes, durations and points are
  encoded as tagged structures that decode back to the SAME driver types. A round trip is exact.
* **Every relationship instance**, not a ``(peer, type, direction)`` set: parallel edges are distinct
  entries, and relationship properties are captured in full.
* **Both identities.** The survivor's pre-merge state is snapshotted alongside the absorbed node's,
  because reversing a merge means reversing the survivor's delta too.
* **Versioned + checksummed.** An unsupported schema version fails closed rather than being
  reinterpreted as a newer, more complete one.

The codec is pure: it maps driver values <-> JSON-safe structures. Reading the graph is the
repository's job (see ``CorrelationRepository.capture_merge_snapshot``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from neo4j.spatial import CartesianPoint, Point, WGS84Point
from neo4j.time import Date, DateTime, Duration, Time

#: Bump ONLY when the snapshot structure changes incompatibly. Readers must keep handling every
#: version they ever wrote (plan: "retain backward readers; never reinterpret an old schema as a
#: newer complete one").
SCHEMA_VERSION = 1

#: Versions this build can decode. A snapshot outside this set fails closed.
SUPPORTED_VERSIONS = frozenset({1})

#: Properties intentionally NOT restored by an unmerge because the graph owns them. Named here (and
#: covered by tests) so the exclusion is explicit rather than an implicit allowlist. They are still
#: CAPTURED in the snapshot -- they are simply not authoritative on restore.
SYSTEM_MANAGED_PROPERTIES = frozenset({"last_accessed"})

_TAG = "$type"


#: Documented hard ceiling on a serialized snapshot (invariant 15). A high-degree hub can have tens
#: of thousands of incident edges; a snapshot that large is both a sidecar-bloat problem and a sign
#: the merge deserves human attention. The merge ABSTAINS -- the data is never truncated to make it
#: fit, because a truncated snapshot silently destroys the inverse it exists to guarantee.
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024  # 8 MiB


class SnapshotSchemaError(ValueError):
    """The snapshot's schema version is unsupported, or its checksum does not verify."""


class SnapshotTooLargeError(SnapshotSchemaError):
    """The serialized snapshot exceeds MAX_SNAPSHOT_BYTES (invariant 15).

    A subclass of SnapshotSchemaError so existing fail-closed handlers still catch it, but a distinct
    type so the coordinator can report the dedicated MERGE_SNAPSHOT_TOO_LARGE reason code.
    """

    def __init__(self, size_bytes: int, limit: int = MAX_SNAPSHOT_BYTES) -> None:
        super().__init__(
            f"merge snapshot is {size_bytes} bytes, over the {limit}-byte limit; "
            "abstaining (snapshot data is never truncated)"
        )
        self.size_bytes = size_bytes
        self.limit = limit


# --------------------------------------------------------------------------- value codec

# srid -> constructor. The driver models a point's coordinate system by subclass, so a bare
# Point(coords) would lose the srid on a round trip; reconstruct the right subclass instead.
_SRID_CARTESIAN_2D = 7203
_SRID_CARTESIAN_3D = 9157
_SRID_WGS84_2D = 4326
_SRID_WGS84_3D = 4979


def encode_value(value: Any) -> Any:
    """Encode one Neo4j/driver value into a JSON-safe, losslessly decodable structure."""
    # bool BEFORE int: bool is a subclass of int in Python and would otherwise encode as a number.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {_TAG: "bytes", "value": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, DateTime):
        return {_TAG: "DateTime", "value": value.iso_format()}
    if isinstance(value, Date):
        return {_TAG: "Date", "value": value.iso_format()}
    if isinstance(value, Time):
        return {_TAG: "Time", "value": value.iso_format()}
    if isinstance(value, Duration):
        # Duration is NOT a fixed span (months and days are calendar-relative), so it has no exact
        # ISO seconds form. Preserve its four components verbatim.
        return {
            _TAG: "Duration",
            "months": value.months,
            "days": value.days,
            "seconds": value.seconds,
            "nanoseconds": value.nanoseconds,
        }
    if isinstance(value, Point):
        return {_TAG: "Point", "srid": int(value.srid), "coordinates": [float(c) for c in value]}
    if isinstance(value, (list, tuple)):
        # A JSON array is unambiguous (only tagged OBJECTS carry $type), so lists stay arrays.
        return [encode_value(v) for v in value]
    if isinstance(value, dict):
        # Wrapped so a user map that happens to contain a "$type" key cannot be misread as a tag.
        return {_TAG: "map", "value": {str(k): encode_value(v) for k, v in value.items()}}
    raise SnapshotSchemaError(f"unencodable value of type {type(value).__name__}: {value!r}")


def decode_value(value: Any) -> Any:
    """Inverse of :func:`encode_value`. Returns the ORIGINAL driver types, not strings."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    if not isinstance(value, dict):
        raise SnapshotSchemaError(f"undecodable node in snapshot: {value!r}")

    tag = value.get(_TAG)
    if tag == "map":
        return {k: decode_value(v) for k, v in (value.get("value") or {}).items()}
    if tag == "bytes":
        return base64.b64decode(str(value["value"]).encode("ascii"))
    if tag == "DateTime":
        return DateTime.from_iso_format(str(value["value"]))
    if tag == "Date":
        return Date.from_iso_format(str(value["value"]))
    if tag == "Time":
        return Time.from_iso_format(str(value["value"]))
    if tag == "Duration":
        return Duration(
            months=int(value.get("months", 0)),
            days=int(value.get("days", 0)),
            seconds=int(value.get("seconds", 0)),
            nanoseconds=int(value.get("nanoseconds", 0)),
        )
    if tag == "Point":
        srid = int(value["srid"])
        coords = [float(c) for c in value["coordinates"]]
        if srid in (_SRID_WGS84_2D, _SRID_WGS84_3D):
            return WGS84Point(coords)
        if srid in (_SRID_CARTESIAN_2D, _SRID_CARTESIAN_3D):
            return CartesianPoint(coords)
        # Unknown srid: do not silently coerce it into the wrong coordinate system.
        raise SnapshotSchemaError(f"unsupported point srid {srid}")
    raise SnapshotSchemaError(f"unknown tagged value {tag!r} in snapshot")


def encode_properties(props: dict[str, Any]) -> dict[str, Any]:
    return {str(k): encode_value(v) for k, v in (props or {}).items()}


def decode_properties(props: dict[str, Any]) -> dict[str, Any]:
    return {str(k): decode_value(v) for k, v in (props or {}).items()}


# --------------------------------------------------------------------------- checksum / envelope

def _canonical(payload: Any) -> str:
    """Deterministic JSON. The basis of the checksum, so key order can never change the digest."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_checksum(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def build_snapshot(
    *,
    survivor: dict[str, Any],
    absorbed: dict[str, Any],
    similarity: float | None = None,
) -> dict[str, Any]:
    """Wrap two already-encoded node snapshots in a versioned, checksummed envelope.

    ``survivor``/``absorbed`` are the encoded node bodies produced by :func:`encode_node`.
    """
    body = {
        "survivor": survivor,
        "absorbed": absorbed,
        "similarity": (float(similarity) if similarity is not None else None),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "checksum": compute_checksum(body),
        "body": body,
    }


def load_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate version + checksum and return the body. Fails closed on anything unexpected."""
    version = snapshot.get("schema_version")
    if version not in SUPPORTED_VERSIONS:
        raise SnapshotSchemaError(
            f"unsupported merge-snapshot schema_version {version!r}; "
            f"this build reads {sorted(SUPPORTED_VERSIONS)}"
        )
    body = snapshot.get("body")
    if not isinstance(body, dict):
        raise SnapshotSchemaError("merge snapshot has no body")
    expected = snapshot.get("checksum")
    actual = compute_checksum(body)
    if expected != actual:
        raise SnapshotSchemaError(
            f"merge-snapshot checksum mismatch (expected {expected}, computed {actual}); "
            "the snapshot is corrupt and must not be used to restore"
        )
    return body


def dumps(snapshot: dict[str, Any], *, enforce_size_limit: bool = True) -> str:
    """Serialize a snapshot, refusing to emit one over the documented size ceiling.

    Enforced HERE (not at the storage layer) so every writer inherits the guarantee, and enforced by
    ABSTAINING rather than truncating: a snapshot that cannot be stored whole cannot invert a merge,
    and a merge without an inverse must not proceed (invariant 15).
    """
    text = _canonical(snapshot)
    if enforce_size_limit:
        # Read the limit at CALL time, and report the limit actually enforced. Binding it as a
        # default arg would freeze it at import and misreport the ceiling under an override.
        limit = MAX_SNAPSHOT_BYTES
        size = len(text.encode("utf-8"))
        if size > limit:
            raise SnapshotTooLargeError(size, limit)
    return text


def snapshot_size_bytes(snapshot: dict[str, Any]) -> int:
    """Serialized size, for observe-mode reporting of near-limit (high-degree) merge candidates."""
    return len(_canonical(snapshot).encode("utf-8"))


def loads(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise SnapshotSchemaError(f"merge snapshot is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------- node encoding

def encode_node(
    *,
    uuid: str,
    labels: list[str],
    properties: dict[str, Any],
    relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Encode one node's complete state.

    ``relationships`` is a list of per-INSTANCE records (parallel edges appear more than once), each
    with: ``type``, ``direction`` ('out'|'in'), ``properties``, ``peer_uuid``, ``peer_labels``, and
    ``peer_identity`` (a best-effort human identifier used to REPORT an unresolvable peer -- never to
    fabricate one).
    """
    return {
        "uuid": str(uuid),
        "labels": sorted(str(x) for x in (labels or [])),
        "properties": encode_properties(properties),
        "relationships": [
            {
                "type": str(r["type"]),
                "direction": str(r["direction"]),
                "properties": encode_properties(r.get("properties") or {}),
                "peer_uuid": (str(r["peer_uuid"]) if r.get("peer_uuid") is not None else None),
                "peer_labels": sorted(str(x) for x in (r.get("peer_labels") or [])),
                "peer_identity": (
                    str(r["peer_identity"]) if r.get("peer_identity") is not None else None
                ),
            }
            for r in (relationships or [])
        ],
    }


def decode_node(node: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`encode_node`: properties and relationship properties become driver types."""
    return {
        "uuid": str(node["uuid"]),
        "labels": list(node.get("labels") or []),
        "properties": decode_properties(node.get("properties") or {}),
        "relationships": [
            {
                "type": str(r["type"]),
                "direction": str(r["direction"]),
                "properties": decode_properties(r.get("properties") or {}),
                "peer_uuid": r.get("peer_uuid"),
                "peer_labels": list(r.get("peer_labels") or []),
                "peer_identity": r.get("peer_identity"),
            }
            for r in (node.get("relationships") or [])
        ],
    }


def restorable_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """The properties an unmerge should write back: everything except the system-managed ones.

    Explicit by contract (plan section 2): the exclusion set is named and tested, never implicit.
    """
    return {k: v for k, v in properties.items() if k not in SYSTEM_MANAGED_PROPERTIES}
