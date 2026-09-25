"""Identity, idempotency, and provenance keys for selective `:TurnEvidence` capture.

Split out of ``turn_evidence_repository.py`` (file-size refactor). The facade re-exports
``EVIDENCE_ROLES``, ``derive_turn_key``, and ``derive_prompt_hash`` so existing import
sites keep working unchanged.
"""

from __future__ import annotations

import hashlib

#: role values evidence may carry. Only 'user' is captured in the Claude MVP; the rest are reserved so
#: a later assistant/tool producer needs no schema change.
EVIDENCE_ROLES = ("user", "assistant", "tool", "agent")


def derive_turn_key(
    *, source_kind: str, session_id: str | None, text: str, cwd: str | None = None,
    prompt_id: str | None = None, namespace: str | None = None,
) -> str:
    """Deterministic idempotency key: the same turn hashes identically, so a double-fired hook merges
    onto one node instead of duplicating evidence.

    IDENTITY (G18): when the producer supplies a stable per-prompt id (`prompt_id` -- e.g. Claude Code's
    `prompt_id` UUID, which is UNIQUE per genuine turn yet STABLE across a double-fired retry of the same
    submission), the key is derived from IT. This is what lets two GENUINE repetitions of the same text
    ("I have 20 coins" said twice) stay DISTINCT evidence sources -- keying on the text alone collapses
    them into one, which breaks the per-observation / reinforcement model. When no `prompt_id` is
    available (older Claude Code < 2.1.196, or a producer that supplies none), the key falls back to the
    prompt TEXT (prior behavior), accepting that identical text collapses to a single source.

    The key is per-namespace because `turn_key` carries a GLOBAL uniqueness constraint, so two
    namespaces presenting the same turn must produce different keys or they collapse onto one node."""
    h = hashlib.sha256()
    identity = (prompt_id or "").strip() or (text or "")
    for part in (source_kind or "", session_id or "", cwd or "", identity, namespace or ""):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _namespace_scoped_key(base_key: str, namespace: str | None) -> str:
    """Bind a caller-supplied `turn_key` to the caller's namespace.

    `turn_key` arrives from the request body, so an unscoped one lets a caller in one
    namespace address a node in another. Derived keys already fold the namespace in;
    this covers the supplied path with the same guarantee.
    """
    h = hashlib.sha256()
    h.update((namespace or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((base_key or "").encode("utf-8", "replace"))
    return h.hexdigest()


def derive_prompt_hash(text: str) -> str:
    """Deterministic content fingerprint of the prompt (sha256 of the raw text).

    Provenance only: identifies WHICH prompt was captured without storing extra copies of it, and is
    stable across clients/sessions (unlike ``turn_key``, which folds in source/session/cwd for
    idempotency). Computed server-side so every stored node carries it regardless of the client."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
