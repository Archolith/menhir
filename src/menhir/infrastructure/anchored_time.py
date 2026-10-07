"""Anchored-time resolver: deterministic parts (detector, prompt, calendar, clause guard, overlay).

Ported from the P0 prototype (reich_lib.py sha256 a7797891, clause_guard.py sha256 ec4472e9);
see .agent/plans/menhir-anchored-time-resolver-p1-plan.md in the workspace. The prompt text is
frozen: any wording change needs a new dev/held-out round. Everything here is pure and
synchronous; the LLM call lives in ``anchored_time_resolver``.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Sequence

PROMPT_VERSION = "a7797891"

# ---------------------------------------------------------------- detector (deterministic)
_NUM = r"(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a few|a couple(?: of)?|several|few)"
_UNIT = r"(?:days?|nights?|weeks?|months?|years?|decades?|hours?)"
_WDAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_SEASON = r"(?:spring|summer|fall|autumn|winter)"
_MONTH = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
          r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)")
CUES = {
    "explicit": re.compile(
        rf"\b(\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|{_MONTH}\.? \d{{1,2}}(?:st|nd|rd|th)?\b|"
        rf"\d{{1,2}}(?:st|nd|rd|th)? of {_MONTH}|in {_MONTH}\b|(?:19|20)\d{{2}}\b)", re.I),
    "speech_relative": re.compile(
        rf"\b({_NUM} {_UNIT} (?:ago|back|from now)|in {_NUM} {_UNIT}\b|yesterday|today|tonight|tomorrow|"
        rf"(?:last|this|next|this past|this coming) (?:week|weekend|month|year|night|morning|evening|{_WDAY}|{_SEASON})|"
        rf"earlier (?:this|today)|later (?:this|today)|the other day|on {_WDAY}\b)", re.I),
    "event_anchored": re.compile(
        rf"\b({_NUM} {_UNIT} (?:before|after|later|earlier|prior|into|in advance)|in advance|ahead of time|beforehand|"
        rf"afterwards?|prior to|(?:right |just |shortly |soon )?(?:before|after|since|until|following|during) "
        rf"(?:the|my|our|his|her|their|we|i|it|that|this)\b|the (?:day|week|month|year|night|morning) (?:before|after)|"
        rf"by the time|when i (?:was|got|moved|started|left|finished))", re.I),
    "duration": re.compile(
        rf"\b(for (?:about |almost |nearly |over |around |roughly |just over |the (?:past|last) )?{_NUM} {_UNIT}|"
        rf"(?:the )?(?:past|last) {_NUM} {_UNIT})\b", re.I),
    "vague": re.compile(r"\b(recently|lately|a while (?:ago|back)|not long ago|some time ago|for (?:ages|years|a while)|"
                        r"these days|nowadays|back then|at some point|in the past)\b", re.I),
}


def detect(text: str) -> list[str]:
    return [k for k, rx in CUES.items() if rx.search(text or "")]


# ---------------------------------------------------------------- resolver prompt (LLM)
SYSTEM = ("You identify WHEN things happened, relative to when they were said. You do not do date arithmetic; "
          "you describe the time expression so code can compute it. Never guess a time that is not stated.")

INSTRUCTIONS = """For each FACT, decide how the TURN locates that fact's event or state in time.

basis:
- "explicit_date": a calendar date, month, day of month, or year is stated, even without the year ("on March 3rd", "in October", "on the 20th", "in 2019"). A fixed-date holiday counts as its calendar date ("around New Year's" = --01-01); a holiday whose date moves each year is "vague" unless dated.
- "speech_relative": measured from when the TURN was said ("two weeks ago", "last Friday", "next month", "yesterday").
- "event_anchored": measured from ANOTHER event ("two days after the outage", "booked it a month before the trip", "since I moved").
- "vague": a time is mentioned but cannot be placed ("recently", "a while back", "lately", "for years").
- "none": no time is stated for this fact (ongoing preferences, questions, requests, general states, recurring schedules like "every Tuesday" or "on Mondays").

