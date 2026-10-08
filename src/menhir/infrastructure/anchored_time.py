"""Anchored-time resolver: deterministic parts (detector, prompt, calendar, clause guard, overlay).

Ported from the P0 prototype (reich_lib.py sha256 a7797891, clause_guard.py sha256 ec4472e9);
see .agent/plans/menhir-anchored-time-resolver-p1-plan.md in the workspace. The prompt text is
frozen: any wording change needs a new dev/held-out round. Everything here is pure and
synchronous; the LLM call lives in ``anchored_time_resolver``.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
import functools
import json
import math
import re
from typing import Sequence

import holidays

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
    if unit in ("month", "year"):
        months = amount * (12 if unit == "year" else 1)
        if float(months).is_integer():
            return _add_months(d, sign * int(months))
        # "1.5 years" is 18 months; "half a month" has no whole-month form, so shift by days
        return d + timedelta(days=sign * round(months * 30.4375))
    raise ValueError(unit)


def _season(ref: date, name: str, which: str, count: int = 1, kind: str = "point_event") -> tuple[date, date]:
    a, b = _SEASONS[name]

    def win(year: int) -> tuple[date, date]:  # season starting in `year`
        if a > b:  # winter spans years
            return date(year, a, 1), _month_window(year + 1, b)[1]
        return date(year, a, 1), _month_window(year, b)[1]

    cands = [win(y) for y in range(ref.year - 6, ref.year + 2)]
    if which == "last":
        past = [w for w in cands if w[1] < ref]
        return past[-count] if count <= len(past) else None
    if which == "next":
        return [w for w in cands if w[0] > ref][0]
    cur = [w for w in cands if w[0] <= ref <= w[1]]
    if cur:
        return cur[0]
    # "this summer" outside summer: upcoming for a plan, the last one otherwise (the caller
    # flags year_ambiguous when the other occurrence is nearer, as for yearless dates)
    if kind == "plan":
        return [w for w in cands if w[0] > ref][0]
    return [w for w in cands if w[1] < ref][-1]


def _calendar(ref: date, cal: dict) -> tuple[date, date] | None:
    which, unit, name = cal.get("which"), cal.get("unit"), (cal.get("name") or "").lower()
    step = {"last": -1, "this": 0, "next": 1, "past": -1, "upcoming": 1}.get(which)
    if step is None:
        return None
    raw = cal.get("count")
    if raw in (None, ""):
        count = 1
    elif isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        return None
    else:
        try:
            count = max(1, int(raw))
        except (TypeError, ValueError):  # an unreadable count is not guessed as 1
            return None
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
    if unit in ("day", "week", "weekend", "month", "year"):
        # count = the n-th unit back or ahead, as for quarters and seasons ("the week before last"
        # = 2; "in the last six weeks" = 6 places a state's start, the prompt's duration rule)
        step *= count
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
            if not 1 <= mo <= 12:
                return None
            # An invalid candidate year (Feb 29 in 2025) is skipped, not fatal: said 2025-03-01 the
            # past Feb 29 is 2024-02-29.
            for y in ((ref.year, ref.year + 1) if kind == "plan" else (ref.year, ref.year - 1)):
                if dy > calendar.monthrange(y, mo)[1]:
                    continue
                d = date(y, mo, dy)
                if (kind == "plan" and d >= ref) or (kind != "plan" and d <= ref):
                    return d, d
    except ValueError:
        return None
    return None


_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december")
_MONTH_WORD = re.compile(r"\b(" + "|".join(_MONTH_NAMES) + r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b",
                         re.I)


def _date_month(value: str) -> int | None:
    for pattern in (r"\d{4}-(\d{2})(?:-\d{2})?", r"--(\d{2})", r"-*(\d{1,2})-\d{1,2}"):
        m = re.fullmatch(pattern, value)
        if m:
            return int(m.group(1))
    return None


def _checked_date(item: dict) -> str | None:
    """The explicit date, checked against the one month its own expression names.

    A dropped month is put back: "on February 10th" -> ``---10`` (seen live) becomes ``--02-10``,
    and "in May 2019" -> ``2019`` becomes ``2019-05`` when the year is in the expression too. A date
    with a different month is rejected (None). No named month, or several: the date as given.
    """
    expression = str(item.get("expression") or "")
    prefixes = [n[:3] for n in _MONTH_NAMES]  # unique per month
    named = {prefixes.index(w.group(1).lower()[:3]) + 1 for w in _MONTH_WORD.finditer(expression)}
    value = str(item.get("date") or "").strip()
    if len(named) != 1:
        return value
    (month,) = named
    if re.fullmatch(r"\d{4}", value):
        return f"{value}-{month:02d}" if re.search(rf"\b{value}\b", expression) else value
    if re.fullmatch(r"---\d{2}", value):
        return f"--{month:02d}-{value[3:]}"
    return value if _date_month(value) == month else None


_HOLIDAY_NAMES = {
    # holidays.US name -> normalized event texts that mean it (see _norm_event)
    "Christmas Day": ("christmas", "christmas day", "xmas"),
    "Christmas Eve": ("christmas eve", "xmas eve"),
    "New Year's Day": ("new years", "new years day"),
    "New Year's Eve": ("new years eve",),
    "Independence Day": ("independence day", "4th of july", "fourth of july", "july 4th", "july fourth"),
    "Thanksgiving Day": ("thanksgiving", "thanksgiving day"),
    "Easter Sunday": ("easter", "easter sunday"),
    "Good Friday": ("good friday",),
    "Memorial Day": ("memorial day",),
    "Labor Day": ("labor day",),
    "Mother's Day": ("mothers day",),
    "Father's Day": ("fathers day",),
    "Valentine's Day": ("valentines day", "valentines"),
    "Saint Patrick's Day": ("st patricks day", "saint patricks day", "st paddys day"),
    "Halloween": ("halloween",),
    "Martin Luther King Jr. Day": ("mlk day", "martin luther king day", "martin luther king jr day"),
    "Washington's Birthday": ("presidents day", "washingtons birthday"),
    "Juneteenth National Independence Day": ("juneteenth",),
    "Veterans Day": ("veterans day",),
    "Columbus Day": ("columbus day",),
    "Groundhog Day": ("groundhog day",),
}
_HOLIDAY_ALIASES = {alias: (name, 0) for name, aliases in _HOLIDAY_NAMES.items() for alias in aliases}
_HOLIDAY_ALIASES.update({"black friday": ("Thanksgiving Day", 1), "cyber monday": ("Thanksgiving Day", 4)})


def _norm_event(text) -> str:
    """Exact-match key: "Father John's sermon" must not become Father's Day."""
    t = re.sub(r"['’.]", "", str(text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t[4:] if t.startswith("the ") else t


@functools.lru_cache(maxsize=64)
def _us_holidays(year: int) -> dict[str, date]:
    """Holiday name -> actual date (observed weekday shifts dropped). US: the LME data is US-centric."""
    out: dict[str, date] = {}
    days = holidays.US(years=year, categories=(holidays.PUBLIC, holidays.UNOFFICIAL))
    for d in sorted(days):
        for name in days.get_list(d):
            if not name.endswith("(observed)"):
                out.setdefault(name, d)
    return out


def compute(ref: date, items: dict[int, dict], i: int, depth: int = 0, *, n_facts: int | None = None):
    """Window (start, end) for fact i; either bound may be None (open). None if not placeable.

    ``n_facts``: when given, fact indexes and anchors outside 0..n_facts-1 are unplaceable.
    """
    return _place(ref, items, i, n_facts, depth)[0]


def _two_options_of(cal: dict | None, w) -> str | None:
    return "two_options" if w and w[0] and w[1] and _two_options(cal, w[0], w[1]) else None


def _other_kind(kind: str) -> str:
    return "point_event" if kind == "plan" else "plan"


def _distance(ref: date, w) -> int | None:
    """Days from the speech date to a closed window (0 when it contains the speech date)."""
    if not w or w[0] is None or w[1] is None:
        return None
    if w[0] <= ref <= w[1]:
        return 0
    return (ref - w[1]).days if w[1] < ref else (w[0] - ref).days


_NEAR_OTHER_DAYS = 31


def _year_ambiguity(ref: date, chosen, other) -> str | None:
    """"year_ambiguous" when a yearless date ("June", "Feb 5th", "on the 20th", "Thanksgiving") is
    placed by the resolver's kind (past for events/states, ahead for plans) while its other occurrence
    is nearer the speech date and within a month of it ("on February 5th" said Feb 3 as a past
    event -> 2022). The kind label alone is not trusted for that move; a far other occurrence
    ("on May 1st" said in November) leaves the kind's choice standing."""
    if not chosen or not other or chosen == other:
        return None
    dc, do = _distance(ref, chosen), _distance(ref, other)
    if dc is None or do is None:
        return None
    return "year_ambiguous" if do < dc and do <= _NEAR_OTHER_DAYS else None


def _place(ref: date, items: dict[int, dict], i: int, n_facts: int | None, depth: int = 0):
    """(window, ambiguity) for fact i. ambiguity: None, "two_options" (its own calendar names two
    candidate days), "year_ambiguous" (a yearless date whose other occurrence is nearer the speech
    date) or "ambiguous_anchor" (a fact it is anchored to is ambiguous)."""
    if n_facts is not None and not 0 <= i < n_facts:
        return None, None
    it = items.get(i)
    if not it or depth > 3:
        return None, None
    basis, kind = it.get("basis"), it.get("kind") or "point_event"
    off, cal = it.get("offset") or None, it.get("calendar") or None
    if basis == "explicit_date":
        value = _checked_date(it)
        if value is None:
            return None, None
        w = _explicit(ref, value, kind)
        return w, _year_ambiguity(ref, w, _explicit(ref, value, _other_kind(kind)))
    if basis == "speech_relative":
        if cal:
            name = str(cal.get("name") or "").lower()
            if cal.get("unit") == "season" and cal.get("which") == "this" and name in _SEASONS:
                w = _season(ref, name, "this", kind=kind)
                return w, _year_ambiguity(ref, w, _season(ref, name, "this", kind=_other_kind(kind)))
            w = _calendar(ref, cal)
            return w, _two_options_of(cal, w)
        amount, sign = (_amount(off), _sign(off)) if off else (None, None)
        if amount is None or sign is None or off.get("unit") not in _TOL:
            return None, None
        c = _shift(ref, amount, off["unit"], sign)
        tol = (_TOL_APPROX if off.get("approx") else _TOL)[off["unit"]]
        return (c - timedelta(days=tol), c + timedelta(days=tol)), None
    if basis == "event_anchored":
        anchor = it.get("anchor") or {}
        anc = anchor.get("fact")
        if anc is None:
            w = _holiday_anchored(ref, anchor.get("event"), it.get("expression"), off, cal, kind)
            alt = _holiday_anchored(ref, anchor.get("event"), it.get("expression"), off, cal, _other_kind(kind))
            return w, (_year_ambiguity(ref, w, alt) or _two_options_of(cal, w))
        if isinstance(anc, bool) or not isinstance(anc, int) or anc == i:
            return None, None
        aw, anchor_ambiguity = _place(ref, items, anc, n_facts, depth + 1)
        if not aw or aw[0] is None or aw[1] is None:
            return None, None
        w = _from_anchor(aw, off, cal)
        if w is None:
            return None, None
        # a date derived from a two-candidate anchor is itself one of two candidates
        return w, ("ambiguous_anchor" if anchor_ambiguity else _two_options_of(cal, w))
    return None, None


def _sign(off: dict) -> int | None:
    return {"after": 1, "before": -1}.get(off.get("direction"))


def _amount(off: dict) -> float | None:
    """A finite, non-negative offset amount; anything else is unusable (never guessed)."""
    a = off.get("amount")
    if isinstance(a, bool):
        return None
    try:
        a = float(a)
    except (TypeError, ValueError):
        return None
    return a if math.isfinite(a) and a >= 0 else None


def _from_anchor(aw: tuple[date, date], off: dict | None, cal: dict | None):
    if cal:  # calendar unit relative to the anchor ("later that week", "the following month")
        w = _calendar(aw[0], cal)
        if not w:
            return None
        if cal.get("later"):
            s = aw[1] + timedelta(days=1)
            # "later that week" = the unit's days after the anchor; none left -> unplaceable
            return (s, w[1]) if w[1] >= s else None
        return w
    if not off:
        return None
    sign = _sign(off)
    if sign is None:
        return None
    if off.get("amount") is None:  # "before the trip" / "after the move": open interval
        return (aw[1], None) if sign > 0 else (None, aw[0])
    amount, unit = _amount(off), off.get("unit")
    if amount is None or unit not in _TOL:  # e.g. hours: keep Graphiti's timestamp
        return None
    tol = (_TOL_APPROX if off.get("approx") else _TOL)[unit]
    s, e = _shift(aw[0], amount, unit, sign), _shift(aw[1], amount, unit, sign)
    return s - timedelta(days=tol), e + timedelta(days=tol)


_HOLIDAY_WHICH = {"last": "last", "this past": "last", "next": "next", "this coming": "next", "this": "this"}
_WHICH_RX = "this past|this coming|last|next|this"


def _holiday_ref(event, expression) -> tuple[str, int, str | None, int | None] | None:
    """(holidays.US name, days after it, last/next/this or None, year or None) for an anchor event.

    "next Black Friday" / "Christmas 2022" in the event; else the same words around the holiday
    name in the expression ("a week before last Christmas").
    """
    m = re.fullmatch(rf"(?:({_WHICH_RX}) )?(.+?)(?:,? (\d{{4}}))?", _norm_event(event))
    if not m or m.group(2) not in _HOLIDAY_ALIASES:
        return None
    which, alias, year = m.groups()
    if which is None and year is None:
        expr = _norm_event(expression)
        w = re.search(rf"\b({_WHICH_RX}) {re.escape(alias)}\b", expr)
        y = re.search(rf"\b{re.escape(alias)},? (\d{{4}})\b", expr)
        which, year = (w.group(1) if w else None), (y.group(1) if y else None)
    name, days_after = _HOLIDAY_ALIASES[alias]
    return name, days_after, _HOLIDAY_WHICH.get(which) if which else None, int(year) if year else None


def _holiday_anchored(ref: date, event, expression, off: dict | None, cal: dict | None, kind: str):
    """Window for an offset from a named US holiday ("a week before Black Friday").

    "last"/"next"/"this" or a year pick the occurrence (last = latest before the speech date,
    next = earliest after it, this = the speech date's year). Otherwise the year comes from the
    speech date: the latest occurrence whose window starts on or before it, or for a plan the
    earliest whose window ends on or after it.
    """
    ref_ = _holiday_ref(event, expression)
    if ref_ is None:
        return None
    name, days_after, which, year = ref_
    days = []
    for y in (year,) if year else (ref.year - 1, ref.year, ref.year + 1):
        d = _us_holidays(y).get(name)
        if d is not None:
            days.append(d + timedelta(days=days_after))
    if which == "last":
        days = [d for d in days if d < ref][-1:]
    elif which == "next":
        days = [d for d in days if d > ref][:1]
    elif which == "this":
        days = [d for d in days if d.year == ref.year]
    wins = [w for w in (_from_anchor((d, d), off, cal) for d in days) if w]
    if year or which:
        return wins[0] if wins else None
    if kind == "plan":
        return next((w for w in wins if (w[1] or w[0]) >= ref), None)
    return next((w for w in reversed(wins) if (w[0] or w[1]) <= ref), None)


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


def _fact_index(value) -> int | None:
    """A non-negative integer index (or its digit string); floats, bools and others are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_items(content: str) -> tuple[dict[int, dict], list]:
    """Resolver items keyed by fact index, and ``missing_events``. Raises on unparseable output."""
    out = parse_output(content)
    if not isinstance(out, dict):
        raise ValueError("resolver output is not a JSON object")
    items: dict[int, dict] = {}
    duplicated: set[int] = set()
    for it in out.get("facts") or []:
        if not isinstance(it, dict):
            continue
        i = _fact_index(it.get("i"))
        if i is None:
            continue
        if i in items:
            duplicated.add(i)  # two items for one fact: neither is trusted
        items[i] = it
    for i in duplicated:
        del items[i]
    missing = out.get("missing_events") or []
    return items, missing if isinstance(missing, list) else []


def granularity_of(item: dict | None) -> str | None:
    if not item:
        return None
    basis = item.get("basis")
    if basis == "explicit_date":
        v = _checked_date(item) or ""
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


def _graphiti_dates(value: datetime, speech_tz: tzinfo | None) -> set[date]:
    """Graphiti's value as a calendar date in UTC, in its own offset, and in the speech timezone.

    Graphiti stores UTC, so speech at 23:00-05:00 comes back dated the next day in UTC.
    """
    dates = {_utc_date(value), value.date()}
    if speech_tz is not None:
        dates.add(_aware(value).astimezone(speech_tz).date())
    return dates


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
    #: P2 persist step outcome (anchored_time_persist): "" not attempted, else ok/error/no_rows/...
    persist: str = ""
    persisted: int = 0
    #: The add_episode invocation that produced this report (graphiti_extraction_policy
    #: anchored_time_owner); only that invocation may persist it.
    owner: str | None = None


def _two_options(cal: dict | None, start: date, end: date) -> bool:
    """"last Tue" said Wed/Thu, or "next Fri": two candidate days, and the midpoint is neither."""
    return (bool(cal) and cal.get("unit") == "weekday" and cal.get("which") in ("last", "next")
            and (end - start).days == 7)


def plan_overlay(
    edges: Sequence[EdgeTimeInput],
    items: dict[int, dict],
    speech_date: date,
    speech_tz: tzinfo | None = None,
) -> list[AnchoredTimeResult]:
    """Decide the ``valid_at`` overlay for every edge. Pure: same inputs, same decisions.

    ``valid_at`` = window midpoint at 00:00 UTC, written only when the basis is dated, the window
    is closed, the fact is not a plan, Graphiti's value is missing or its speech-date default and
    outside the window, neither the window nor any anchor it derives from is two candidate days,
    every fact index and anchor is within ``edges``, and the edge's ``invalid_at`` would
    stay after it. Everything else keeps
    Graphiti's value: a date Graphiti resolved itself is never overridden.
    """
    results: list[AnchoredTimeResult] = []
    for i, edge in enumerate(edges):
        item = items.get(i) or {}
        basis = str(item.get("basis") or "none")
        kind = str(item.get("kind") or "point_event")
        window, ambiguity = (_place(speech_date, items, i, len(edges)) if basis in DATED_BASES
                             else (None, None))
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
            g_dates = _graphiti_dates(edge.valid_at, speech_tz) if edge.valid_at is not None else set()
            if any(start <= d <= end for d in g_dates):
                reason = "graphiti_inside_window"
            elif g_dates and speech_date not in g_dates:
                # Offline over 18 runs: overriding Graphiti's own dates only ever lost facts.
                reason = "graphiti_resolved"
            elif ambiguity:  # "two_options", "year_ambiguous", or "ambiguous_anchor" from an anchor
                reason = ambiguity
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
