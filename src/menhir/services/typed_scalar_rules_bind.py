"""Canonical-self identity and fail-closed subject binding for the typed-scalar boundary.

Self tokens/display, injected binding seams, subject spelling variants, and the unique
exact-name binder used by C.4.3. Moved verbatim from ``typed_scalar_rules``; the facade module
re-exports every name so existing import sites keep working unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

#: Exact first-person self tokens (C.4.3 canonical-self binding). The extraction PROMPT already
#: normalizes "I"/"me"/"my" to subject="user" (see the extract instruction), so "user" is the common
#: case; the rest are defensive. This is an EXACT allowlist, NEVER a prefix — a possessed object like
#: "my car" (subject="my car") and every NAMED third party fall through to ordinary entity binding and
#: can therefore never bind to self.
#: DOMAIN: scalar-proposal ``subject_text``. Deliberately NARROWER than the extraction-time set in
#: ``graphiti_extraction_patches`` (which adds "my"/"mine"): those are plausible entity NAMES but not
#: plausible SUBJECTS -- a proposal with subject "my" is malformed, and admitting it would bind
#: garbage to the human. Do not "unify" these three sets; they answer different questions over
#: different inputs. See ``domain/self_identity.SELF_ALIASES``.
SELF_TOKENS = frozenset({"user", "the user", "i", "me", "myself"})

#: Canonical display for a self-bound subject. Matches the extraction prompt's subject convention so a
#: materialized View reads naturally ("user's coins: 37"); the authority is the bound self UUID.
SELF_SUBJECT_DISPLAY = "user"

#: A seam that maps a namespace to its ONE canonical self subject: returns (subject_uuid, display) for
#: the namespace's stable self :Entity (ensuring it exists), or None when self-binding is unavailable
#: (e.g. no namespace). Injected so the pure binders stay offline-testable.
ResolveSelfSubject = Callable[["str | None"], "tuple[str, str] | None"]


def _is_self_reference(subject_text: str) -> bool:
    """True when `subject_text` denotes the first-person self (the namespace's canonical user)."""
    return subject_text.strip().lower() in SELF_TOKENS


#: Leading determiners the perceiver sometimes keeps on an extracted subject where the resolved
#: entity does not carry them ("my boots" against an entity named "boots"). Stripped only as a
#: FALLBACK spelling of the query, never on the first attempt, and never enough on its own to make a
#: match non-unique -- see `_bind_subject`.
_SUBJECT_DETERMINERS = ("my ", "your ", "our ", "his ", "her ", "their ", "its ", "the ")
#: The POSSESSIVE subset of `_SUBJECT_DETERMINERS`. Incidental ``new`` is stripped ONLY after one of
#: these ("my new black shoes" -> "black shoes"); the plain article "the" never triggers a ``new``
#: strip ("the new house" keeps "new house"), because "the <new> <name>" is far more often a proper
#: name than a possessed object, and collapsing it risks the same referent-ambiguity the fail-closed
#: binder exists to refuse.
_POSSESSIVE_DETERMINERS = ("my ", "your ", "our ", "his ", "her ", "their ", "its ")


#: A seam that resolves a subject against entities OUTSIDE the current episode — the same-namespace
#: repository — by a bounded list of normalized subject spellings. Injected so the pure binder stays
#: offline-testable. Returns candidate `{uuid, name}` ROWS (the repository already enforces
#: same-namespace/group_id, nonblank uuid, and excludes derived Views); the caller applies the SAME
#: unique fail-closed binder to them. None/absent means the fallback is disabled (local-only binding).
LookupNamespaceEntities = Callable[["str | None", "list[str]"], "list[dict[str, Any]]"]


def _subject_variants(target: str) -> list[str]:
    """Fallback spellings of an extracted subject, in priority order, excluding `target` itself.

    Two syntactic normalizations only, both of which rewrite HOW the same subject is written rather
    than WHICH subject is meant:
      * drop a leading determiner ("my boots" -> "boots")
      * a CONSTRAINED POSSESSIVE + incidental ``new``: only when a leading POSSESSIVE determiner was
        dropped ("my new black shoes") does a following ``new`` become optional, yielding "new black
        shoes" then "black shoes". The ``new``-keeping spelling is always emitted BEFORE the
        ``new``-stripped one, ``new`` is NEVER stripped from the bare target (an arbitrary name like
        "New York" with no determiner is untouched), and it is NOT stripped after the plain article
        "the new house" (which stays "new house") — the article case is far more often a proper name.
      * treat `_`/`-` as word separators ("shift_schedule" -> "shift schedule"), which is the
        perceiver emitting a slot key where a surface form belongs

    Deliberately NOT stemming, synonyms, or substring matching: those change the referent and would
    reintroduce exactly the ambiguity the fail-closed rule exists to refuse."""
    seen: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate != target and candidate not in seen:
            seen.append(candidate)

    stripped = target
    for determiner in _SUBJECT_DETERMINERS:
        if target.startswith(determiner):
            stripped = target[len(determiner):].strip()
            _add(stripped)
            # Constrained possessive + incidental "new": only after a POSSESSIVE determiner was
            # dropped, so "the new house" keeps "new house" and a bare "New York" is untouched.
            if determiner in _POSSESSIVE_DETERMINERS and stripped.startswith("new "):
                _add(stripped[len("new "):])
            break
    for base in (target, stripped):
        if "_" in base or "-" in base:
            _add(base.replace("_", " ").replace("-", " "))
    return seen


def _subject_spellings(subject_text: str) -> list[str]:
    """The bounded, priority-ordered list of NORMALIZED spellings used for an exact repository lookup:
    the target itself first, then its syntactic variants, deduplicated. No fuzzy/substring/stem/
    synonyms — every spelling is an exact normalized name form, and the binder still requires a unique
    match."""
    target = subject_text.strip().lower()
    if not target:
        return []
    out = [target]
    for variant in _subject_variants(target):
        if variant not in out:
            out.append(variant)
    return out


def _bind_from_candidates(
    subject_text: str, candidates: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """The UNIQUE fail-closed binder over a candidate row set: match each normalized spelling, in
    priority order, by exact name equality; bind on a single match with a nonblank uuid; abstain
    (None, None) on zero, multiple, or a blank-uuid match. `candidates` is either the episode's linked
    entities or the result of a same-namespace repository lookup — both are fed through this SAME
    binder so authority never depends on which candidate source won."""
    target = subject_text.strip().lower()
    if not target:
        return None, None

    def _unique(candidate: str) -> tuple[str | None, str | None]:
        matches = [
            e for e in candidates
            if str(e.get("name", "") or "").strip().lower() == candidate
        ]
        if len(matches) != 1:
            return None, None
        uuid = str(matches[0].get("uuid") or "").strip()
        if not uuid:
            return None, None
        return uuid, (str(matches[0].get("name") or "").strip() or subject_text)

    uuid, display = _unique(target)
    if uuid is not None:
        return uuid, display

    for variant in _subject_variants(target):
        if variant in SELF_TOKENS:
            continue
        uuid, display = _unique(variant)
        if uuid is not None:
            return uuid, display
    return None, None


def _bind_subject(
    subject_text: str, entities: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Bind `subject_text` to a UNIQUE surviving entity by exact normalized-name equality. Returns
    (subject_uuid, subject_display) on a single match; (None, None) on zero OR multiple (abstain from
    authority) OR a match whose uuid is blank. Deliberately NOT fuzzy: asserting authority on an
    ambiguous or approximate identity is precisely what the fail-closed binding rule forbids.

    The exact spelling is tried FIRST and always wins, so this is behaviour-preserving for every
    subject that already bound. Only when it matches nothing do syntactic variants
    (`_subject_variants`) get a turn, and each variant must ITSELF produce exactly one match --
    ambiguity in a fallback abstains just as it does in the primary. A variant that resolves to a
    first-person token is refused outright: self binds through the canonical-self seam and MUST NOT
    be reachable by lexical name-match, or a per-episode `user` node could become the self subject."""
    return _bind_from_candidates(subject_text, entities)


def _resolve_subject(
    subject_text: str, entities: list[dict[str, Any]], namespace: str | None,
    resolve_self_subject: ResolveSelfSubject | None,
    lookup_namespace_entities: LookupNamespaceEntities | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a subject to (subject_uuid, subject_display), preferring the canonical self identity.

    Ordering is deliberately conservative and additive:
      1. CANONICAL SELF first: a first-person subject binds to the namespace's ONE stable self entity
         via the injected `resolve_self_subject` seam — never to a per-episode node and never through
         lexical name-match, so replay is idempotent and no `I`/`user` entity is ever minted per
         episode. Named third parties never reach this branch (SELF_TOKENS is an exact allowlist).
      2. EXACT LOCAL EPISODE matching next: bind against the episode's SURVIVING linked entities by
         exact normalized-name equality (`_bind_from_candidates`). Authority is asserted ONLY on a
         unique local match.
      3. OPTIONAL same-namespace repository lookup, ONLY after the local pass fails: when
         `lookup_namespace_entities` is supplied AND `namespace` is nonblank, ask the repository for
         same-namespace entities matching the bounded normalized spellings, then feed those rows
         through the SAME unique fail-closed binder. No fuzzy/substring/stem/synonyms anywhere.

    When the self seam is absent, unavailable, or the subject is not first-person, and neither the
    local nor (when enabled) namespace pass yields a unique match, binding abstains to (None, None) —
    the caller records an advisory, never an unverified owner."""
    if resolve_self_subject is not None and _is_self_reference(subject_text):
        bound = resolve_self_subject(namespace)
        if bound is not None and bound[0]:
            return bound
        # CF-131: the seam was CONSULTED for a real namespace and failed (ensure_self_entity
        # raised and was swallowed). Refuse the lexical fallback -- self binds through the
        # canonical seam or not at all.
        #
        # Scoped to `namespace` truthiness on purpose, because that is exactly the condition
        # under which the fallback is dangerous. `_make_self_seam` returns None in two shapes:
        #
        #   ns is blank    -> it declines WITHOUT touching the adapter. ensure_self_entity never
        #                     runs, so _absorb_self_entity_forks never runs for that namespace,
        #                     so a per-episode `user` node is NOT doomed. Legacy lexical binding
        #                     stays, which is what test_resolve_falls_through_when_no_seam and
        #                     the coordinator tests pin.
        #   ns is present  -> the MERGE was attempted and failed. The self machinery IS live here,
        #                     so the next successful ensure_self_entity runs
        #                     _absorb_self_entity_forks, which ends in DETACH DELETE and orphans
        #                     any assertion bound to the twin onto a dead uuid. Refuse.
        if namespace:
            return None, None
    local = _bind_from_candidates(subject_text, entities)
    if local[0] is not None:
        return local
    if lookup_namespace_entities is not None and namespace:
        try:
            rows = lookup_namespace_entities(namespace, _subject_spellings(subject_text))
        except Exception:  # noqa: BLE001 - the repository fallback is best-effort; never crash binding
            rows = []
        namespace_match = _bind_from_candidates(subject_text, rows or [])
        if namespace_match[0] is not None:
            return namespace_match
    return None, None
