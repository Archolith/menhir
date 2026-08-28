"""Generate a strict MENHIR generation MANIFEST.json (schema 1).

Enumerates every regular file under a generation directory, assigns each file
an explicit durability class (authority / secret / config), and refuses any
unclassified file. Used by backup-generation.sh and by the pre-restore anchor
so both produce generations accepted by the same strict integrity rules.

Usage:
  make_manifest.py <target> <generation> <menhir_image> <menhir_digest> \
      <neo4j_image> <neo4j_digest> <commit> <sha256sums_sha256> \
      <created_utc> <release_id> <release_manifest_sha256>
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

MARKERS = frozenset({"MANIFEST.json", "SHA256SUMS", "COMPLETE"})

AUTHORITY_FILES = frozenset({
    "neo4j/neo4j.dump",
    "neo4j/system.dump",
    "state/oauth/menhir_oauth_as.db",
    "state/telemetry/mcp_telemetry.db",
})


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify(rel: str) -> str:
    if rel.startswith("secrets/"):
        return "secret"
    if rel in AUTHORITY_FILES:
        return "authority"
    if rel.endswith(".integrity.txt") or rel.startswith("policy/") or rel.startswith("config/"):
        return "config"
    raise SystemExit("unclassified file in generation: %s" % rel)


def main(argv):
    (target, generation, menhir_image, menhir_digest, neo4j_image, neo4j_digest,
     commit, sha256sums, created_utc, release_id, release_manifest_sha256) = argv[1:12]

    files = {}
    for root, _dirs, names in os.walk(target):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, target).replace(os.sep, "/")
            if rel in MARKERS:
                continue
            files[rel] = {"sha256": _sha256(full), "class": classify(rel)}

    manifest = {
        "schema": 1,
        "generation": generation,
        "created_utc": created_utc,
        "build": {
            "repo_commit": commit,
            "menhir_image": menhir_image,
            "menhir_image_digest": menhir_digest,
            "neo4j_image": neo4j_image,
            "neo4j_image_digest": neo4j_digest,
        },
        "release": {
            "release_id": release_id,
            "release_manifest_sha256": release_manifest_sha256,
        },
        "restore_order": ["neo4j", "system", "oauth", "telemetry", "secrets", "policy"],
        "files": files,
        "sha256sums_sha256": sha256sums,
    }
    with open(os.path.join(target, "MANIFEST.json"), "w", encoding="ascii") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
