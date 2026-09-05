"""Read-only Ed25519 owner-confirmation source for canonical-self assertions."""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from menhir.domain.self_authority import (
    SelfAssertionProposal,
    SelfAuthorizationDecision,
    canonical_json_bytes,
)

_MAX_KEY_BYTES = 16_384
_MAX_CONFIRMATION_BYTES = 1_048_576
_MAX_CONFIRMATIONS = 256

__all__ = ["FileSelfAssertionAuthorizer", "confirmation_filename"]


def confirmation_filename(episode_uuid: str) -> str:
    """Map an untrusted episode identifier to a traversal-proof confirmation filename."""

    return f"{sha256(str(episode_uuid).encode('utf-8')).hexdigest()}.json"


def _normalize_fingerprint(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    return normalized


class FileSelfAssertionAuthorizer:
    """Verify exact signed records without exposing a signing operation to Menhir or its agents.

    The configured public-key fingerprint is mandatory.  A path alone is not a trust anchor: an
    actor able to replace both a key file and a confirmation file could otherwise mint authority.
    Deployments must mount the key and confirmation directory read-only; Menhir never writes them.
    """

    def __init__(
        self,
        *,
        public_key_path: str,
        public_key_sha256: str,
        confirmation_directory: str,
    ) -> None:
        self._public_key_path = str(public_key_path or "").strip()
        self._public_key_sha256 = _normalize_fingerprint(public_key_sha256)
        self._confirmation_directory = str(confirmation_directory or "").strip()
        self._loaded_key: tuple[Ed25519PublicKey, str] | None = None

    def _public_key(self) -> tuple[Ed25519PublicKey, str] | SelfAuthorizationDecision:
        if self._loaded_key is not None:
            return self._loaded_key
        if not self._public_key_path or not self._confirmation_directory:
            return SelfAuthorizationDecision(False, "authority_not_configured")
        if len(self._public_key_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self._public_key_sha256
        ):
            return SelfAuthorizationDecision(False, "authority_fingerprint_not_configured")
        try:
            raw = Path(self._public_key_path).read_bytes()
        except OSError:
            return SelfAuthorizationDecision(False, "authority_key_unreadable")
        if not raw or len(raw) > _MAX_KEY_BYTES:
            return SelfAuthorizationDecision(False, "authority_key_invalid")
        try:
            loaded = serialization.load_pem_public_key(raw)
        except (TypeError, ValueError):
            return SelfAuthorizationDecision(False, "authority_key_invalid")
        if not isinstance(loaded, Ed25519PublicKey):
            return SelfAuthorizationDecision(False, "authority_key_not_ed25519")
        public_raw = loaded.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fingerprint = sha256(public_raw).hexdigest()
        if fingerprint != self._public_key_sha256:
            return SelfAuthorizationDecision(False, "authority_key_fingerprint_mismatch")
        key_id = f"ed25519:{fingerprint[:24]}"
        self._loaded_key = (loaded, key_id)
        return self._loaded_key

    def _records(self, episode_uuid: str) -> list[Any] | SelfAuthorizationDecision:
        path = Path(self._confirmation_directory) / confirmation_filename(episode_uuid)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return SelfAuthorizationDecision(False, "confirmation_not_found")
        except OSError:
            return SelfAuthorizationDecision(False, "confirmation_unreadable")
        if not raw or len(raw) > _MAX_CONFIRMATION_BYTES:
            return SelfAuthorizationDecision(False, "confirmation_file_invalid")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return SelfAuthorizationDecision(False, "confirmation_file_invalid")
        records = document.get("confirmations") if isinstance(document, dict) else document
        if not isinstance(records, list) or len(records) > _MAX_CONFIRMATIONS:
            return SelfAuthorizationDecision(False, "confirmation_file_invalid")
        return records

    def authorize(self, proposal: SelfAssertionProposal) -> SelfAuthorizationDecision:
        """Authorize only a byte-exact payload signed by the pinned owner key."""

        key_result = self._public_key()
        if isinstance(key_result, SelfAuthorizationDecision):
            return key_result
        public_key, key_id = key_result
        records = self._records(proposal.episode_uuid)
        if isinstance(records, SelfAuthorizationDecision):
            return records
        expected = proposal.confirmation_payload()
        saw_exact = False
        for record in records:
            if not isinstance(record, dict) or record.get("payload") != expected:
                continue
            saw_exact = True
            encoded_signature = record.get("signature")
            if not isinstance(encoded_signature, str) or len(encoded_signature) > 256:
                continue
            try:
                signature = base64.b64decode(encoded_signature, validate=True)
            except (binascii.Error, ValueError):
                continue
            try:
                public_key.verify(signature, canonical_json_bytes(expected))
            except InvalidSignature:
                continue
            return SelfAuthorizationDecision(True, "owner_signature_verified", key_id)
        return SelfAuthorizationDecision(
            False,
            "confirmation_signature_invalid" if saw_exact else "confirmation_no_exact_match",
            key_id,
        )
