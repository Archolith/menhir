"""Scanner image pinning and structural validation of Syft/Grype reports."""

from __future__ import annotations

import json
from typing import Any

from .base import DIGEST_RE, MAX_EVIDENCE_BYTES, SCANNER_REPOSITORIES, BuildImageError


def _validate_scanner_image(value: Any, scanner: str) -> str:
    if not isinstance(value, str):
        raise BuildImageError(f"{scanner} scanner image is required")
    repository, separator, digest = value.rpartition("@")
    if (not separator or repository not in SCANNER_REPOSITORIES[scanner]
            or DIGEST_RE.fullmatch(digest) is None):
        raise BuildImageError(
            f"{scanner} scanner must use its official image pinned by SHA-256 digest"
        )
    return value


def _scanner_descriptor(document: dict[str, Any], scanner: str) -> str:
    descriptor = document.get("descriptor")
    if not isinstance(descriptor, dict):
        raise BuildImageError(f"{scanner} report descriptor is missing")
    name = descriptor.get("name")
    version = descriptor.get("version")
    if not isinstance(name, str) or name.lower() != scanner:
        raise BuildImageError(f"{scanner} report names an unexpected scanner")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise BuildImageError(f"{scanner} report version is missing or malformed")
    return version


def _scanner_source_image_id(
    document: dict[str, Any], scanner: str, expected_image_id: str,
) -> str:
    source = document.get("source")
    if not isinstance(source, dict) or source.get("type") != "image":
        raise BuildImageError(f"{scanner} report source is not a container image")
    candidates: list[Any] = []
    for field in ("metadata", "target"):
        nested = source.get(field)
        if isinstance(nested, dict):
            candidates.extend((nested.get("id"), nested.get("imageID")))
    candidates.extend((source.get("imageID"), source.get("id")))
    image_ids = {
        value for value in candidates
        if isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None
    }
    if expected_image_id not in image_ids:
        raise BuildImageError(
            f"{scanner} report does not identify the expected candidate image"
        )
    return expected_image_id


def _parse_report(raw: bytes, scanner: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise BuildImageError(f"{scanner} report is empty or too large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildImageError(f"{scanner} report is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise BuildImageError(f"{scanner} report must contain a JSON object")
    return document
