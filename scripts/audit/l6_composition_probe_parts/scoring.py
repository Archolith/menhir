"""Scoring passes for the L6 composition probe (passes 1b-5).

Moved verbatim from ``l6_composition_probe.py``; re-exported through the parts package.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .corpus import CATEGORIES, LOSSY_BOUNDARIES, WEAKENING_PARAMS
from .indexing import TESTS, CallSite, FuncDef, _rel, iter_py


# --------------------------------------------------------------------------------------
# Pass 1b -- close the control-signal set over the call graph.
#
# "Can this callee deliver a control signal?" is a property of the call graph, not of one
# function's text. The signal originates in a callback, passes through the emitter, and the
# audit spec is explicit that every frame between the raiser and the actor must name it --
# "not just the first". A frame that shields a delivering callee behind a blanket
# `except Exception` STOPS the signal there, which is both why it does not propagate further
# and why that call site is the finding.
# --------------------------------------------------------------------------------------

def propagate_control_signals(defs: list[FuncDef]) -> None:
    by_name: dict[str, list[FuncDef]] = defaultdict(list)
    for fd in defs:
        by_name[fd.name].append(fd)

    # Propagate ONLY through names that resolve to a single definition. Callee resolution
    # here is by name, and an unbounded closure over that graph is worthless: the first run
    # of this probe attributed `SagaOwnershipRevoked` to the LLM usage emitter, because the
    # emitter calls `.get()` and one of the six unrelated `get` definitions reaches the Neo4j
    # driver. A speculative edge does not become evidence by being walked twice.
    #
    # Direct calls to an ambiguously-named raiser (`execute`) are NOT lost -- they are picked
    # up at the call site as AMBIGUOUS_CALLEE, and scored lower, because that is what they
    # are: a call that MIGHT be the raiser.
    unique = {name: fds[0] for name, fds in by_name.items() if len(fds) == 1}

    changed = True
    rounds = 0
    while changed and rounds < 20:  # the graph is shallow; the bound is a safety net
        changed = False
        rounds += 1
        for fd in defs:
            for callee in fd.callees - fd.shielded_callees:
                target = unique.get(callee)
                if target is None or target is fd:
                    continue
                gained = target.delivers - fd.delivers
                if gained:
                    fd.delivers |= gained
                    changed = True


# --------------------------------------------------------------------------------------
# Pass 2 -- classify the corpus.
# --------------------------------------------------------------------------------------

def classify(fd: FuncDef) -> None:
    # A frame that can deliver a control signal is consequential by construction, whatever
    # it is named: the whole point of the signal is to affect its caller.
    if fd.delivers:
        fd.categories.add("control_signal")
    params = set(fd.params) | set(fd.kwonly)
    for category, spec in CATEGORIES.items():
        by_param = bool(params & spec["params"])          # type: ignore[operator]
        by_name = bool(spec["name"].search(fd.name))      # type: ignore[union-attr]
        by_body = bool(spec["body"].search(fd.code_src))  # type: ignore[union-attr]
        if by_param or by_name:
            fd.categories.add(category)
        elif by_body:
            fd.categories.add(category)
            fd.body_only = True


# --------------------------------------------------------------------------------------
# Pass 3 -- what do the tests actually name?
#
# A name reference in a test file is a weak proxy for execution, and is treated as one: it
# can only ever DOWNGRADE a candidate (the tests do mention this caller, so look elsewhere
# first). It never promotes anything to safe.
# --------------------------------------------------------------------------------------

def index_tests() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = defaultdict(set)
    for path in iter_py(TESTS):
        rel = _rel(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                refs[node.id].add(rel)
            elif isinstance(node, ast.Attribute):
                refs[node.attr].add(rel)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # patch("...path.to.symbol") and monkeypatch string targets
                for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                    refs[part].add(rel)
    return refs


# --------------------------------------------------------------------------------------
# Pass 4 -- score (helper, caller) pairs against the L6 questions.
# --------------------------------------------------------------------------------------

@dataclass
class Pair:
    helper: FuncDef
    site: CallSite
    signals: list[str] = field(default_factory=list)
    score: int = 0
    helper_tests: set[str] = field(default_factory=set)
    caller_tests: set[str] = field(default_factory=set)


def analyse(defs: list[FuncDef], calls: list[CallSite],
            test_refs: dict[str, set[str]]) -> list[Pair]:
    by_name: dict[str, list[FuncDef]] = defaultdict(list)
    for fd in defs:
        by_name[fd.name].append(fd)

    calls_by_callee: dict[str, list[CallSite]] = defaultdict(list)
    for site in calls:
        calls_by_callee[site.callee].append(site)

    pairs: list[Pair] = []
    for name, candidates in by_name.items():
        corpus = [fd for fd in candidates if fd.categories]
        if not corpus:
            continue
        helper = max(corpus, key=lambda f: len(f.categories))
        # `link_episode_admission` has three definitions across three layers; a call to that
        # name might reach any of them. Say so rather than implying the probe resolved it.
        ambiguous = len(candidates) > 1
        weakening = ((set(helper.params) | set(helper.kwonly)) & WEAKENING_PARAMS)
        optional_weakening = weakening & helper.defaults_none

        helper_tests = test_refs.get(name, set())

        for site in calls_by_callee.get(name, []):
            # A recursive/self call inside the helper's own definition is not a caller.
            if site.file == helper.file and site.enclosing == helper.name:
                continue

            pair = Pair(helper=helper, site=site, helper_tests=helper_tests)
            pair.caller_tests = test_refs.get(site.enclosing, set())

            # Q1 -- does the caller choose an argument that can disable the helper?
            omitted = optional_weakening - site.kwargs
            # Positional args may supply the first N params; be conservative and drop them.
            if site.n_positional:
                omitted -= set(helper.params[:site.n_positional])
            has_value = {p for p in omitted if p in site.names_in_scope}
            if has_value:
                pair.signals.append(
                    f"ARGUMENT_GAP: caller omits {sorted(has_value)} but has the name in scope")
                pair.score += 4
            elif omitted:
                pair.signals.append(f"ARGUMENT_ABSENT: {sorted(omitted)} not passed")
                pair.score += 1
            if site.none_kwargs & weakening:
                pair.signals.append(
                    f"EXPLICIT_NONE: {sorted(site.none_kwargs & weakening)} passed as None")
                pair.score += 3
            if site.star_kwargs and weakening:
                pair.signals.append("STAR_KWARGS: forwarded **kwargs, not statically checkable")
                pair.score += 2

            # Q2 -- is a verdict returned and ignored?
            if site.discarded and helper.returns in {"bool", "bool | None", "Optional[bool]"}:
                pair.signals.append("DISCARDED_VERDICT: bool return dropped at a bare call")
                pair.score += 3

            # Q3 -- can a deliberate control signal cross a blanket handler here?
            if site.in_blanket_except and helper.delivers:
                if helper.seeded:
                    origin, weight = "raises/re-raises", 4
                else:
                    origin, weight = "propagates (transitive)", 2
                note = " AMBIGUOUS_CALLEE" if ambiguous else ""
                pair.signals.append(
                    f"SWALLOWED_SIGNAL:{note} callee {origin} {sorted(helper.delivers)}; "
                    f"call sits inside a blanket except Exception")
                pair.score += weight if not ambiguous else max(weight - 2, 1)

            # Q5 -- is the helper tested while the caller that composes it is not?
            if helper_tests and not pair.caller_tests:
                pair.signals.append(
                    f"CALLER_UNTESTED: helper named in {len(helper_tests)} test file(s); "
                    f"caller `{site.enclosing}` in none")
                pair.score += 3
            elif not helper_tests and not pair.caller_tests:
                pair.signals.append("BOTH_UNTESTED: neither helper nor caller named in tests")
                pair.score += 2

            if pair.signals:
                pairs.append(pair)

    pairs.sort(key=lambda p: (-p.score, p.helper.name, p.site.loc))
    return pairs


# --------------------------------------------------------------------------------------
# Pass 5 -- context boundaries (the ContextVar half of Q4, reported separately because it
# is a property of the BOUNDARY, not of any one pair).
# --------------------------------------------------------------------------------------

def context_boundaries(module_src: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for rel, source in module_src.items():
        for lineno, line in enumerate(source.splitlines(), start=1):
            if LOSSY_BOUNDARIES.search(line) and "to_thread" not in line:
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits
