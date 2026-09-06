"""Keep active canonical-self release documents on the owner-approved contract."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_ID = "automatic-memory-v1"
_ACTIVE_CONTRACT_DOCS = (
    _ROOT / ".agent" / "plans" / "menhir-production-release-2026-09-04.md",
    _ROOT / ".agent" / "workflows" / "canonical-self-migration-runbook.md",
    _ROOT / ".agent" / "architecture.md",
)
_OBSOLETE_RELEASE_GUARANTEES = (
    "quoted, negated, questioned",
    "for every negative case require no canonical authority",
    "one negative quoted/reported-speech case that does not gain canonical authority",
    "a false canonical bind, especially quoted/reported speech",
)


def test_active_canonical_self_docs_share_the_automatic_memory_contract() -> None:
    missing = [
        path.relative_to(_ROOT).as_posix()
        for path in _ACTIVE_CONTRACT_DOCS
        if _CONTRACT_ID not in path.read_text(encoding="utf-8")
    ]
    assert missing == [], f"active canonical-self docs missing {_CONTRACT_ID}: {missing}"


def test_active_docs_do_not_restore_grammar_as_identity_authority() -> None:
    restored = {
        path.relative_to(_ROOT).as_posix(): [
            phrase
            for phrase in _OBSOLETE_RELEASE_GUARANTEES
            if phrase in path.read_text(encoding="utf-8").casefold()
        ]
        for path in _ACTIVE_CONTRACT_DOCS
    }
    restored = {path: phrases for path, phrases in restored.items() if phrases}
    assert restored == {}, f"obsolete semantic refusal guarantees restored: {restored}"

    plan = _ACTIVE_CONTRACT_DOCS[0].read_text(encoding="utf-8").casefold()
    assert "a model-attribution error in a question, negation, or reported-speech sample is not by itself" in plan


def test_historical_parser_contract_is_explicitly_superseded() -> None:
    changelog = (_ROOT / ".agent" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Superseded by `automatic-memory-v1` on 2026-09-06" in changelog