Rules:
- The time phrase is often in the TURN even when the FACT text omits it. Attach it when it describes the fact's own event or state; do not borrow a time that belongs to a different event in the turn.
- If the FACT text itself states a time ("In early May, the user ..."), use it, even when the TURN is vaguer; the fact may summarize an earlier conversation.
- "since" or "as" meaning "because" ("since I have a big dog, ...") is not a time expression.
- Requests, questions, wishes, and interests ("wants ideas for next month's trip", "asks about events this fall") get "none": the time belongs to the topic, not to the asking. A user's own plan or intended action ("plans to repaint the kitchen next month") does get the time.
- A duration that runs up to now places the START of a state: "I've lived here for three years" / "subscribed for about six months now" = speech_relative offset {amount: 3, unit: year, direction: before}. A duration of a finished activity ("it took me two weeks") does not place the activity.
- "expression" is copied verbatim from the TURN (or from the FACT when the time comes from the fact text); null for "none".
- kind: "point_event" (happened once), "state" (holds over a period), or "plan" (intended/future).
- For speech_relative and event_anchored give EITHER "offset" {amount, unit: day|week|month|year, direction: before|after, approx: true if "about/a few/a couple/several"} OR "calendar" {which: last|this|next|past|upcoming, unit: day|week|weekend|month|quarter|year|weekday|season, name: weekday or season name or null, count: 1 unless stated}. "past"/"upcoming" = a bare weekday or season with no last/next, placed by tense ("on Saturday" in past tense = past). "two summers ago" = {which: last, unit: season, name: summer, count: 2}. Use calendar, not offset, whenever a weekday or season is named. "a few"/"a couple" -> amount 3 or 2 with approx true. "yesterday" = calendar {which: last, unit: day}; "today"/"tonight" = {which: this, unit: day}; "tomorrow" = {which: next, unit: day}.
- For event_anchored, "anchor" = {"fact": index of the anchor event in FACTS, or null; "event": short description if it is not in FACTS}. offset/direction are relative to the anchor. "since X" = offset 0 after X. If no amount is given ("before the trip"), offset {amount: null, direction: before|after}. A calendar unit relative to the anchor uses "calendar" instead of offset, with "later": true when the phrase says later/afterwards: "later that week" = {which: this, unit: week, later: true}; "the following month" = {which: next, unit: month}; "that same evening" = {which: this, unit: day}.
- For explicit_date give "date" exactly as stated, never adding a missing year: YYYY-MM-DD, YYYY-MM, YYYY, --MM-DD (month and day), --MM (month only), or ---DD (day of month only).
- "missing_events": dated events or states in the TURN that no FACT covers, each {"event", "expression"}. Empty list if none.

Return JSON only: {"facts": [{"i", "expression", "basis", "kind", "offset", "calendar", "date", "anchor"}], "missing_events": [...]}

Example A
SPEECH_TIME: 2024-02-14 (Wednesday)
TURN: We finally shipped the billing migration two days after the outage last Thursday, and I've been on call ever since. I still prefer Postgres for this kind of thing.
FACTS:
0. The team shipped the billing migration.
1. There was a production outage.
2. The user has been on call.
3. The user prefers Postgres.
OUTPUT: {"facts": [
 {"i": 0, "expression": "two days after the outage", "basis": "event_anchored", "kind": "point_event", "offset": {"amount": 2, "unit": "day", "direction": "after", "approx": false}, "calendar": null, "date": null, "anchor": {"fact": 1, "event": null}},
 {"i": 1, "expression": "last Thursday", "basis": "speech_relative", "kind": "point_event", "offset": null, "calendar": {"which": "last", "unit": "weekday", "name": "thursday"}, "date": null, "anchor": null},
 {"i": 2, "expression": "ever since", "basis": "event_anchored", "kind": "state", "offset": {"amount": 0, "unit": "day", "direction": "after", "approx": false}, "calendar": null, "date": null, "anchor": {"fact": 0, "event": null}},
 {"i": 3, "expression": null, "basis": "none", "kind": "state", "offset": null, "calendar": null, "date": null, "anchor": null}],
 "missing_events": []}

