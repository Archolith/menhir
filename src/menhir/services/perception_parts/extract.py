"""The extraction pass: prose episodes -> typed event groups (the stochastic half of the boundary).

One call per sample turns numbered episodes into `PerceivedGroup`s by (subject, measure), peeling
stated totals (kind=assertion) into `stated_event` for triangulation / move-1 commits.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from menhir.domain.fold_algebra import Event, latest
from menhir.services.perception_parts.keys import sanitize_measure_key, sanitize_subject_name
from menhir.services.perception_parts.model import Episode, PerceivedGroup, _infer_reducer
from menhir.services.perception_parts.prompts import SYSTEM_PROMPT
from menhir.services.seam_types import LlmComplete

# ---------------------------------------------------------------------------- (1) extractor


def _parse_json_array(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    text = re.sub(r"(:\s*)\+(\d)", r"\1\2", text)  # LLMs emit "+1"; invalid JSON, strip the +
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def extract_once(episodes: list[Episode], llm_complete: LlmComplete) -> list[PerceivedGroup]:
    """One extraction pass: prose -> typed events grouped by (subject, measure). Each event is
    attributed to its source episode's uuid for provenance / the Law-2 replay ledger. Stated totals
    (kind=assertion) are peeled into `stated_event` (the LATEST is the live claim) — used to
    triangulate a fold, or, for a group with no fold events, to commit as the move-1 value directly."""
    if not episodes:
        return []
    log = "\n".join(f"[{i}] {e.content}" for i, e in enumerate(episodes))
    raw = _parse_json_array(llm_complete(SYSTEM_PROMPT, log))

    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"events": [], "kinds": [], "stated": []}
    )
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        subject = sanitize_subject_name(str(ev.get("subject") or "")) or "user"
        # CF-4/CF-5/CF-69: constrain the model-authored key here, at its origin, so the prompt
        # sites and the durable View property all inherit it. A label that is not a measure key
        # yields "" and the existing guard below drops the event.
        measure = sanitize_measure_key(str(ev.get("measure") or ""))
        kind = str(ev.get("kind") or "").strip().lower()
        when = str(ev.get("when") or "").strip()
        if not measure or not when:
            continue
        try:
            idx = int(ev.get("episode"))
            uuid = episodes[idx].uuid if 0 <= idx < len(episodes) else None
        except (TypeError, ValueError):
            uuid = None

        cell = grouped[(subject, measure)]
        if kind == "assertion":
            try:
                val = float(ev.get("value"))
            except (TypeError, ValueError):
                continue
            cell["stated"].append(Event(
                when=when, kind="assertion", value=val,
                what=str(ev.get("what")).strip() if ev.get("what") is not None else None,
                episode_uuid=uuid,
            ))
            continue

        value = None
        if ev.get("value") is not None:
            try:
                value = float(ev.get("value"))
            except (TypeError, ValueError):
                value = None
        identity = ev.get("identity")
        category = ev.get("category")
        cell["events"].append(
            Event(
                when=when,
                kind=kind or "occurrence",
                value=value,
                identity=str(identity).strip() if identity is not None else None,
                what=str(ev.get("what")).strip() if ev.get("what") is not None else None,
                episode_uuid=uuid,
                category=str(category).strip().lower() if category else None,
            )
        )
        cell["kinds"].append(kind)

    out: list[PerceivedGroup] = []
    for (subject, measure), cell in grouped.items():
        # LWW register over stated totals: the latest-dated assertion is the live claim.
        stated_event = latest(cell["stated"]) if cell["stated"] else None
        if not cell["events"] and stated_event is None:
            continue
        out.append(
            PerceivedGroup(
                subject=subject,
                measure=measure,
                # 'stated' = a pure move-1 group (the assertion IS the value; no events to fold).
                reducer=_infer_reducer(cell["kinds"]) if cell["events"] else "stated",
                events=cell["events"],
                stated_total=stated_event.value if stated_event is not None else None,
                stated_event=stated_event,
            )
        )
    return out + _category_spend_groups(out)


def _category_spend_groups(groups: list[PerceivedGroup]) -> list[PerceivedGroup]:
    """Lever C1 — derive `<category>_spend` SUM groups from per-item purchase events' `category` tags.
    The extractor tags each item locally (helmet→'biking'), a stable judgment; grouping is then
    DETERMINISTIC — sum all purchase events sharing a category. This makes a HETEROGENEOUS total
    (bike lights + helmet + chain → biking_spend) a first-class measure the gate can vote on, where
    the item name alone never groups. Per-item measures are left intact (independent facts); the
    category total is additive. Only categories with ≥2 distinct items are synthesized — a single
    categorized item is already its own measure (and would trip the count-floor as a SUM of one)."""
    by_cat: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for g in groups:
        for e in g.events:
            if e.kind in ("purchase", "spend") and e.value is not None and e.category:
                by_cat[(g.subject, e.category)].append(e)
    synth: list[PerceivedGroup] = []
    for (subject, category), events in by_cat.items():
        if len({e.identity or e.what for e in events}) < 2:
            continue  # a lone item is already its own measure; no category aggregation to add
        synth.append(PerceivedGroup(subject=subject, measure=f"{category}_spend",
                                    reducer="sum", events=events))
    return synth
