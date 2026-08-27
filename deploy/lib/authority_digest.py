"""Deterministic, tamper-evident digest of Menhir's authoritative state (blocker 5).

Candidate acceptance must prove that no authoritative data changed across the
recall and mutation-refusal probes. This module computes that proof as a single
sha256 over two independent layers:

* ``local_files(root)`` hashes every regular file beneath an
  authority root (OAuth, telemetry, queues, leases, sessions, recall/audit/
  usage stores) using canonical relative paths and a fully sorted traversal.
  Symlinks and special entries (sockets, fifos, devices) hard-fail: a candidate
  whose state tree contains one is rejected outright. There is no exclusion
  interface; disposable probe output must be mounted outside this root.

* ``combine(local_hex, neo4j_hex)`` folds the local file digest together with a
  canonical full Neo4j content digest (node labels + properties and
  relationship types + properties + endpoints, supplied pre-computed by the
  caller from a live cypher-shell dump). The pre-probe and post-probe combined
  digests must be byte-for-byte equal for acceptance to pass.

The module has no runtime dependencies beyond the standard library, so it is
unit-testable without Docker or Neo4j.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
from collections.abc import Iterable, Mapping
from typing import Any

_PREFIX_LOCAL = b"authority-local\x00"
_PREFIX_NEO4J = b"authority-neo4j\x00"
_PREFIX_LOCAL_SET = b"authority-local-set\x00"
_PREFIX_NEO4J_RECORD = b"authority-neo4j-record\x00"


NEO4J_AUTHORITY_QUERIES = (
    (
        "nodes",
        "MATCH (n) RETURN elementId(n) AS element_id, labels(n) AS labels, "
        "properties(n) AS properties",
    ),
    (
        "relationships",
        "MATCH (start)-[r]->(end) RETURN elementId(r) AS element_id, "
        "type(r) AS type, elementId(start) AS start_element_id, "
        "elementId(end) AS end_element_id, properties(r) AS properties",
    ),
    ("indexes", "SHOW INDEXES"),
    ("constraints", "SHOW CONSTRAINTS"),
    ("databases", "SHOW DATABASES"),
    ("users", "SHOW USERS"),
    ("roles", "SHOW ROLES WITH USERS"),
    ("privileges", "SHOW PRIVILEGES"),
)

_GRAPH_QUERY_NAMES = frozenset({"nodes", "relationships", "indexes", "constraints"})


def _canonical(rel: str) -> str:
    """Normalize a relative path to forward slashes, rooted at the tree root."""
    return rel.replace("\\", "/").lstrip("/")


def _file_identity(path: str) -> "tuple[int, str]":
    file_digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            size += len(chunk)
            file_digest.update(chunk)
    return size, file_digest.hexdigest()


def _reject_special(root: str) -> None:
    """Refuse symlinks and special (non-regular, non-directory) entries."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = _canonical(os.path.relpath(full, root))
            if os.path.islink(full):
                raise ValueError("authority tree contains symlink: %s" % rel)
            if not (os.path.isfile(full) or os.path.isdir(full)):
                raise ValueError("authority tree contains special entry: %s" % rel)


