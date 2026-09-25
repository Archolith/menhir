"""Concrete ViewKind implementations and their per-kind formatting, split from ``view_models.py``.

Holds the counter, timeline (legacy subject-only and event-lane), and admission-audit kinds
together with the retrieval-surface, signature, and normalization helpers only those kinds use.
Every symbol here is re-exported from ``view_models.py``, so the original import path keeps
working unchanged.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from collections.abc import Mapping
from datetime import timezone
from typing import Any

from menhir.domain.temporal import parse_iso8601
from menhir.infrastructure.view_models_base import ViewAudience, ViewKind, _COUNT_TOKEN


class CounterKind(ViewKind):
    """kind='counter' (QuantState): a scalar (subject, counter) -> value, supersedable by value."""

    name = "counter"
    lww_register = True  # a current-total register: newer valid_at wins (fold-algebra Law 1)
    read_fields = ("n.uuid AS uuid, n.view_subject AS subject, n.qs_counter AS counter, "
                   "n.view_value AS value, toString(n.valid_at) AS valid_at")

    def audience(self, payload: dict[str, Any]) -> ViewAudience:
        return ViewAudience.RECALL

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        return str(payload["counter"])

    def signature(self, payload: dict[str, Any]) -> str:
        return _fmt(float(payload["value"]))

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        counter = str(payload["counter"]); value = float(payload["value"])
        n_eps = len(payload.get("episode_uuids") or [])
        name = _counter_retrieval_text(subject, counter, value)
        return name, _counter_summary(subject, payload, n_eps)

    def summary_template(self, subject: str, payload: dict[str, Any]) -> str | None:
        return _counter_summary(subject, payload, _COUNT_TOKEN)

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        counter = str(payload["counter"]); value = float(payload["value"])
        return {
            "view_value": value,
            # back-compat mirror: pre-View readers/queries keyed on is_quantstate/qs_* keep working
            "is_quantstate": True, "qs_key": key, "qs_subject": subject.strip(),
            "qs_counter": counter.strip(), "qs_value": value, "qs_current": True,
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"uuid": row.get("uuid"), "subject": row.get("subject"),
                "counter": row.get("counter"), "value": row.get("value"),
                "valid_at": row.get("valid_at")}


class TimelineKind(ViewKind):
    """kind='timeline': an ordered event list for a subject, supersedable by the set of events.
    Entries are normalized ONCE by the wrapper (`record_timeline`) before reaching these methods.

    Two modes share the SAME kind and node shape, distinguished only by the payload:
      - LEGACY subject-only mode (no `predicate`): value is an ordered list of `{when, what,
        episode_uuid}` — `key_discriminator` == 'timeline', ordered (when, what) signature, the
        subject-only surface/render, and `parse` returns exactly the existing public keys.
      - EVENT-LANE mode (`predicate` nonblank, optional `domain`): the value is an ordered list of
        the fixed query-sufficient event-entry schema. The lane discriminator is a collision-safe
        `timeline:event:` key, the signature covers predicate/domain and the full normalized
        entries, the surface/render uses occurrence/history language, and `parse` additionally
        projects `predicate`/`domain` only when a predicate is present.
    Event entries are normalized by the dedicated private normalizer (`_normalize_event_entries`);
    the legacy `_normalize_entries` is never reused for them."""

    name = "timeline"
    read_fields = ("n.uuid AS uuid, n.view_subject AS subject, n.view_value AS count, "
                   "n.view_payload AS payload, n.view_predicate AS predicate, "
                   "n.view_domain AS domain, toString(n.valid_at) AS valid_at")

    def subtype(self, payload: dict[str, Any]) -> str:
        return "event_timeline" if _event_mode(payload) else "legacy_timeline"

    def audience(self, payload: dict[str, Any]) -> ViewAudience:
        return ViewAudience.RECALL if _event_mode(payload) else ViewAudience.OPERATOR

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        if _event_mode(payload):
            return _event_lane_suffix(payload.get("predicate"), payload.get("domain"))
        return "timeline"

    def signature(self, payload: dict[str, Any]) -> str:
        if _event_mode(payload):
            return _event_sig(payload.get("predicate"), payload.get("domain"), payload["entries"])
        return _timeline_sig(payload["entries"])

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        entries = payload["entries"]
        if _event_mode(payload):
            pred = payload.get("predicate")
            dom = payload.get("domain")
            return _event_surface(subject, pred, dom, entries), \
                _render_event_timeline(subject, pred, dom, entries)
        return _timeline_surface(subject, entries), _render_timeline(subject, entries)

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload["entries"]
        props = {"view_value": float(len(entries)),
                 "view_payload": json.dumps(entries, ensure_ascii=False)}
        if _event_mode(payload):
            # Lane stamp: predicate always; domain only when present (None is dropped so legacy
            # subject-only write_props stays byte-identical).
            props["view_predicate"] = str(payload.get("predicate") or "").strip()
            dom = str(payload.get("domain") or "").strip()
            if dom:
                props["view_domain"] = dom
        return props

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {"uuid": row.get("uuid"), "subject": row.get("subject"),
               "count": row.get("count"), "valid_at": row.get("valid_at")}
        out["entries"] = json.loads(row.get("payload") or "[]")
        # Event rows project predicate/domain; legacy rows (no predicate) keep the exact shape.
        if row.get("predicate"):
            out["predicate"] = row.get("predicate")
            out["domain"] = row.get("domain")
        return out

    def episode_uuids(self, payload: dict[str, Any]) -> list[str]:
        return [str(e["episode_uuid"]) for e in payload["entries"] if e.get("episode_uuid")]

    def valid_at(self, payload: dict[str, Any]) -> str | None:
        entries = payload["entries"]
        return entries[-1]["when"] if entries else None


class AdmissionAuditKind(ViewKind):
    """kind='admission_audit': a record of an admission verdict on a user-tier claim.
    Payload: {requested_source, effective_source, granted, turn_evidence_uuid, reason}.
    Non-idempotent: every verdict creates a new row (always supersedes prior).
    """

    name = "admission_audit"
    lww_register = False  # Every audit entry is a separate event, not a value register.
    read_fields = (
        "n.uuid AS uuid, n.view_subject AS subject, n.view_value AS granted, "
        "n.requested_source AS requested_source, n.effective_source AS effective_source, "
        "n.reason AS reason, n.turn_evidence_uuid AS turn_evidence_uuid, "
        "toString(n.valid_at) AS valid_at"
    )

    def key_discriminator(self, payload: dict[str, Any]) -> str:
        return "admission_audit"

    def signature(self, payload: dict[str, Any]) -> str:
        # Non-idempotent: every verdict is unique (timestamp-based), so hash all audit fields.
        import hashlib
        basis = "|".join(str(v) for v in [
            payload.get("requested_source", ""),
            payload.get("effective_source", ""),
            payload.get("granted", False),
            payload.get("turn_evidence_uuid", ""),
            payload.get("reason", ""),
        ])
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        granted = bool(payload.get("granted", False))
        requested = str(payload.get("requested_source", ""))
        effective = str(payload.get("effective_source", ""))
        reason = str(payload.get("reason", ""))
        status = "granted" if granted else "denied"
        name = f"Admission {status}: {subject} claimed {requested}"
        summary = (
            f"Admission verdict for {subject}: requested {requested} tier, "
            f"effective {effective} ({status}). Reason: {reason}"
        )
        return name, summary

    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "view_value": float(bool(payload.get("granted", False))),
            "requested_source": str(payload.get("requested_source", "")),
            "effective_source": str(payload.get("effective_source", "")),
            "reason": str(payload.get("reason", "")),
            "turn_evidence_uuid": (
                str(payload["turn_evidence_uuid"]) if payload.get("turn_evidence_uuid") else None
            ),
        }

    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "uuid": row.get("uuid"),
            "subject": row.get("subject"),
            "granted": bool(row.get("granted")),
            "requested_source": row.get("requested_source"),
            "effective_source": row.get("effective_source"),
            "reason": row.get("reason"),
            "turn_evidence_uuid": row.get("turn_evidence_uuid"),
            "valid_at": row.get("valid_at"),
        }

    # NO episode_uuids override, deliberately. `turn_evidence_uuid` stays an audit PROPERTY
    # (write_props/parse above) and is not declared as a contributor receipt.
    #
    # The admission audit is written into the "agent-status" telemetry silo while its grounding
    # TurnEvidence lives in the USER namespace, so the contributor is cross-tenant by design.
    # Declaring it made every audit write unsatisfiable: the shared FACT writer resolves evidence
    # under `tenant_scope_cypher`, scoped to the VIEW's namespace, so the contributor resolved to
    # zero candidates and the whole write was refused ("must resolve to live evidence") -- the audit
    # row was lost entirely, silently, because the call site swallows it at DEBUG.
    #
    # The MENTIONS edge it was trying to create only serves `view_live_provenance_cypher`, and that
    # predicate is applied ONLY to `view_audience = 'RECALL'` views. This kind stamps OPERATOR, so
    # the edge bought nothing here -- and could never have been satisfied anyway, since that same
    # predicate requires `evidence_tenant = view_tenant`.


def _fmt(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else str(v)


def _day(when: str) -> str:
    return str(when)[:10]


def _counter_summary(subject: str, payload: dict[str, Any], n_events: Any) -> str:
    """The counter's human-readable body. `n_events` is either the real supporting-event count or
    `_COUNT_TOKEN` (rendering a template Cypher fills in) — one formatter, so the summary written on
    the CREATE path and the one rewritten by the provenance refresh can never drift apart."""
    counter = str(payload["counter"]); value = float(payload["value"])
    valid_at = str(payload.get("valid_at") or "")
    return (f"{subject.strip()} — {counter.strip()} = {_fmt(value)} "
            f"(current as of {valid_at[:10]}; {n_events} supporting event(s))")


def _counter_retrieval_text(subject: str, counter: str, value: float) -> str:
    """Counter BM25/embedding surface. LEADS with a natural, answer-readable statement, THEN keeps
    the 'how many / how much' keywords for retrieval. The old lead — 'how many times {subject}
    {counter}: {value}' — read as telemetry: an answer A/B showed it ranked #1 yet the answer model
    could not use it ('how many times user playlists: 20' does not read as 'the user has 20
    playlists'), so a correct, top-ranked View still produced 'I don't know'. Leading with
    '{subject}'s {counter}: {value}' fixes readability without losing the query match."""
    human = counter.strip().replace("_", " ")
    v = _fmt(value)
    subj = subject.strip()
    return (f"{subj}'s {human}: {v}. {human} = {v} "
            f"(how many / how much {human}: {v}; {human} count is {v}).")


def _timeline_surface(subject: str, entries: list[dict[str, Any]]) -> str:
    """Timeline BM25/embedding surface — lexicalises the chronology so 'when did X …' and
    'what happened with X over time' queries match. Leads with subject + span, then events."""
    subj = subject.strip()
    head = f"timeline of {subj}: {len(entries)} event(s)"
    if entries:
        span = f" from {_day(entries[0]['when'])} to {_day(entries[-1]['when'])}"
        body = "; ".join(f"{_day(e['when'])} {str(e.get('what', '')).strip()}" for e in entries)
        return f"{head}{span} — {body}"
    return head


def _normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce + sort entries ascending by `when`. Keeps only when/what/episode_uuid; drops
    entries without a `when`. Deterministic order is what makes the signature stable."""
    out: list[dict[str, Any]] = []
    for e in entries or []:
        when = e.get("when")
        if not when:
            continue
        out.append({"when": str(when), "what": str(e.get("what", "")).strip(),
                    "episode_uuid": (str(e["episode_uuid"]) if e.get("episode_uuid") else None)})
    out.sort(key=lambda x: (x["when"], x["what"]))
    return out


def _timeline_sig(entries: list[dict[str, Any]]) -> str:
    """Idempotency signature: stable hash of the ordered (when, what) pairs. Adding/removing/
    editing an event changes it -> supersede; re-running on the same events -> no-op."""
    basis = "|".join(f"{e['when']}~{e['what']}" for e in entries)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------
# Event-lane mode: predicate/domain scoped timeline entries. Pure + dependency-light. The legacy
# subject-only helpers above are left untouched; this slice adds its own normalizer and surface.
# ---------------------------------------------------------------------------------------------

#: The fixed, query-sufficient schema for one event-lane entry. Everything else is stripped.
_EVENT_ENTRY_ALLOWLIST = (
    "assertion_key", "source_key", "when", "what", "object_key", "object_uuid",
    "quote", "episode_uuid", "turn_evidence_uuid", "time_basis", "evidence_tier",
)

#: Fields that must be present and nonblank for an event entry to be query-sufficient.
_EVENT_ENTRY_REQUIRED = (
    "assertion_key", "source_key", "when", "what", "object_key", "quote", "episode_uuid",
)


def _event_mode(payload: dict[str, Any]) -> bool:
    """Whether the timeline payload is in event-lane mode. Event mode is explicit via a nonblank
    `predicate`; `domain` is optional. Domain WITHOUT predicate must fail closed (a lane is
    predicate-scoped, so a domain with no predicate would silently over-merge lanes)."""
    predicate = (payload.get("predicate") or "").strip()
    domain = (payload.get("domain") or "").strip()
    if not predicate and domain:
        raise ValueError(
            "event timeline mode requires a non-blank predicate when domain is present"
        )
    return bool(predicate)


def _event_lane_suffix(predicate: Any, domain: Any) -> str:
    """Deterministic, collision-safe discriminator for one predicate/domain event lane.

    Encodes the normalized (lowercased, trimmed) predicate and optional domain after a
    `timeline:event:` prefix using a length-prefixed, percent-encoded scheme. Percent-encoding
    removes every delimiter-ambiguous byte (colons, '%', '/', ...) from the segments, and the
    leading decimal length disambiguates the segment boundary even in principle — so raw colon
    concatenation collisions (e.g. predicate 'a' + domain 'b:c' vs predicate 'a:b' + domain 'c')
    cannot occur."""
    pred = (predicate or "").strip().lower()
    if not pred:
        raise ValueError("event timeline lane requires a non-blank predicate")
    dom = (domain or "").strip().lower()

    def _seg(text: str) -> str:
        enc = urllib.parse.quote(text, safe="")
        return f"{len(enc)}:{enc}"

    suffix = f"timeline:event:{_seg(pred)}"
    if dom:
        suffix += f":{_seg(dom)}"
    return suffix


def _event_sig(predicate: Any, domain: Any, entries: list[dict[str, Any]]) -> str:
    """Event-lane idempotency signature: covers predicate/domain AND the full normalized entry
    payload. Any change to a quote, object, provenance, source time, or the lane itself yields a
    new projection version; the legacy subject-only `_timeline_sig` is unchanged for legacy rows."""
    pred = (predicate or "").strip().lower()
    dom = (domain or "").strip().lower()
    basis = json.dumps(
        {"predicate": pred, "domain": dom, "entries": entries},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _normalize_event_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedicated event-lane normalizer (never the legacy `_normalize_entries`).

    For each entry: validates the required nonblank fields, parses `when` through
    `menhir.domain.temporal.parse_iso8601` (invalid/unparseable world time -> the entry is excluded
    from this disposable View; ingest time is never used), canonicalizes accepted times to UTC ISO
    with 'Z', and keeps ONLY the fixed allowlist. Exact replays (same `assertion_key`) are
    deduplicated deterministically and the result is sorted ascending by parsed world time, then
    `assertion_key` — independent of input order."""
    def _get(name: str, default: Any = None) -> Any:
        return getattr(e, name, default)

    out: list[dict[str, Any]] = []
    for e in entries or []:
        if isinstance(e, Mapping):
            get = e.get
        else:
            get = _get
        missing = [f for f in _EVENT_ENTRY_REQUIRED
                   if not str(get(f, "") or "").strip()]
        if missing:
            raise ValueError(
                f"event timeline entry missing required field(s): {', '.join(missing)}"
            )
        when_dt = parse_iso8601(get("when"))
        if when_dt is None:
            continue  # invalid/unparseable world time -> excluded (auditable later, not here)
        norm: dict[str, Any] = {
            "when": when_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for f in _EVENT_ENTRY_ALLOWLIST:
            if f == "when":
                continue
            v = get(f)
            if f in ("object_uuid", "turn_evidence_uuid"):
                norm[f] = str(v) if v else None
            elif isinstance(v, str):
                norm[f] = v.strip()
            else:
                norm[f] = v
        out.append(norm)
    # Representative selection must be TOTAL and input-order independent. Exact replays share an
    # assertion_key (and therefore, by construction, `when`), but can still differ in quote/
    # metadata — so sorting on (when, assertion_key) alone is a partial order whose tie is resolved
    # by Python's stable sort = INPUT order. Break the tie with the canonical JSON of the FULL
    # normalized entry, then keep the first (minimum) representative per assertion_key.
    out.sort(key=lambda x: (
        x["when"], x["assertion_key"],
        json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    ))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in out:
        if entry["assertion_key"] in seen:
            continue
        seen.add(entry["assertion_key"])
        deduped.append(entry)
    # Final ordering is (world time, assertion_key) regardless of the representative tie-break.
    deduped.sort(key=lambda x: (x["when"], x["assertion_key"]))
    return deduped


def _event_surface(subject: str, predicate: Any, domain: Any,
                   entries: list[dict[str, Any]]) -> str:
    """Event-lane BM25/embedding surface. Uses occurrence/history language (never current-state or
    ownership/supersession) and makes the predicate, optional domain, objects, source dates, and
    quotes readable so 'when did X <predicate>' queries match."""
    subj = subject.strip()
    pred = (predicate or "").strip()
    dom = (domain or "").strip()
    dom_txt = f" in domain {dom}" if dom else ""
    head = f"history of {subj}: {pred}{dom_txt} ({len(entries)} recorded occurrence(s))"
    if not entries:
        return head
    parts: list[str] = []
    for e in entries:
        obj = str(e.get("object_key", "")).strip()
        when = _day(e["when"])
        if obj:
            parts.append(f"{when}: {e['what']} ({obj})")
        else:
            parts.append(f"{when}: {e['what']}")
    return f"{head} — " + "; ".join(parts)


def _render_event_timeline(subject: str, predicate: Any, domain: Any,
                           entries: list[dict[str, Any]]) -> str:
    """Event-lane human-readable body. Occurs/history wording only; exposes predicate, optional
    domain, objects, source dates, and quotes without claiming current ownership/supersession."""
    subj = subject.strip()
    pred = (predicate or "").strip()
    dom = (domain or "").strip()
    dom_txt = f", domain: {dom}" if dom else ""
    lines = [f"Occurrence history — {subj}: {pred}{dom_txt} ({len(entries)} occurrence(s)):"]
    for e in entries:
        when = _day(e["when"])
        obj = str(e.get("object_key", "")).strip()
        src = str(e.get("source_key", "")).strip()
        q = str(e.get("quote", "")).strip()
        if len(q) > 60:
            q = q[:57] + "..."
        bits = []
        if obj:
            bits.append(f"object {obj}")
        if src:
            bits.append(f"source {src}")
        if q:
            bits.append(f"\"{q}\"")
        line = f"  {when}: {e['what']}"
        lines.append(line + (f" — {', '.join(bits)}" if bits else ""))
    return "\n".join(lines)


def _render_timeline(subject: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"Timeline — {subject.strip()} ({len(entries)} event(s)):"]
    lines += [f"  {_day(e['when'])}: {e['what']}" for e in entries]
    return "\n".join(lines)
