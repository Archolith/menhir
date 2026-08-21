"""CF-131: a first-person subject must not fall back to lexical name-match when the canonical
self seam was consulted and failed.

`_make_self_seam`'s docstring already states the intent: "any ensure failure skips self-binding for
that namespace (the subject falls through to ordinary binding -> advisory)". It assumes ordinary
binding finds nothing. When extraction has minted a per-episode `user` :Entity -- which
`episode_lifecycle` records that it does -- ordinary binding finds it and binds the twin instead.

Why that is durable damage rather than a cosmetic mis-bind: the next successful
`ensure_self_entity` runs `_absorb_self_entity_forks`, which ends in `DETACH DELETE f`. The twin is
deleted and the assertion is orphaned onto a dead uuid. Recall meanwhile computes the canonical
`uuid5("menhir-self:<ns>")` with no database read, so it was already looking at the wrong node.

TWO CORRECTIONS TO THE REGISTER ENTRY, both load-bearing for the shape of this fix:

1. The entry says "`_resolve_subject:1797` binds lexically first". Line 1797 is inside the
   DOCSTRING, and that docstring says the opposite: "CANONICAL SELF first". The code tries the seam
   first and only then falls back to `_bind_from_candidates`. So this never bites when the seam
   works -- only when it is consulted and declines.

2. The entry's fix -- refuse `SELF_TOKENS` inside `_bind_from_candidates` -- is too broad. It
   refuses in two cases where nothing is at risk, and breaks 17 existing tests:

     * no seam wired at all -> no self machinery, so no `_absorb_self_entity_forks`, so the
       per-episode node is not doomed. `test_resolve_falls_through_when_no_seam` pins this as
       "ordinary name-match, unchanged".
     * seam wired but the namespace is BLANK -> `_make_self_seam` returns None WITHOUT touching
       the adapter, so again `ensure_self_entity` never runs and nothing absorbs anything.

The refusal therefore lives in `_resolve_subject`, gated on a non-blank namespace: exactly the case
where the MERGE was attempted, failed, and the self machinery is live.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.services.typed_scalar_rules import (
    SELF_TOKENS,
    _bind_from_candidates,
    _resolve_subject,
)

pytestmark = pytest.mark.unit

#: What extraction mints per episode, per episode_lifecycle's own recorded behavior.
EPISODE_USER_TWIN = [{"uuid": "per-episode-user-node-abc", "name": "user"}]


def _declining_seam(_ns: str | None) -> tuple[str, str] | None:
    """`_make_self_seam` after `ensure_self_entity` raised: the exception is swallowed, `uuid` is
    "", and the seam returns None."""
    return None


def _working_seam(uuid: str = "canonical-self-uuid"):
    def _seam(_ns: str | None) -> tuple[str, str]:
        return (uuid, "self")
    return _seam


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", sorted(SELF_TOKENS))
def test_a_declined_seam_refuses_the_lexical_twin_for_every_self_spelling(token: str) -> None:
    """The defect. Seam wired, real namespace, MERGE failed -> the twin must NOT be bound."""
    entities: list[dict[str, Any]] = [{"uuid": f"ep-{token}", "name": token}]

    uuid, display = _resolve_subject(token, entities, "ns", _declining_seam)

    assert (uuid, display) == (None, None), f"{token!r} bound to a doomed per-episode twin"


def test_the_twin_extraction_actually_mints_is_refused() -> None:
    """The concrete node from the entry, not a synthetic one."""
    assert _resolve_subject("user", EPISODE_USER_TWIN, "ns", _declining_seam) == (None, None)


# ---------------------------------------------------------------------------
# The two cases the entry's broader fix would have broken, where nothing is at risk
# ---------------------------------------------------------------------------


def test_no_seam_wired_still_falls_through_to_lexical_name_match() -> None:
    """No seam means no self machinery, so no `_absorb_self_entity_forks` and no DETACH DELETE.
    The node is not doomed and legacy binding stands -- as
    `test_resolve_falls_through_when_no_seam` in the self-binding suite pins."""
    uuid, display = _resolve_subject("user", EPISODE_USER_TWIN, "ns", None)

    assert uuid == "per-episode-user-node-abc"
    assert display == "user"


@pytest.mark.parametrize("blank", ["", None])
def test_a_blank_namespace_still_falls_through(blank: str | None) -> None:
    """`_make_self_seam` declines on a blank namespace WITHOUT calling the adapter, so
    `ensure_self_entity` never runs and nothing absorbs the twin. Refusing here would be refusing
    on a case that carries no risk."""
    uuid, _ = _resolve_subject("user", EPISODE_USER_TWIN, blank, _declining_seam)

    assert uuid == "per-episode-user-node-abc"


# ---------------------------------------------------------------------------
# Positive controls
# ---------------------------------------------------------------------------


def test_a_working_seam_still_returns_the_canonical_self() -> None:
    """The fix must refuse the LEXICAL path only. If it broke self binding itself, every test
    above would still pass."""
    uuid, display = _resolve_subject("user", EPISODE_USER_TWIN, "ns", _working_seam())

    assert uuid == "canonical-self-uuid"
    assert display == "self"


def test_the_seam_wins_even_though_a_lexical_match_exists() -> None:
    """Ordering: canonical self is preferred, never reached by lexical name-match."""
    uuid, _ = _resolve_subject("user", [{"uuid": "ent-x", "name": "user"}], "ns", _working_seam())

    assert uuid == "canonical-self-uuid"


def test_an_ordinary_named_subject_is_unaffected_by_a_declined_seam() -> None:
    """A third party never reaches the self branch at all (SELF_TOKENS is an exact allowlist), so a
    declining seam must not stop it binding. Without this, refusing everything would pass."""
    uuid, display = _resolve_subject("Alice", [{"uuid": "ent-a", "name": "Alice"}], "ns",
                                     _declining_seam)

    assert uuid == "ent-a"
    assert display == "Alice"


def test_the_variant_path_guard_in_bind_from_candidates_is_untouched() -> None:
    """`_bind_from_candidates` already refuses SELF_TOKENS among the fallback VARIANT spellings.
    That guard predates this fix and must survive it -- the fix adds a caller-level refusal, it
    does not move this one."""
    uuid, display = _bind_from_candidates("user's", EPISODE_USER_TWIN)

    assert (uuid, display) == (None, None)