Example B
SPEECH_TIME: 2023-09-03 (Sunday)
TURN: My knee started hurting about three weeks ago, right after the half-marathon, and the physio appointment is next Friday. I renewed my gym membership on August 1st.
FACTS:
0. The user's knee hurts.
1. The user has a physio appointment.
OUTPUT: {"facts": [
 {"i": 0, "expression": "about three weeks ago", "basis": "speech_relative", "kind": "state", "offset": {"amount": 3, "unit": "week", "direction": "before", "approx": true}, "calendar": null, "date": null, "anchor": null},
 {"i": 1, "expression": "next Friday", "basis": "speech_relative", "kind": "plan", "offset": null, "calendar": {"which": "next", "unit": "weekday", "name": "friday"}, "date": null, "anchor": null}],
 "missing_events": [{"event": "user ran a half-marathon", "expression": "about three weeks ago"}, {"event": "user renewed gym membership", "expression": "on August 1st"}]}
"""


def build_messages(turn: str, speech_date: str, facts: list[str]) -> list[dict]:
    d = date.fromisoformat(speech_date[:10])
    listed = "\n".join(f"{i}. {f}" for i, f in enumerate(facts))
    user = (f"{INSTRUCTIONS}\nNow the real input.\nSPEECH_TIME: {d.isoformat()} ({d.strftime('%A')})\n"
            f"TURN: {turn}\nFACTS:\n{listed}\nOUTPUT:")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


# ---------------------------------------------------------------- deterministic calendar
_WD = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11), "winter": (12, 2)}
_TOL = {"day": 0, "week": 3, "month": 10, "year": 60}
_TOL_APPROX = {"day": 1, "week": 7, "month": 30, "year": 365}


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _month_window(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _shift(d: date, amount: float, unit: str, sign: int) -> date:
    if unit == "day":
        return d + timedelta(days=sign * amount)
    if unit == "week":
        return d + timedelta(days=sign * 7 * amount)
    if unit == "month":
        return _add_months(d, sign * int(round(amount)))
    if unit == "year":
        return _add_months(d, sign * 12 * int(round(amount)))
    raise ValueError(unit)


def _season(ref: date, name: str, which: str, count: int = 1) -> tuple[date, date]:
    a, b = _SEASONS[name]

    def win(year: int) -> tuple[date, date]:  # season starting in `year`
        if a > b:  # winter spans years
            return date(year, a, 1), _month_window(year + 1, b)[1]
        return date(year, a, 1), _month_window(year, b)[1]

    cands = [win(y) for y in range(ref.year - 6, ref.year + 2)]
    if which == "last":
        past = [w for w in cands if w[1] < ref]
        return past[-count]
    if which == "next":
        return [w for w in cands if w[0] > ref][0]
    cur = [w for w in cands if w[0] <= ref <= w[1]]
    return cur[0] if cur else [w for w in cands if w[1] < ref][-1]


def _calendar(ref: date, cal: dict) -> tuple[date, date] | None:
    which, unit, name = cal.get("which"), cal.get("unit"), (cal.get("name") or "").lower()
    step = {"last": -1, "this": 0, "next": 1, "past": -1, "upcoming": 1}.get(which)
    if step is None:
        return None
    try:
        count = max(1, int(cal.get("count") or 1))
    except (TypeError, ValueError):
        count = 1
    if unit == "weekday" and name in _WD and which in ("past", "upcoming"):  # bare weekday, by tense
        k = _WD.index(name)
        d = ref - timedelta(days=(ref.weekday() - k) % 7 or 7) if which == "past" \
            else ref + timedelta(days=(k - ref.weekday()) % 7 or 7)
        return d, d
    which = {"past": "last", "upcoming": "next"}.get(which, which)
    if unit == "quarter":
        q = ref.replace(day=1, month=3 * ((ref.month - 1) // 3) + 1)
        s = _add_months(q, 3 * step * (count if step else 1))
        return s, _add_months(s, 3) - timedelta(days=1)
    if unit == "season" and name in _SEASONS and which == "last" and count > 1:
        return _season(ref, name, "last", count)
    if unit == "day":
        d = ref + timedelta(days=step)
        return d, d
    monday = ref - timedelta(days=ref.weekday())
    if unit == "week":
        s = monday + timedelta(days=7 * step)
        return s, s + timedelta(days=6)
    if unit == "weekend":
        s = monday + timedelta(days=7 * step + 5)
        return s, s + timedelta(days=1)
    if unit == "weekday" and name in _WD:
        k = _WD.index(name)
        if which == "last":
            back = (ref.weekday() - k) % 7 or 7
            d = ref - timedelta(days=back)
            return (d - timedelta(days=7), d) if back <= 2 else (d, d)  # "last Fri" said on Sat/Sun is ambiguous
        if which == "next":
            fwd = (k - ref.weekday()) % 7 or 7
            d = ref + timedelta(days=fwd)
            return d, d + timedelta(days=7)  # "next Fri" may mean this coming or the one after
        d = monday + timedelta(days=k)
        return d, d
    if unit == "month":
        m = _add_months(ref.replace(day=1), step)
        return _month_window(m.year, m.month)
    if unit == "year":
        return date(ref.year + step, 1, 1), date(ref.year + step, 12, 31)
    if unit == "season" and name in _SEASONS:
        return _season(ref, name, which)
    return None


def _explicit(ref: date, value: str, kind: str) -> tuple[date, date] | None:
    v = (value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            d = date.fromisoformat(v)
            return d, d
        if re.fullmatch(r"\d{4}-\d{2}", v):
            return _month_window(int(v[:4]), int(v[5:7]))
        if re.fullmatch(r"\d{4}", v):
            return date(int(v), 1, 1), date(int(v), 12, 31)
        m = re.fullmatch(r"--(\d{2})", v)  # month, no year
        if m:
            mo = int(m.group(1))
            for y in ((ref.year, ref.year + 1) if kind == "plan" else (ref.year, ref.year - 1)):
                w = _month_window(y, mo)
                if (kind == "plan" and w[1] >= ref) or (kind != "plan" and w[0] <= ref):
                    return w
            return None
        m = re.fullmatch(r"---(\d{2})", v)  # day of month only
        if m:
            dy, cur = int(m.group(1)), ref.replace(day=1)
            for k in range(0, 3):
                mm = _add_months(cur, k if kind == "plan" else -k)
                if dy <= calendar.monthrange(mm.year, mm.month)[1]:
                    d = mm.replace(day=dy)
                    if (kind == "plan" and d >= ref) or (kind != "plan" and d <= ref):
                        return d, d
            return None
        m = re.fullmatch(r"-*(\d{1,2})-(\d{1,2})", v)  # month-day; tolerate any dash prefix
        if m:
            mo, dy = int(m.group(1)), int(m.group(2))
            for y in ((ref.year, ref.year + 1) if kind == "plan" else (ref.year, ref.year - 1)):
                d = date(y, mo, dy)
                if (kind == "plan" and d >= ref) or (kind != "plan" and d <= ref):
                    return d, d
    except ValueError:
        return None
    return None


def compute(ref: date, items: dict[int, dict], i: int, depth: int = 0):
    """Window (start, end) for fact i; either bound may be None (open). None if not placeable."""
    it = items.get(i)
    if not it or depth > 3:
        return None
    basis, kind = it.get("basis"), it.get("kind") or "point_event"
    off, cal = it.get("offset") or None, it.get("calendar") or None
    if basis == "explicit_date":
        return _explicit(ref, it.get("date"), kind)
    if basis == "speech_relative":
        if cal:
            return _calendar(ref, cal)
        if off and off.get("amount") is not None and off.get("unit") in _TOL:
            sign = 1 if off.get("direction") == "after" else -1
            c = _shift(ref, float(off["amount"]), off["unit"], sign)
            tol = (_TOL_APPROX if off.get("approx") else _TOL)[off["unit"]]
            return c - timedelta(days=tol), c + timedelta(days=tol)
        return None
    if basis == "event_anchored":
        anc = (it.get("anchor") or {}).get("fact")
        if not isinstance(anc, int) or anc == i:
            return None
        aw = compute(ref, items, anc, depth + 1)
        if not aw or aw[0] is None or aw[1] is None:
            return None
        if cal:  # calendar unit relative to the anchor ("later that week", "the following month")
            w = _calendar(aw[0], cal)
            if not w:
                return None
            if cal.get("later"):
                s = aw[1] + timedelta(days=1)
                # anchor at the end of its unit: "later that week" = the days right after it
                return (s, w[1]) if w[1] >= s else (s, s + timedelta(days=6))
            return w
        if not off:
            return None
        sign = 1 if off.get("direction") == "after" else -1
        if off.get("amount") is None:  # "before the trip" / "after the move": open interval
            return (aw[1], None) if sign > 0 else (None, aw[0])
        unit = off.get("unit") if off.get("unit") in _TOL else "day"
        tol = (_TOL_APPROX if off.get("approx") else _TOL)[unit]
        s, e = _shift(aw[0], float(off["amount"]), unit, sign), _shift(aw[1], float(off["amount"]), unit, sign)
        return s - timedelta(days=tol), e + timedelta(days=tol)
    return None


def parse_output(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    return json.loads(t)


def window_midpoint(w):
    """Midpoint of a window (the prototype's ``_mid``); an open window gives its known bound."""
    s, e = w
    if s is None and e is None:
        return None
    if s is None:
        return e
    if e is None:
        return s
    return s + (e - s) / 2


# ---------------------------------------------------------------- clause guard (deterministic)
# A time expression the resolver attached to several facts must not scope BACKWARD over earlier
# members of a list in the same sentence ("read A, B, and C, which I finished last weekend").
# Only removes times; never adds or moves one. Rules are documented in the P0 prototype.
_STOP = set("""a an the and or but so if of to in on at by for from with without into onto about over under after
before since until during while as than then that this these those there here it its it's i i'm i've i'd me my
mine we we're we've we'd us our you your he him his she her they them their is am are was were be been being
have has had do does did done will would can could should may might must just also too very really still now
not no yes up out off one ones some any all each every both other another which who whom whose where when what
how why user user's users team""".split())
_TIME = set("""day days week weeks weekend weekends month months year years decade decades hour hours night nights
morning evening afternoon ago later earlier early late mid last next past this coming previous following
monday tuesday wednesday thursday friday saturday sunday spring summer fall autumn winter january february
march april june july august september october november december yesterday today tonight tomorrow
few couple several two three four five six seven eight nine ten twelve""".split())
_REL = r"(?:which|who|whom|where|when|whose)"
_LEAD = {"so", "and", "but", "well", "oh", "also", "then", "anyway", "actually", "plus"}
_ABBR = r"(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bSt)(?<!\bJr)(?<!\bSr)(?<!\bvs)(?<!\bMt)(?<!\bNo)"


def _stem(w: str) -> str:
    w = w.lower().strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9']*", (text or "").lower())


def _content(text: str, exclude: set[str] = frozenset()) -> set[str]:
    return {_stem(w) for w in _words(text) if w not in _STOP and len(w) > 2 and _stem(w) not in exclude}


def _sentence(turn: str, pos: int) -> tuple[int, int]:
    ends = [m.end() for m in re.finditer(rf"{_ABBR}[.!?]+(?=\s|$)|[;\n]", turn)]
    s = max([e for e in ends if e <= pos], default=0)
    e = min([e for e in ends if e > pos], default=len(turn))
    return s, e


def _clause_start_and_text(turn: str, pos: int) -> tuple[int, str]:
    """Absolute start of the expression's clause, and the clause text (with a relative clause's antecedent)."""
    s0, e0 = _sentence(turn, pos)
    sent = turn[s0:e0]
    cuts = sorted({0, len(sent)} | {m.end() for m in re.finditer(rf",|\s[-\u2013\u2014]+\s|\s(?={_REL}\b)", sent,
                                                                    flags=re.I)})
    segs = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1) if sent[cuts[i]:cuts[i + 1]].strip()]
    rel = pos - s0
    k = max([i for i, (a, _) in enumerate(segs) if a <= rel], default=0)
    a, b = segs[k]
    text = sent[a:b]
    if k > 0 and re.match(rf"\s*{_REL}\b", text, flags=re.I):
        text = " ".join(_words(sent[segs[k - 1][0]:segs[k - 1][1]])[-4:]) + " " + text
    return s0 + a, text


def _sentence_initial(turn: str, pos: int) -> bool:
    s0, _ = _sentence(turn, pos)
    return all(w in _LEAD for w in _words(turn[s0:pos]))


def _norm(expr) -> str:
    return (expr or "").strip().strip('"').lower()


def clause_guard(turn: str, facts: list[str], items: dict[int, dict]) -> list[int]:
    """Mutates items: drops a list-distributed time on earlier list members. Returns dropped indices."""
    low = turn.lower()
    groups: dict[int, list[int]] = {}
    span_end: dict[int, int] = {}
    for i, it in items.items():
        expr = _norm(it.get("expression"))
        if not expr or it.get("basis") in (None, "none") or not (0 <= i < len(facts)):
            continue
        pos = low.find(expr)
        if pos < 0:
            continue
        groups.setdefault(pos, []).append(i)
        span_end[pos] = max(span_end.get(pos, 0), pos + len(expr))
    dropped = []
    for pos, members in groups.items():
        if len(members) < 2 or _sentence_initial(turn, pos):
            continue
        expr_all = _content(turn[pos:span_end[pos]])
        anchor_words = expr_all - _TIME
        s0, e0 = _sentence(turn, pos)
        c_start, c_text = _clause_start_and_text(turn, pos)
        clause = _content(c_text, exclude=expr_all)
        forward = clause | _content(turn[c_start:e0], exclude=expr_all)
        before = _content(turn[s0:c_start])
        own = {i: _content(facts[i], exclude=expr_all) for i in members}
        dist = {i: own[i] - set().union(*(own[j] for j in members if j != i)) for i in members}
        if not any(dist[i] & clause for i in members):
            continue
        for i in members:
            if _norm(items[i].get("expression")) in facts[i].lower():
                continue
            if _content(facts[i]) & anchor_words:
                continue
            if dist[i] & forward or not (dist[i] & before):
                continue
            items[i] = {**items[i], "basis": "none", "offset": None, "calendar": None, "date": None,
                        "anchor": None, "guard": "clause"}
            dropped.append(i)
    return sorted(dropped)


# ---------------------------------------------------------------- Menhir integration (pure)
DATED_BASES = frozenset({"explicit_date", "speech_relative", "event_anchored"})
_ROLE_PREFIX = re.compile(r"^\s*user:\s*", re.I)
_GRANULARITY = {"day": "day", "weekday": "day", "week": "week", "weekend": "week", "month": "month",
                "quarter": "quarter", "season": "season", "year": "year"}


def strip_user_prefix(text: str) -> str:
    """The ingest writes ``user: ...``; P0 turns had no prefix (mechanical change, not wording)."""
    return _ROLE_PREFIX.sub("", text or "", count=1)


def parse_items(content: str) -> tuple[dict[int, dict], list]:
    """Resolver items keyed by fact index, and ``missing_events``. Raises on unparseable output."""
    out = parse_output(content)
    if not isinstance(out, dict):
        raise ValueError("resolver output is not a JSON object")
    items: dict[int, dict] = {}
    for it in out.get("facts") or []:
        if not isinstance(it, dict):
            continue
        try:
            items[int(it.get("i"))] = it
        except (TypeError, ValueError):
            pass
    missing = out.get("missing_events") or []
    return items, missing if isinstance(missing, list) else []


def granularity_of(item: dict | None) -> str | None:
    if not item:
        return None
    basis = item.get("basis")
    if basis == "explicit_date":
        v = str(item.get("date") or "").strip()
        if re.fullmatch(r"\d{4}", v):
            return "year"
        if re.fullmatch(r"\d{4}-\d{2}|--\d{2}", v):
            return "month"
        return "day" if v else None
    if basis in ("speech_relative", "event_anchored"):
        cal = item.get("calendar") or None
        if isinstance(cal, dict):
            return _GRANULARITY.get(str(cal.get("unit") or ""))
        off = item.get("offset") or None
        if isinstance(off, dict) and off.get("amount") is not None:
            return _GRANULARITY.get(str(off.get("unit") or ""))
    return None


def _utc_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(timezone.utc).date()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class EdgeTimeInput:
    uuid: str
    fact: str
    valid_at: datetime | None
    invalid_at: datetime | None


@dataclass(frozen=True)
class AnchoredTimeResult:
    """Per-fact resolver outcome; the in-memory data contract P2 will persist (P1 persists none)."""

    edge_uuid: str
    fact_index: int
    expression: str | None
    basis: str
    kind: str
    granularity: str | None
    window_start: date | None
    window_end: date | None
    anchor_ref: str | None
    anchor_offset: dict | None
    planned_window: tuple[date | None, date | None] | None
    guard: str | None
    written: bool
    reason: str
    graphiti_valid_at: datetime | None
    new_valid_at: datetime | None


@dataclass
class AnchoredTimeReport:
    """Per-episode telemetry on the extraction receipt."""

    status: str = "pending"
    reason: str = ""
    prompt_version: str = PROMPT_VERSION
    model: str = ""
    facts: int = 0
    overridden: int = 0
    kept_inside_window: int = 0
    plan_not_written: int = 0
    guard_drops: tuple[int, ...] = ()
    missing_events: int = 0
    latency_s: float | None = None
    cached: bool = False
    results: tuple[AnchoredTimeResult, ...] = field(default_factory=tuple)


