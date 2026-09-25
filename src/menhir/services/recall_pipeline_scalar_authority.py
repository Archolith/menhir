"""Deterministic current-value scalar-state authority injection for recall (Phase 4a.4/4b)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from menhir.domain.models import FreshnessState, NodeScope
from menhir.domain.namespace import stamped_namespace
from menhir.domain.recall import RetrievalScoreKind, ScalarAuthorityVerdict
from menhir.domain.retrieval_tuning import CandidateSource
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.services.recall_policies import _authority_contributors

logger = logging.getLogger(__name__)


def _projection_is_recall_eligible(view: dict[str, Any]) -> bool:
    """Consume the repository's read-side eligibility decision without filtering inspection reads.

    The decision is fail-closed for production and test doubles alike: only explicit ``True`` may
    enter context. Missing, false, or malformed status is ineligible.
    """
    return view.get("recall_eligible") is True


async def _inject_scalar_authority(
    service: Any,
    query: str,
    namespace: str | None,
    candidate_inputs: list[dict[str, object]],
    metadata_by_uuid: dict[str, dict[str, object]],
    authority_layer: list[ScalarAuthorityVerdict],
    existing_uuids: set[str],
    obs_slots: set[tuple[str, str, str, str, str]],
    obs_subject_displays: dict[str, str],
    obs_added: int,
) -> tuple[int, Any]:
    """Inject as-of folds, expiry verdicts, and current scalar_state Views per surfaced slot."""
    from menhir.infrastructure.audit_trail import RECALL as _authority_audit
    # Phase 4a.4: for each SURFACED slot, DETERMINISTICALLY inject the current scalar_state
    # View by slot-keyed lookup (NOT embedding rank), so the authoritative CURRENT value
    # surfaces even when its View did not win ranking -- the G5 stale-value fix (the "how
    # many coins now" query returns 37, not the stale 20 the observation embedding matched).
    # A slot with no current View (abstained/expired -> current unknown by design) injects
    # nothing. Floor-exempt SCALAR_AUTHORITY source so the cosine floor never drops it.
    #
    # Phase 4b FOUNDATION GATE (G10/7.G): an injected View is always visible (additive), but
    # it is marked the CURRENT AUTHORITY (leads the answer) ONLY when its effective tier
    # rests on a SOURCE FOUNDATION (not `agent`-only probabilistic extraction, which
    # memory-governance.md forbids from self-authorizing). The effective tier is read from
    # the FOLD SSOT (current_authority) at as_of=now (G16 -- not the stamped snapshot; G19 --
    # not folding future in), cached per subject. Absent a foundation the View is injected as
    # ADVISORY (is_scalar_authority=False): the current value is still surfaced, just never
    # falsely presented as verified authority.
    from datetime import datetime, timezone
    from menhir.domain.scalar_view_authority import FOUNDATION_TIERS, QueryIntent
    from menhir.domain.scalar_view_suppression import authority_query_intent
    _now = datetime.now(timezone.utc)
    _authority_by_subject: dict[str, dict[tuple[str, str, str, str], str]] = {}
    # G13: an EXPIRED slot has NO current View by design; for a current-state (or bare) query
    # the recall must surface the EXPIRY VERDICT (last-known + date + current-UNKNOWN) so the
    # old observations never read as current. Historical/as-of intents (PREVIOUS_VALUE/
    # COMPARISON) are slice-B territory and are NOT expiry-verdicted here.
    _intent = authority_query_intent(query)
    # G13 slice B: an explicit "as of <date>" (COMPARISON) query leads with the deterministic
    # AS-OF FOLD at that date (absolutes + deltas folded up to <t>), NOT the current View and
    # NOT a raw observation ordering. History cues without a date (PREVIOUS_VALUE) carry no
    # resolvable timestamp, so they fall through to ordinary ranking (slice A already keeps the
    # expiry verdict off a history query). Only an explicit date is folded here.
    from menhir.domain.temporal_intent import classify_temporal_intent
    _as_of_str = classify_temporal_intent(query).as_of
    _as_of_dt = None
    if _intent == QueryIntent.COMPARISON and _as_of_str:
        try:
            _as_of_dt = datetime.strptime(_as_of_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            _as_of_dt = None
    _asof_states_by_subject: dict[str, dict[tuple[str, str, str, str], Any]] = {}

    def _asof_state_for(subject_uuid: str, slot: tuple[str, str, str, str]):
        if subject_uuid not in _asof_states_by_subject:
            try:
                svc = service.graph_adapter.scalar_state_service()
                res = svc.fold_entity(
                    subject_uuid, namespace=stamped_namespace(namespace), as_of=_as_of_dt)
                _asof_states_by_subject[subject_uuid] = {
                    (s.attribute, s.scope, s.value_kind, s.unit): s for s in res.states}
            except Exception:
                logger.exception("as-of fold failed subject=%r", subject_uuid)
                _asof_states_by_subject[subject_uuid] = {}
        return _asof_states_by_subject[subject_uuid].get(slot)

    _expiries_by_subject: dict[str, dict[tuple[str, str, str, str], Any]] = {}

    def _expiry_for(subject_uuid: str, slot: tuple[str, str, str, str]):
        if subject_uuid not in _expiries_by_subject:
            try:
                svc = service.graph_adapter.scalar_state_service()
                _expiries_by_subject[subject_uuid] = svc.current_expiries(
                    subject_uuid, namespace=stamped_namespace(namespace), as_of=_now)
            except Exception:
                logger.exception(
                    "expiry read failed subject=%r; no verdict", subject_uuid)
                _expiries_by_subject[subject_uuid] = {}
        return _expiries_by_subject[subject_uuid].get(slot)

    # G17 (4a.3): resolve the QUERY's subject INDEPENDENTLY of the injection provenance row,
    # so a View about subject B is never injected as authority for a query about subject A
    # (the cross-subject leak; Gate 2 was tautological because query/View/fact subjects all
    # came from one row). Two deterministic, I/O-free signals: (1) a first-person query
    # resolves the namespace's canonical self subject -- whose uuid is DETERMINISTIC,
    # uuid5("menhir-self:<ns>") (episode_lifecycle.ensure_self_entity), so no DB read; (2) a
    # named third party the query MENTIONS is rescued by matching a surfaced observation's
    # subject_display as a whole word in the query ("my dad's cars" -> the `dad` View is
    # allowed). When the query subject cannot be resolved (no first-person, no named match)
    # the set is empty and the gate is OPEN (today's behavior -- do not over-restrict).
    import re as _re
    from menhir.services.typed_scalar_perception import SELF_TOKENS
    _qlow = query.lower()
    _first_person = bool(_re.search(r"\b(i|me|my|mine|myself|we|us|our)\b", _qlow))
    query_subjects: set[str] = set()
    if _first_person:
        # the canonical self subject, via the identity SSOT: deterministic and I/O-free,
        # matching what perception binds self-subject assertions to -- no DB read.
        query_subjects.add(self_uuid_for_namespace(namespace))
    for _s_uuid, _s_disp in obs_subject_displays.items():
        _disp = (_s_disp or "").strip().lower()
        if not _disp:
            continue
        if _first_person and _disp in SELF_TOKENS:
            query_subjects.add(_s_uuid)                     # a self observation (display)
        elif _re.search(rf"\b{_re.escape(_disp)}\b", _qlow):
            query_subjects.add(_s_uuid)                     # a named third party in the query

    def _effective_tier(subject_uuid: str, slot: tuple[str, str, str, str]) -> str | None:
        if subject_uuid not in _authority_by_subject:
            try:
                svc = service.graph_adapter.scalar_state_service()
                _authority_by_subject[subject_uuid] = svc.current_authority(
                    subject_uuid, namespace=stamped_namespace(namespace), as_of=_now)
            except Exception:
                logger.exception(
                    "foundation-gate authority read failed subject=%r; advisory", subject_uuid)
                _authority_by_subject[subject_uuid] = {}
        return _authority_by_subject[subject_uuid].get(slot)

    async def _assertion_contributor_payload(
        assertion_ids: list[str], relations: dict[str, str]
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                service.graph_adapter.fetch_assertion_contributors,
                assertion_ids=assertion_ids, relations=relations, limit=8)
        except Exception:
            logger.exception("structured assertion provenance unavailable; returning head only")
            return {"contributors": [], "total": len(assertion_ids), "next_offset": None}

    async def _view_contributor_payload(view_uuid: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                service.graph_adapter.fetch_scalar_authority_contributors,
                view_uuid=view_uuid, limit=8, offset=0,
                namespace=stamped_namespace(namespace))
        except Exception:
            logger.exception("structured View provenance unavailable; returning head only")
            return {"contributors": [], "total": 0, "next_offset": None}

    auth_added = 0
    _authority_slots = obs_slots if service.scalar_view_authority_enabled else set()
    for (subj, attr, scp, vk, un) in _authority_slots:
        if not subj or not attr or not vk:
            continue
        # G17 subject safety: skip the authority injection for a subject the query is NOT
        # about (when the query subject could be resolved). The observation candidate itself
        # already surfaced additively; only the View-as-authority is subject-gated.
        if query_subjects and subj not in query_subjects:
            logger.debug(
                "scalar-authority injection skipped cross-subject subj=%r (query subjects=%r)",
                subj, query_subjects)
            continue
        if _intent == QueryIntent.COMPARISON and _as_of_dt is None:
            # The as-of date matched _AS_OF_RE (so intent is COMPARISON) but did not resolve
            # to a real calendar date -- e.g. "as of 2026-02-30". Injecting nothing is the
            # only safe answer: falling through would hand back the CURRENT View, which is
            # never the answer to "as of <date>", and the expiry verdict below is
            # current-state-only so it would not correct it either.
            continue
        if _intent == QueryIntent.COMPARISON and _as_of_dt is not None:
            # G13 slice B: lead with the AS-OF folded value at <date>; the current View is not
            # the answer to "as of <date>". Founds-gated like the current authority.
            st = _asof_state_for(subj, (attr, scp, vk, un))
            if st is not None:
                auuid = (f"scalar-asof:{stamped_namespace(namespace)}:{_as_of_str}:{subj}:"
                         f"{attr}:{scp}:{vk}:{un}")
                if auuid not in existing_uuids:
                    existing_uuids.add(auuid)
                    a_founded = False
                    try:
                        a_founded = await asyncio.to_thread(
                            service.graph_adapter.assertions_have_user_foundation,
                            assertion_ids=list(st.contributor_ids),
                            namespace=stamped_namespace(namespace))
                    except Exception:
                        logger.exception(
                            "as-of foundation read failed slot=%r; advisory", (subj, attr))
                    aname = (f"{st.subject_display or 'user'}'s {attr} as of {_as_of_str} = "
                             f"{st.value}.")
                    candidate_inputs.append({
                        "uuid": auuid, "name": aname, "content": aname,
                        "scope": str(NodeScope.PERSISTENT), "memory_type": "SCALAR_STATE",
                        "similarity": 1.0, "last_accessed_days_ago": 0.0, "edge_count": 0,
                        "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                        "conflict_status": None,
                        "source": CandidateSource.SCALAR_AUTHORITY,
                        "contributing_sources": frozenset({CandidateSource.SCALAR_AUTHORITY}),
                        "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
                        "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                        "content_cosine": None, "is_superseded_view": False,
                        "view_kind": "scalar_state",
                        "is_scalar_authority": a_founded,
                    })
                    metadata_by_uuid[auuid] = {
                        "name": aname, "content": aname,
                        "namespace": stamped_namespace(namespace),
                        "view_kind": "scalar_state", "view_current": True,
                        "scalar_asof": _as_of_str, "as_of_value": st.value,
                        "valid_at": st.valid_at, "has_foundation": a_founded,
                    }
                    relations = {st.anchor_id: "CURRENT_ANCHOR"}
                    relations.update({i: "CONTRIBUTED_TO"
                                      for i in st.contributed_delta_ids})
                    relations.update({i: "SUPERSEDED_ANCHOR"
                                      for i in st.superseded_anchor_ids})
                    contributor_payload = await _assertion_contributor_payload(
                        [
                            st.anchor_id, *st.contributed_delta_ids,
                            *st.superseded_anchor_ids,
                        ],
                        relations,
                    )
                    authority_layer.append(ScalarAuthorityVerdict(
                        kind="as_of", status="leads" if a_founded else "advisory",
                        subject_uuid=subj, attribute=attr, scope=scp,
                        value_kind=vk, unit=un, value=st.value,
                        valid_at=st.valid_at, view_uuid=None,
                        has_foundation=a_founded,
                        contributors=_authority_contributors(contributor_payload),
                        contributors_total=int(contributor_payload.get("total", 0)),
                        contributors_truncated=(
                            contributor_payload.get("next_offset") is not None),
                        next_offset=contributor_payload.get("next_offset"),
                    ))
                    _authority_audit.audit(
                        "authority_annotation", "leads" if a_founded else "advisory",
                        namespace=stamped_namespace(namespace), subject_uuid=subj,
                        slot=[attr, scp, vk, un],
                        details={"kind": "as_of", "as_of": _as_of_str,
                                 "value": str(st.value), "user_foundation": a_founded})
                    auth_added += 1
            continue
        view = await asyncio.to_thread(
            service.graph_adapter.fetch_current_scalar_view_for_slot,
            subject_uuid=subj, attribute=attr, scope=scp, value_kind=vk, unit=un,
            namespace=stamped_namespace(namespace),
        )
        if not view:
            # G13: no current View. If the slot EXPIRED (value ended, no replacement) and the
            # query is current-state / bare, inject the EXPIRY VERDICT so the old observations
            # are never read as current. It LEADS only on a user foundation (the "used to own"
            # was user-declared, via the expiry contributors' FOUNDS); else advisory.
            if _intent in (QueryIntent.CURRENT_STATE, QueryIntent.AMBIGUOUS):
                exp = _expiry_for(subj, (attr, scp, vk, un))
                if exp is not None:
                    euuid = (f"scalar-expiry:{stamped_namespace(namespace)}:{subj}:{attr}:"
                             f"{scp}:{vk}:{un}")
                    if euuid not in existing_uuids:
                        existing_uuids.add(euuid)
                        e_founded = False
                        try:
                            e_founded = await asyncio.to_thread(
                                service.graph_adapter.assertions_have_user_foundation,
                                assertion_ids=list(exp.contributor_ids),
                                namespace=stamped_namespace(namespace))
                        except Exception:
                            logger.exception(
                                "expiry foundation read failed slot=%r; advisory",
                                (subj, attr))
                        ename = (
                            f"{exp.subject_display or 'user'}'s {attr}: EXPIRED. last known "
                            f"{exp.expired_value} as of {exp.valid_at}; current {attr} UNKNOWN.")
                        candidate_inputs.append({
                            "uuid": euuid, "name": ename, "content": ename,
                            "scope": str(NodeScope.PERSISTENT), "memory_type": "SCALAR_STATE",
                            "similarity": 1.0, "last_accessed_days_ago": 0.0, "edge_count": 0,
                            "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                            "conflict_status": None,
                            "source": CandidateSource.SCALAR_AUTHORITY,
                            "contributing_sources": frozenset({CandidateSource.SCALAR_AUTHORITY}),
                            "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
                            "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                            "content_cosine": None, "is_superseded_view": False,
                            "view_kind": "scalar_state",
                            "is_scalar_authority": e_founded,
                        })
                        metadata_by_uuid[euuid] = {
                            "name": ename, "content": ename,
                            "namespace": stamped_namespace(namespace),
                            "view_kind": "scalar_state", "view_current": True,
                            "scalar_expiry": True, "expired_value": exp.expired_value,
                            "valid_at": exp.valid_at, "has_foundation": e_founded,
                        }
                        contributor_payload = await _assertion_contributor_payload(
                            list(exp.contributor_ids),
                            {i: "EXPIRY_INPUT" for i in exp.contributor_ids},
                        )
                        authority_layer.append(ScalarAuthorityVerdict(
                            kind="expired",
                            status="leads" if e_founded else "advisory",
                            subject_uuid=subj, attribute=attr, scope=scp,
                            value_kind=vk, unit=un, value=exp.expired_value,
                            valid_at=exp.valid_at, view_uuid=None,
                            has_foundation=e_founded,
                            contributors=_authority_contributors(contributor_payload),
                            contributors_total=int(contributor_payload.get("total", 0)),
                            contributors_truncated=(
                                contributor_payload.get("next_offset") is not None),
                            next_offset=contributor_payload.get("next_offset"),
                        ))
                        _authority_audit.audit(
                            "authority_annotation", "leads" if e_founded else "advisory",
                            namespace=stamped_namespace(namespace), subject_uuid=subj,
                            slot=[attr, scp, vk, un],
                            details={"kind": "expiry", "expired_value": str(exp.expired_value),
                                     "valid_at": exp.valid_at, "user_foundation": e_founded})
                        auth_added += 1
            continue
        if not _projection_is_recall_eligible(view):
            continue
        vuuid = str(view.get("uuid") or "").strip()
        if not vuuid:
            continue
        view_already_ranked = vuuid in existing_uuids
        if not view_already_ranked:
            existing_uuids.add(vuuid)
        vname = str(view.get("name") or f"{attr}: {view.get('value')}")
        tier = await asyncio.to_thread(_effective_tier, subj, (attr, scp, vk, un))
        # G14 slice 3 (10.G basis gate): the View leads on a SOURCE FOUNDATION, from EITHER
        # a trusted non-perception write path (effective tier not `agent`) OR -- the bridge
        # payoff -- the head's CURRENT_ANCHOR tracing to a declarant='user' :TurnEvidence
        # admission (FOUNDS edge). So an `agent`-tier extraction of an ADMITTED user statement
        # now leads in a Turn-capturing box, while `agent` extraction with no user admission
        # (Episodic fixtures) stays advisory. Additive: never weakens the 4b tier basis.
        tier_foundation = bool(tier) and tier in FOUNDATION_TIERS
        user_foundation = False
        if not tier_foundation:
            try:
                user_foundation = await asyncio.to_thread(
                    service.graph_adapter.scalar_view_has_user_foundation,
                    view_uuid=vuuid, namespace=stamped_namespace(namespace))
            except Exception:
                logger.exception(
                    "foundation-gate FOUNDS read failed view=%r; advisory", vuuid)
        has_foundation = tier_foundation or user_foundation
        authority_candidate = {
            "uuid": vuuid, "name": vname, "content": vname,
            "scope": str(NodeScope.PERSISTENT), "memory_type": "SCALAR_STATE",
            "similarity": 1.0, "last_accessed_days_ago": 0.0, "edge_count": 0,
            "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
            "conflict_status": None, "source": CandidateSource.SCALAR_AUTHORITY,
            "contributing_sources": frozenset({CandidateSource.SCALAR_AUTHORITY}),
            "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
            "bm25_rank": None, "cosine_rank": None, "content_rank": None,
            "content_cosine": None, "is_superseded_view": False,
            "view_kind": "scalar_state",
            # Phase 4b: lead ONLY on a foundation; else injected but advisory.
            "is_scalar_authority": has_foundation,
        }
        if view_already_ranked:
            # A View can win ordinary vector search before the deterministic slot lookup.
            # Do not let UUID dedup skip its authority annotation (the wake_time e2e case):
            # upgrade that same candidate in place and still emit the structured verdict.
            for existing in candidate_inputs:
                if str(existing.get("uuid") or "") == vuuid:
                    existing.update(authority_candidate)
                    break
        else:
            candidate_inputs.append(authority_candidate)
        metadata_by_uuid[vuuid] = {
            "name": vname, "content": vname, "namespace": view.get("namespace"),
            "view_kind": "scalar_state", "view_current": True,
            "ss_value": view.get("value"), "valid_at": view.get("valid_at"),
            "effective_tier": tier, "has_foundation": has_foundation,
            "tier_foundation": tier_foundation, "user_foundation": user_foundation,
        }
        contributor_payload = await _view_contributor_payload(vuuid)
        authority_layer.append(ScalarAuthorityVerdict(
            kind="current", status="leads" if has_foundation else "advisory",
            subject_uuid=subj, attribute=attr, scope=scp, value_kind=vk, unit=un,
            value=view.get("value"), valid_at=view.get("valid_at"), view_uuid=vuuid,
            has_foundation=has_foundation,
            contributors=_authority_contributors(contributor_payload),
            contributors_total=int(contributor_payload.get("total", 0)),
            contributors_truncated=(contributor_payload.get("next_offset") is not None),
            next_offset=contributor_payload.get("next_offset"),
        ))
        _authority_audit.audit(
            "authority_annotation", "leads" if has_foundation else "advisory",
            namespace=stamped_namespace(namespace), subject_uuid=subj,
            slot=[attr, scp, vk, un],
            details={"kind": "current", "value": str(view.get("value")),
                     "effective_tier": tier, "tier_foundation": tier_foundation,
                     "user_foundation": user_foundation})
        auth_added += 1
    logger.debug("scalar-authority injection query=%r added=%d", query[:60], auth_added)
    return auth_added, _intent
