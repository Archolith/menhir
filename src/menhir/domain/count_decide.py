"""Related-count matching, decide step (counting plan unit 4R, PR 2): pure prompt, parse, vote and
code rules. The runner does the I/O.

One decide call per page covers every candidate that has related counts; it is sampled k times and a
candidate's (decision, target) must win `threshold` of the samples. The model only ever sees short
handles (N1, R1), never assertion ids. Code rules then decide what may be stored:
- a target must be in the candidate's own related set and the same subject, resolved to its root;
- a target that is not counted (or resolves to nothing counted) leaves the candidate uncertain;
- a truncated candidate is never `counted`;
- a repeat of a LATER-dated counted row swaps: the earlier one is counted and the later one becomes
  the repeat, written as ONE swap so the happening never counts twice if either write fails.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from menhir.domain.count_match import (
    COUNTED,
    TARGETED_STATUSES,
    UNCERTAIN,
    RelatedSet,
    normalize_family,
    relation_tokens,
    row_family,
)

DECIDE_K = 3
DECIDE_THRESHOLD = 2 / 3
#: a row is re-decided at most this many times in total (R3-8).
MAX_DECIDE_ATTEMPTS = 3
#: a namespace with no new turns re-decides a row only this long after its last decision, so the
#: scheduler's quiet-namespace sweep cannot spend every attempt in a few ticks on unchanged input.
REDECIDE_COOLDOWN_S = 3600
#: re-decide candidates per namespace pass, oldest first.
REDECIDE_LIMIT = 100
#: cosine on `count_key_embedding` at or above which two happenings are related (after token hits).
COSINE_MIN = 0.85

DECIDE_SYSTEM_PROMPT = (
    "You check a user's counted happenings for retellings. Each NEW happening below was just read "
    "from the user's messages. Under it are RELATED happenings that are already counted or were "
    "read earlier on this page. Decide for each NEW happening: \"counted\" when it is a different "
    "happening from every related one (another gym visit on another day is a new happening); "
    "\"repeat\" when it is the SAME happening told again (same occasion, same item, the user "
    "mentioning it a second time); \"same_event\" when it is a different action that belongs to the "
    "same occasion as a related one (bought the ticket and went to that same concert). For repeat "
    "and same_event give the related handle as \"target\". Use the dates: the same happening "
    "retold later usually refers back ('like I said', 'that kit I finished'). When unsure, answer "
    "\"counted\" only if the details clearly differ. Also give \"family\": the short name of the "
    "kind of thing, reusing a related happening's family when it fits. Output ONLY a JSON array with "
    "one object per NEW happening: {\"new\": <handle>, \"decision\": \"counted\"|\"repeat\"|"
    "\"same_event\", \"target\": <related handle or \"\">, \"family\": <family>}."
)


def key_text(row: dict[str, Any]) -> str:
    """The text embedded as `count_key_embedding`: "object action item"."""
    parts = [str(row.get(k) or "").strip() for k in ("count_object", "count_action", "count_item")]
    return " ".join(p for p in parts if p)


@dataclass
class DecidePrompt:
    user: str
    #: handle -> assertion_id, for candidates (N*) and related rows (R*).
    handles: dict[str, str] = field(default_factory=dict)
    #: candidate assertion_id -> its handle.
    candidate_handles: dict[str, str] = field(default_factory=dict)


def _describe(row: dict[str, Any]) -> str:
    return (f"date={str(row.get('valid_at') or '')[:10] or 'unknown'} "
            f"object={json.dumps(str(row.get('count_object') or row_family(row) or ''))} "
            f"action={json.dumps(str(row.get('count_action') or ''))} "
            f"item={json.dumps(str(row.get('count_item') or ''))} "
            f"quote={json.dumps(str(row.get('stated_span') or ''))}")


def build_decide_prompt(sets: list[RelatedSet]) -> DecidePrompt:
    """The user message for one decide call over the candidates that have related counts. A page
    candidate that is related to a later one keeps its N handle."""
    prompt = DecidePrompt(user="")
    for i, rs in enumerate(sets, start=1):
        cid = str(rs.candidate.get("assertion_id") or "")
        handle = f"N{i}"
        prompt.handles[handle] = cid
        prompt.candidate_handles[cid] = handle
    related_handle: dict[str, str] = {}
    related_rows: list[tuple[str, dict[str, Any]]] = []
    lines: list[str] = []
    for rs in sets:
        cid = str(rs.candidate.get("assertion_id") or "")
        refs: list[str] = []
        for row in rs.related:
            rid = str(row.get("assertion_id") or "")
            if rid in prompt.candidate_handles:
                refs.append(prompt.candidate_handles[rid])
                continue
            if rid not in related_handle:
                h = f"R{len(related_handle) + 1}"
                related_handle[rid] = h
                prompt.handles[h] = rid
                related_rows.append((h, row))
            refs.append(related_handle[rid])
        lines.append(f"NEW {prompt.candidate_handles[cid]}: {_describe(rs.candidate)}")
        lines.append(f"  related: {', '.join(refs)}")
    for h, row in related_rows:
        lines.append(f"RELATED {h}: {_describe(row)}")
    prompt.user = "\n".join(lines)
    return prompt


def parse_decide_response(text: str, prompt: DecidePrompt) -> dict[str, tuple[str, str, str]]:
    """{candidate assertion_id: (decision, target assertion_id or "", family)} from one sample.
    Anything malformed, unknown, or naming an unknown handle is dropped (that sample abstains)."""
    raw = str(text or "")
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        items = json.loads(raw[start:end + 1])
    except ValueError:
        return {}
    out: dict[str, tuple[str, str, str]] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        handle = str(item.get("new") or "").strip()
        cid = prompt.handles.get(handle)
        if not cid or cid not in prompt.candidate_handles or cid in out:
            continue
        decision = str(item.get("decision") or "").strip().lower()
        target = ""
        if decision in TARGETED_STATUSES:
            target = prompt.handles.get(str(item.get("target") or "").strip(), "")
            if not target or target == cid:
                continue
        elif decision != COUNTED:
            continue
        family = item.get("family")
        out[cid] = (decision, target, family if isinstance(family, str) else "")
    return out


def vote(
    samples: list[dict[str, tuple[str, str, str]]], candidate_ids: list[str], *,
    k: int, threshold: float = DECIDE_THRESHOLD,
) -> dict[str, tuple[str, str, str, str]]:
    """{candidate_id: (decision, target, family, vote)} for candidates whose (decision, target)
    won at least `threshold` of k samples (an abstaining sample counts against). Family is the
    modal non-empty family among the winning samples, ties to first appearance."""
    need = max(1, math.ceil(threshold * k - 1e-9))
    out: dict[str, tuple[str, str, str, str]] = {}
    for cid in candidate_ids:
        picks = [s[cid] for s in samples if cid in s]
        tally = Counter((d, t) for d, t, _ in picks)
        if not tally:
            continue
        (decision, target), n = max(tally.items(), key=lambda kv: kv[1])
        if n < need:
            continue
        fams = Counter(f for d, t, f in picks if (d, t) == (decision, target) and f.strip())
        family = max(fams, key=lambda f: fams[f]) if fams else ""
        out[cid] = (decision, target, family, f"{n}/{k}")
    return out


@dataclass
class DecideOutcome:
    decisions: list[dict[str, Any]] = field(default_factory=list)
    #: atomic swaps: {candidate_id, target_id, status, family, vote, reason}.
    swaps: list[dict[str, Any]] = field(default_factory=list)
    #: candidates left undecided (needed a decide call that did not run).
    skipped: list[str] = field(default_factory=list)


def _decision(cid: str, status: str, *, target: str = "", family: str = "", vote_: str = "",
              reason: str, truncated: bool) -> dict[str, Any]:
    return {"assertion_id": cid, "status": status, "target_id": target, "family": family,
            "vote": vote_, "reason": reason, "truncated": truncated}


def apply_decide_rules(
    sets: list[RelatedSet], voted: dict[str, tuple[str, str, str, str]], *,
    decided: bool, current_version: str,
) -> DecideOutcome:
    """Turn votes into storable decisions, in candidate order (earlier valid_at first), so a later
    candidate can target an earlier page candidate's final decision. `decided` False means the
    decide call did not run (budget): candidates with related counts stay undecided."""
    out = DecideOutcome()
    final: dict[str, tuple[str, str]] = {}  # page candidate -> (status, root target)
    for rs in sets:
        cand = rs.candidate
        cid = str(cand.get("assertion_id") or "")
        base_family = normalize_family(str(cand.get("count_object") or ""))
        if not relation_tokens(cand):
            d = _decision(cid, UNCERTAIN, family=base_family, reason="no_family",
                          truncated=rs.truncated)
        elif not rs.related:
            d = _decision(cid, UNCERTAIN if rs.truncated else COUNTED, family=base_family,
                          reason="truncated" if rs.truncated else "no_related",
                          truncated=rs.truncated)
        elif not decided:
            out.skipped.append(cid)
            continue
        else:
            d = _rule_one(rs, voted.get(cid), final, base_family, current_version, out)
            if d is None:  # recorded as a swap
                final[cid] = (COUNTED, "")
                continue
        final[cid] = (d["status"], d["target_id"])
        out.decisions.append(d)
    return out


def _rule_one(
    rs: RelatedSet, vote_: tuple[str, str, str, str] | None,
    final: dict[str, tuple[str, str]], base_family: str, current_version: str,
    out: DecideOutcome,
) -> dict[str, Any] | None:
    cand = rs.candidate
    cid = str(cand.get("assertion_id") or "")
    if vote_ is None:
        return _decision(cid, UNCERTAIN, family=base_family, reason="no_agreement",
                         truncated=rs.truncated)
    decision, target, raw_family, tally = vote_
    choices = {base_family} | {normalize_family(row_family(r)) for r in rs.related}
    family = normalize_family(raw_family)
    family = family if family and family in choices else base_family
    if decision == COUNTED:
        if rs.truncated:
            return _decision(cid, UNCERTAIN, family=family, vote_=tally, reason="truncated",
                             truncated=True)
        return _decision(cid, COUNTED, family=family, vote_=tally, reason="decided_new",
                         truncated=False)
    related = {str(r.get("assertion_id") or ""): r for r in rs.related}
    row = related.get(target)
    if row is None or str(row.get("subject_uuid") or "") != str(cand.get("subject_uuid") or ""):
        return _decision(cid, UNCERTAIN, family=family, vote_=tally, reason="target_not_related",
                         truncated=rs.truncated)
    if target in final:  # an earlier page candidate: use its final decision, resolved to the root
        status, root_of = final[target]
        if status == COUNTED:
            root = target
        elif status in TARGETED_STATUSES and root_of:
            root = root_of
        else:
            return _decision(cid, UNCERTAIN, family=family, vote_=tally,
                             reason="target_uncertain", truncated=rs.truncated)
        return _decision(cid, decision, target=root, family=family, vote_=tally,
                         reason="decided_" + decision, truncated=rs.truncated)
    # A pool row already swapped to an earlier page candidate is now that candidate's repeat: the
    # root is that candidate. (Two swaps onto one target would leave both candidates counted.)
    swapped_to = {s["target_id"]: s["candidate_id"] for s in out.swaps}
    if target in swapped_to:
        return _decision(cid, decision, target=swapped_to[target], family=family, vote_=tally,
                         reason="decided_" + decision, truncated=rs.truncated)
    # A pool row: the pool holds only current counted rows.
    if str(row.get("count_status") or "").strip().lower() != COUNTED:
        return _decision(cid, UNCERTAIN, family=family, vote_=tally, reason="target_uncertain",
                         truncated=rs.truncated)
    cand_at, target_at = str(cand.get("valid_at") or ""), str(row.get("valid_at") or "")
    if (cand_at and target_at and cand_at < target_at and not rs.truncated
            and str(row.get("perceiver_version") or "") == current_version):
        out.swaps.append({"candidate_id": cid, "target_id": target, "status": decision,
                          "family": family, "vote": tally, "reason": "swap_earlier"})
        return None
    return _decision(cid, decision, target=target, family=family, vote_=tally,
                     reason="decided_" + decision, truncated=rs.truncated)


__all__ = [
    "COSINE_MIN",
    "DECIDE_K",
    "DECIDE_SYSTEM_PROMPT",
    "DECIDE_THRESHOLD",
    "DecideOutcome",
    "DecidePrompt",
    "MAX_DECIDE_ATTEMPTS",
    "REDECIDE_LIMIT",
    "apply_decide_rules",
    "build_decide_prompt",
    "key_text",
    "parse_decide_response",
    "vote",
]