def plan_overlay(
    edges: Sequence[EdgeTimeInput],
    items: dict[int, dict],
    speech_date: date,
) -> list[AnchoredTimeResult]:
    """Decide the ``valid_at`` overlay for every edge. Pure: same inputs, same decisions.

    ``valid_at`` = window midpoint at 00:00 UTC, written only when the basis is dated, the window
    is closed, the fact is not a plan, Graphiti's value is missing or outside the window, and the
    edge's ``invalid_at`` would stay after it. Everything else keeps Graphiti's value.
    """
    results: list[AnchoredTimeResult] = []
    for i, edge in enumerate(edges):
        item = items.get(i) or {}
        basis = str(item.get("basis") or "none")
        kind = str(item.get("kind") or "point_event")
        window = compute(speech_date, items, i) if basis in DATED_BASES else None
        start, end = window if window else (None, None)
        anchor = item.get("anchor") if isinstance(item.get("anchor"), dict) else None
        anchor_ref = None
        if anchor:
            idx = anchor.get("fact")
            if isinstance(idx, int) and 0 <= idx < len(edges) and idx != i:
                anchor_ref = edges[idx].uuid
            elif anchor.get("event"):
                anchor_ref = str(anchor.get("event"))
        offset = item.get("offset") if isinstance(item.get("offset"), dict) else None
        cal = item.get("calendar") if isinstance(item.get("calendar"), dict) else None
        new_valid_at = None
        if not item:
            reason = "no_item"
        elif basis not in DATED_BASES:
            reason = "guard" if item.get("guard") else "undated"
        elif window is None or start is None or end is None:
            reason = "open_or_unplaceable"
        elif kind == "plan":
            reason = "plan"
        else:
            mid = window_midpoint((start, end))
            candidate = datetime(mid.year, mid.month, mid.day, tzinfo=timezone.utc)
            if edge.valid_at is not None and start <= _utc_date(edge.valid_at) <= end:
                reason = "graphiti_inside_window"
            elif edge.invalid_at is not None and _aware(edge.invalid_at) <= candidate:
                reason = "would_invert_interval"
            else:
                reason = "written"
                new_valid_at = candidate
        results.append(AnchoredTimeResult(
            edge_uuid=edge.uuid,
            fact_index=i,
            expression=item.get("expression") if isinstance(item.get("expression"), str) else None,
            basis=basis,
            kind=kind,
            granularity=granularity_of(item) if basis in DATED_BASES else None,
            window_start=start,
            window_end=end,
            anchor_ref=anchor_ref,
            anchor_offset=offset or cal,
            planned_window=(start, end) if kind == "plan" and window else None,
            guard=item.get("guard") if isinstance(item.get("guard"), str) else None,
            written=new_valid_at is not None,
            reason=reason,
            graphiti_valid_at=edge.valid_at,
            new_valid_at=new_valid_at,
        ))
    return results
