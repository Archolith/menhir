"""Pure-Python verb-centric question rule (no spaCy): a positional question frame plus small word lists.

Frame:  [how many | which | what] <noun phrase> <aux> I <verb group> ...   (wh-question)
        <aux> I <verb group> ...                                           (yes/no question)
Verdict: VERB if >= 2 coordinated content verbs; else VERB if the asked noun is a generic category,
not compound / named, and the verb group has a content verb; else NOUN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_AUX = frozenset({"did", "do", "does", "have", "has", "had", "am", "was", "were", "will", "would", "can", "could"})
_WH = frozenset({"many", "which", "what", "much"})
_LIGHT = frozenset({"be", "been", "being", "am", "is", "are", "was", "were", "have", "has", "had", "do", "does",
                    "did", "get", "got", "make", "made", "go", "went", "take", "took", "spend", "spent", "want",
                    "need", "plan", "planned", "try", "tried", "use", "used", "remember", "mention", "mentioned",
                    "tell", "told", "say", "said", "know", "knew", "think", "thought", "start", "started",
                    "begin", "began", "keep", "kept", "own", "currently"})
_PASS_TO = frozenset({"need", "needed", "plan", "planned", "want", "wanted", "try", "tried", "learn", "learned",
                      "learnt", "start", "started", "begin", "began", "decide", "decided", "going", "have", "had",
                      "manage", "managed", "hope", "hoped"})  # "<verb> to <action>": the action is the real verb
_ADVERBS = frozenset({"currently", "recently", "ever", "also", "already", "actually", "still", "just", "mostly",
                      "most", "really", "finally", "first", "initially", "previously", "personally", "regularly",
                      "usually", "often", "together", "simultaneously", "successfully", "so", "far", "now", "not",
                      "never", "once", "again", "even"})
_PARTICLES = frozenset({"up", "out", "on", "off", "back", "down", "away", "over", "in", "into", "through", "around"})
_STRICT_PARTICLES = frozenset({"up", "out", "off", "back", "down", "away"})
_ENDERS = frozenset({"the", "a", "an", "my", "your", "our", "their", "his", "her", "its", "this", "that", "these",
                     "those", "some", "any", "all", "each", "every", "for", "from", "at", "since", "during", "with",
                     "by", "about", "before", "after", "between", "than", "to", "of", "in", "on", "last", "past",
                     "this", "including", "excluding", "while", "when", "where", "if", "because", "per", "across",
                     "?", "."})
_GENERIC = frozenset({"item", "items", "thing", "things", "stuff", "clothing", "clothes", "project", "projects",
                      "place", "places", "people", "person", "activity", "activities", "task", "tasks", "event",
                      "events", "object", "objects", "product", "products", "purchase", "purchases", "gift",
                      "gifts", "piece", "pieces", "type", "types", "kind", "kinds"})
_NOUN_MODIFIERS = frozenset({"different", "total", "other", "new", "more", "distinct", "separate", "of"})
_IRREGULAR = {"bought": "buy", "led": "lead", "made": "make", "got": "get", "took": "take", "went": "go",
              "ran": "run", "ate": "eat", "saw": "see", "read": "read", "sold": "sell", "built": "build",
              "brought": "bring", "caught": "catch", "taught": "teach", "found": "find", "gave": "give",
              "kept": "keep", "left": "leave", "lost": "lose", "met": "meet", "paid": "pay", "sent": "send",
              "spent": "spend", "won": "win", "wrote": "write", "written": "write", "drove": "drive",
              "driven": "drive", "flew": "fly", "flown": "fly", "rode": "ride", "ridden": "ride",
              "swam": "swim", "sang": "sing", "began": "begin", "begun": "begin", "drew": "draw",
              "grew": "grow", "knew": "know", "threw": "throw", "wore": "wear", "worn": "wear",
              "tore": "tear", "chose": "choose", "chosen": "choose", "broke": "break", "broken": "break",
              "fixed": "fix", "held": "hold", "heard": "hear", "lent": "lend", "borrowed": "borrow",
              "did": "do", "done": "do", "seen": "see", "taken": "take", "given": "give", "eaten": "eat",
              "gone": "go", "been": "be", "had": "have", "fed": "feed", "sat": "sit", "stood": "stand",
              "slept": "sleep", "felt": "feel", "dug": "dig", "hung": "hang", "shot": "shoot", "set": "set",
              "put": "put", "cut": "cut", "hit": "hit", "quit": "quit", "shut": "shut", "split": "split"}
_IRREGULAR_PAST: dict[str, set[str]] = {}
for _past, _base in _IRREGULAR.items():
    _IRREGULAR_PAST.setdefault(_base, set()).add(_past)

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*|[?,.]")
_SKIP_AUX = frozenset({"been", "be", "being", "am", "is", "are", "was", "were"})


@dataclass(frozen=True)
class VerbFacet:
    label: str
    forms: tuple[str, ...]


def _tokens(question: str) -> list[str]:
    return _TOKEN.findall(question)


def _lemma(word: str) -> str:
    w = word.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    for suffix, repl in (("ied", "y"), ("ies", "y")):
        if w.endswith(suffix) and len(w) > 4:
            return w[: -len(suffix)] + repl
    if w.endswith("ing") and len(w) > 5:
        stem = w[:-3]
        return stem[:-1] if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "lsz" else stem
    if w.endswith("ed") and len(w) > 4:
        stem = w[:-2]
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
            return stem[:-1]
        return stem if stem.endswith(("ck", "ll", "ss", "sh", "ch", "x", "w", "y")) or stem[-1] not in "cgsvz" \
            else stem + "e"
    return w


_VOWELS = "aeiou"


def _single_vowel_consonant_end(b: str) -> bool:
    """'stop', 'complet', 'bak': one vowel then one consonant (not w/x/y) at the end."""
    return (len(b) >= 3 and b[-1] not in _VOWELS + "wxy" and b[-2] in _VOWELS
            and b[-3] not in _VOWELS)


def _suffix_stripped(w: str) -> str | None:
    """The part before a regular -ed/-ing suffix, or None when the word has none."""
    if w in _IRREGULAR or w.endswith(("ied", "ies")):
        return None
    if w.endswith("ing") and len(w) > 5:
        return w[:-3]
    if w.endswith("ed") and len(w) > 4:
        return w[:-2]
    return None


def _doubled(stripped: str | None) -> bool:
    """'stopp' (stopped) doubled its final consonant; 'call' and 'dress' end that way anyway."""
    return (stripped is not None and len(stripped) > 2 and stripped[-1] == stripped[-2]
            and stripped[-1] not in "lsz")


def _bases(word: str) -> set[str]:
    """Candidate base forms. Suffix stripping cannot tell 'completed' (complete) from
    'returned' (return) or 'focused' (focus) from 'danced' (dance), so search both."""
    base = _lemma(word)
    stripped = _suffix_stripped(word.lower())
    if stripped is None or (len(stripped) > 2 and stripped[-1] == stripped[-2]):
        return {base}  # no regular suffix, or a double letter ("stopped", "called") that settles it
    if base.endswith("e"):
        return {base, base[:-1]}
    if _single_vowel_consonant_end(base) or (base[-1] in "cgsvz" and base[-2] not in _VOWELS + base[-1]):
        return {base, base + "e"}
    return {base}


def _inflections(base: str, *, double: bool) -> set[str]:
    stem = base[:-1] if base.endswith("e") else base
    out = {base, base + "s", stem + "ed", stem + "ing"}
    if base.endswith(("s", "x", "z", "sh", "ch")):
        out.add(base + "es")  # fixes, dresses
    if len(base) > 2 and base[-1] == "y" and base[-2] not in _VOWELS:
        out |= {base[:-1] + "ies", base[:-1] + "ied"}  # tries, tried
    if double:
        out |= {base + base[-1] + "ed", base + base[-1] + "ing"}  # stop -> stopped, stopping
    return out


def _forms(word: str, particle: str) -> list[str]:
    w = word.lower()
    stripped = _suffix_stripped(w)
    doubled = _doubled(stripped)
    words = {w} | _IRREGULAR_PAST.get(_lemma(word), set())
    for base in _bases(word):
        double = doubled or (stripped is None and w == base and _single_vowel_consonant_end(base))
        words |= _inflections(base, double=double and not base.endswith("e"))
    return sorted(f"{x} {particle}".strip() for x in words)


def _frame(toks: list[str]):
    """(asked noun words, raw words, index just after the subject "I") or None when not in the frame."""
    low = [t.lower() for t in toks]
    for i, w in enumerate(low):
        if w == "i" and i > 0 and low[i - 1] in _AUX:
            aux_i = i - 1
            wh = next((j for j in range(aux_i) if low[j] in _WH), None)
            if wh is not None:
                return low[wh + 1:aux_i], [t for t in toks[wh + 1:aux_i]], i + 1
            if aux_i == 0:
                return [], [], i + 1
            return None
    return None


def _verb_group(toks: list[str], start: int):
    """Verbs after the subject until the object starts. Returns [(verb word, particle)], joined flag."""
    low = [t.lower() for t in toks]
    verbs, pending, joined, i = [], False, False, start
    while i < len(low):
        w = low[i]
        if w in _ADVERBS or w in _SKIP_AUX:
            i += 1
            continue
        if w in ("or", "and", ","):
            if verbs:
                pending = True
            i += 1
            continue
        if w == "to" and verbs and verbs[-1][0].lower() in _PASS_TO:
            verbs.pop()  # "need to pick up": "need" is not the action
            pending = bool(verbs)  # "service or plan to service": keep the joiner before "plan"
            i += 1
            continue
        is_particle = (w in _PARTICLES and verbs and not verbs[-1][1] and not pending and i + 1 < len(low)
                       and (w in _STRICT_PARTICLES or low[i + 1] in ("or", "and", ",", "?", ".")
                            or low[i + 1] in _ADVERBS))
        if is_particle:
            verbs[-1] = (verbs[-1][0], w)
            i += 1
            continue
        if w in _ENDERS:
            break
        if toks[i][0].isupper():
            break  # a name or title starts the object
        if w[0].isalpha():
            if verbs and not pending:
                break  # two words in a row with no joiner: the second starts the object
            joined = joined or pending
            pending = False
            verbs.append((toks[i], ""))
            i += 1
            continue
        break
    return verbs, joined


def _analyse(question: str):
    toks = _tokens(question)
    fr = _frame(toks)
    if fr is None:
        return "NOUN", {"reason": "no frame", "verbs": [], "noun": ""}
    noun_low, _noun_raw, start = fr
    verbs, joined = _verb_group(toks, start)
    # A light verb with a particle is a real action ("tried out").
    content = [(v, p) for v, p in verbs if p or (v.lower() not in _LIGHT and _lemma(v) not in _LIGHT)]
    coord = content if joined and len(content) >= 2 else []
    if "of" in noun_low:
        k = noun_low.index("of")
        left, right = noun_low[:k], noun_low[k + 1:]
        heads = [h for h in (left[-1:] + right[-1:])]
        modifiers = left[:-1] + right[:-1]
    else:
        heads, modifiers = noun_low[-1:], noun_low[:-1]
    generic = bool(heads) and all(h in _GENERIC for h in heads)
    named = any(t[0].isupper() and t != "I" for t in toks[1:])
    compound = any(m not in _NOUN_MODIFIERS and "-" not in m for m in modifiers)
    if coord:
        verdict = "VERB"
    elif content and generic and not named and not compound:
        verdict = "VERB"
    else:
        verdict = "NOUN"
    return verdict, {"verbs": [f"{v} {p}".strip() for v, p in content], "coord": bool(coord),
                     "noun": " ".join(noun_low), "generic": generic}


def is_verb_centric(question: str) -> bool:
    return _analyse(question)[0] == "VERB"


def verb_facets(question: str) -> tuple[VerbFacet, ...]:
    if _analyse(question)[0] != "VERB":
        return ()
    toks = _tokens(question)
    _, _, start = _frame(toks)
    verbs, _ = _verb_group(toks, start)
    out: list[VerbFacet] = []
    seen: set[str] = set()
    for v, p in verbs:
        if v.lower() in _LIGHT or _lemma(v) in _LIGHT:
            continue
        label = f"{_lemma(v)} {p}".strip()
        if label not in seen:
            seen.add(label)
            out.append(VerbFacet(label=label, forms=tuple(_forms(v, p))))
    return tuple(out)