def local_files(root: str) -> str:
    """Canonical sha256 over every regular file under ``root``.

    Symlinks and special entries are rejected. Every file is hashed.
    """
    if not os.path.isdir(root) or os.path.islink(root):
        raise ValueError("authority root is not a real directory: %s" % root)
    _reject_special(root)

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise ValueError("authority tree contains symlink: %s"
                                 % _canonical(os.path.relpath(full, root)))
            if not os.path.isfile(full):
                raise ValueError("authority tree contains special entry: %s"
                                 % _canonical(os.path.relpath(full, root)))
            rel = _canonical(os.path.relpath(full, root))
            files.append(rel)

    digest = hashlib.sha256()
    for rel in sorted(files):
        size, file_hex = _file_identity(
            os.path.join(root, rel.replace("/", os.sep))
        )
        digest.update(_PREFIX_LOCAL)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\x00")
        digest.update(file_hex.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def combine(local_hex: str, neo4j_hex: str) -> str:
    """Fold the local file digest and the canonical Neo4j digest into one."""
    for label, value in (("local", local_hex), ("neo4j", neo4j_hex)):
        if not isinstance(value, str) or len(value) != 64 or not all(
                c in "0123456789abcdef" for c in value):
            raise ValueError("%s digest must be a 64-char lowercase sha256" % label)
    digest = hashlib.sha256()
    digest.update(b"combine\x00")
    digest.update(_PREFIX_LOCAL)
    digest.update(b"\x00")
    digest.update(local_hex.encode("ascii"))
    digest.update(b"\x00")
    digest.update(_PREFIX_NEO4J)
    digest.update(b"\x00")
    digest.update(neo4j_hex.encode("ascii"))
    return digest.hexdigest()


def _canonical_value(value: Any) -> Any:
    """Return an explicit, type-preserving JSON value for a Neo4j result."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "nan:" + struct.pack(">d", value).hex()
        elif math.isinf(value):
            rendered = "+inf" if value > 0 else "-inf"
        else:
            rendered = value.hex()
        return ["float64", rendered]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]

    value_type = type(value)
    module = value_type.__module__
    type_name = value_type.__name__
    if module.startswith("neo4j.spatial"):
        coordinates = [_canonical_value(item) for item in tuple(value)]
        return ["neo4j-spatial", type_name, int(value.srid), coordinates]
    if module.startswith("neo4j.time"):
        if type_name == "Duration":
            return [
                "neo4j-duration",
                str(value.months),
                str(value.days),
                str(value.seconds),
                str(value.nanoseconds),
            ]
        formatter = getattr(value, "iso_format", None)
        rendered = formatter() if callable(formatter) else str(value)
        return ["neo4j-temporal", type_name, rendered]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Neo4j authority maps require string keys")
        return [
            "map",
            [[key, _canonical_value(value[key])] for key in sorted(value)],
        ]
    if isinstance(value, (list, tuple)):
        return ["list", [_canonical_value(item) for item in value]]
    raise TypeError(
        "unsupported Neo4j authority value type: %s.%s" % (module, type_name)
    )


def _structured_record(record: Mapping[str, Any]) -> bytes:
    canonical = _canonical_value(record)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def structured_records(records: Iterable[Mapping[str, Any]]) -> str:
    """Hash a multiset of collision-safe, type-preserving structured rows."""
    rows = sorted(_structured_record(record) for record in records)
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_PREFIX_NEO4J_RECORD)
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return digest.hexdigest()


def _authority_row(name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize fields whose database semantics are unordered collections."""
    normalized = dict(row)
    unordered_fields = {
        "nodes": ("labels",),
        "databases": ("aliases",),
        "users": ("roles",),
    }
    for field in unordered_fields.get(name, ()):
        value = normalized.get(field)
        if isinstance(value, (list, tuple)):
            normalized[field] = sorted(value, key=_structured_record)
    return normalized


def neo4j_authority_digest(
    *,
    uri: str,
    username: str,
    password: str,
    database: str,
) -> str:
    """Read and hash graph, schema, database, user, role, and privilege authority."""
    from neo4j import GraphDatabase

    records: list[dict[str, Any]] = []
    with GraphDatabase.driver(uri, auth=(username, password)) as driver:
        for name, query in NEO4J_AUTHORITY_QUERIES:
            selected_database = database if name in _GRAPH_QUERY_NAMES else "system"
            with driver.session(database=selected_database) as session:
                for row in session.run(query):
                    records.append({"authority": name, "row": _authority_row(name, row)})
    return structured_records(records)


def _secret(path: str) -> str:
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError("Neo4j password path must be a regular non-symlink file")
    with open(path, encoding="utf-8") as handle:
        value = handle.read().strip()
    if not value:
        raise ValueError("Neo4j password is empty")
    return value


def local_set(named_roots: dict[str, str]) -> str:
    """Hash a fixed set of independently rooted local authority trees."""
    if not isinstance(named_roots, dict) or not named_roots:
        raise ValueError("local authority set must not be empty")
    if any(not label or "/" in label or "\\" in label for label in named_roots):
        raise ValueError("local authority labels must be simple components")
    resolved = [os.path.realpath(path) for path in named_roots.values()]
    if len(resolved) != len(set(resolved)):
        raise ValueError("local authority roots must be unique")
    digest = hashlib.sha256()
    for label in sorted(named_roots):
        value = local_files(named_roots[label])
        digest.update(_PREFIX_LOCAL_SET)
        digest.update(label.encode("ascii"))
        digest.update(b"\x00")
        digest.update(value.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def main(argv: "list[str]") -> int:
    if len(argv) == 3 and argv[1] == "local":
        root = argv[2]
        try:
            print(local_files(root))
            return 0
        except (ValueError, OSError) as exc:
            print("authority local digest failed: %s" % exc, file=sys.stderr)
            return 1
    if len(argv) == 4 and argv[1] == "combine":
        try:
            print(combine(argv[2], argv[3]))
            return 0
        except ValueError as exc:
            print("authority combine failed: %s" % exc, file=sys.stderr)
            return 1
    if len(argv) >= 3 and argv[1] == "local-set":
        try:
            pairs = {}
            for item in argv[2:]:
                label, separator, root = item.partition("=")
                if not separator or label in pairs:
                    raise ValueError("local-set arguments must be unique LABEL=ROOT pairs")
                pairs[label] = root
            print(local_set(pairs))
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print("authority local-set digest failed: %s" % exc, file=sys.stderr)
            return 1
    if len(argv) == 2 and argv[1] == "neo4j":
        try:
            print(neo4j_authority_digest(
                uri=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
                username=os.environ.get("NEO4J_USER", "neo4j"),
                password=_secret("/run/secrets/menhir/neo4j-password"),
                database=os.environ.get("NEO4J_DATABASE", "neo4j"),
            ))
            return 0
        except Exception:
            # Driver failures can contain connection details. Fail closed without
            # echoing exception text into deployment logs.
            print("authority Neo4j digest failed", file=sys.stderr)
            return 1
    print("usage: authority_digest.py <local ROOT | local-set LABEL=ROOT... | "
          "neo4j | combine LOCAL_HEX NEO4J_HEX>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
