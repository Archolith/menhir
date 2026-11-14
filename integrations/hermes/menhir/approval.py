"""Profile-local, user-confirmed memory-write proposals for the Hermes provider.

Writing an episode requires two independent events: an agent tool call stages it;
a subsequent directly-authored user turn containing its random approval ID commits it.
This is deliberately not an LLM-callable approve tool.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

APPROVAL_TTL_SECONDS = 86_400
MAX_PENDING_PROPOSALS = 64
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{24}$")
_DECISION_PATTERN = re.compile(r"^(Approve|Reject) Menhir memory ([a-zA-Z0-9_-]{24})$", re.IGNORECASE)


def parse_decision(message: str) -> tuple[str, str] | None:
    """Require the entire user message to match; instructions embedded in other text never authorize writes."""
    match = _DECISION_PATTERN.fullmatch(message.strip())
    return (match.group(1).lower(), match.group(2)) if match else None


class PendingApprovalStore:
    def __init__(self, hermes_home: str, namespace: str):
        self._path = Path(hermes_home) / "menhir" / "pending"
        self._namespace = namespace

    def _filename(self, approval_id: str) -> Path:
        if not _ID_PATTERN.fullmatch(approval_id):
            raise ValueError("Invalid Menhir approval ID")
        return self._path / (approval_id + ".json")

    def stage(self, episode: str, session_id: str, author_id: str = "") -> str:
        """Create a private, expiring proposal. Never call Menhir from this step."""
        self._path.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = sorted(self._path.glob("*.json"))
        if len(pending) >= MAX_PENDING_PROPOSALS:
            # Reading removes expired proposals. Do not delete files we did not create.
            for filename in pending:
                if _ID_PATTERN.fullmatch(filename.stem):
                    self.read(filename.stem)
            if len(list(self._path.glob("*.json"))) >= MAX_PENDING_PROPOSALS:
                raise ValueError("Menhir has too many pending proposals; approve or reject some first")
        for _ in range(3):
            approval_id = secrets.token_urlsafe(18)
            filename = self._filename(approval_id)
            try:
                descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"schema": "hermes-menhir-approval-v1", "episode": episode,
                               "namespace": self._namespace, "session_id": session_id,
                               "author_id": author_id, "created_at": time.time()}, stream)
            except BaseException:
                filename.unlink(missing_ok=True)
                raise
            return approval_id
        raise RuntimeError("Could not allocate a unique Menhir approval ID")

    def read(self, approval_id: str) -> dict[str, Any] | None:
        filename = self._filename(approval_id)
        try:
            if filename.is_symlink():
                return None
            if filename.stat().st_size > 16_384:
                return None
            data = json.loads(filename.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None
        if (not isinstance(data, dict) or data.get("schema") != "hermes-menhir-approval-v1"
                or data.get("namespace") != self._namespace):
            return None
        episode = data.get("episode")
        created_at = data.get("created_at")
        if not isinstance(episode, str) or not episode.strip() or len(episode) > 4_000:
            return None
        if type(created_at) not in {int, float}:
            return None
        age = time.time() - created_at
        if age > APPROVAL_TTL_SECONDS:
            self.remove(approval_id)  # Expired proposals cannot later become authorized.
            return None
        if age < 0:
            return None
        return data

    def remove(self, approval_id: str) -> None:
        filename = self._filename(approval_id)
        if not filename.is_symlink():
            filename.unlink(missing_ok=True)
