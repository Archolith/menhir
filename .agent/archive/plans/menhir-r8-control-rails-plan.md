# R8 Control Rails — Guard 4 + Guard 7 Implementation Plan

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Both guards code-complete and wired: Guard 4
> `domain/diversity.py` (`family_distribution`/`dominance`/`normalized_entropy`/`is_collapsed`/
> `diversify`; tests `tests/domain/test_diversity.py`; wired `recall_service.py:724`) and Guard 7
> `domain/warden.py:247` `ContradictionWarden` (wired `recall_service.py:568,703`). Both ship
> **default-off** (`frontier_diversity_gate`/`frontier_contradiction_interrupt` = False) — tracked
> in `.agent/default-off-features.md`. Owner approved archiving as code-complete: default-off is a
> deferred activation decision, not unfinished plan work.

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the R8 SelfReinforcementGuard rail by adding the two guards that are still
missing — Guard 4 (RetrievalDiversityGate, set-level) and Guard 7 (ContradictionInterrupt,
assertion-side) — following the established pure-domain + warden pattern, bench-gated and
default-off.

**Architecture:** Guard 4 is a deterministic **set-level post-combiner gate** (a new pure module
`domain/diversity.py`) called from `recall_service._apply_frontier` after the oracle-ranking
reorder, behind a new `enable_diversity_gate` flag. Guard 7 is a new **per-candidate
`ContradictionWarden`** in `domain/warden.py`, appended to the `AssertionPipeline` warden chain
behind a new `enable_contradiction_interrupt` flag; it REFUSES a contradicted candidate under
current intent and FLAGs it `conflict` otherwise (the Guard-1 productive-touch heat-freeze is
explicitly **deferred** — assertion-side only).

**Tech stack:** Python 3.12+, stdlib only for the domain modules (frozen dataclasses, enums,
pure functions), pytest for unit tests. No graph access in the new domain code.

## Global Constraints

- **Bench-first / gated (chain-handoff §8):** no guard graduates `speculative -> supported-by-eval`
  without an archolith-bench artifact. Both new portions ship **default-off**; with no
  `MENHIR_FRONTIER_*` env set, recall is byte-for-byte today's behavior.
- **Determinism (retrieval-control-rails.md non-goals):** control rails MUST NOT change ranking
  nondeterministically. Guard 4's reorder is a stable, fully deterministic transform.
- **Rule zero:** retrieval is evidence of attention, not truth; only external/productive outcomes
  increase durable heat. Guard 7 does not promote truth; it suppresses a contradicted *current*
  assertion and preserves historical access.
- **Pure domain:** `domain/diversity.py` and the warden take pre-labeled inputs (families,
  signals); menhir-specific derivation lives at the call site, mirroring how `exhaustion.py`
  takes `RetrievalStats` and `warden.py` reads `WardenContext`.
- **Tests static:** do not modify existing guard/warden tests; only add new ones.

---

## Corrected R8 status (the reframing — read first)

The chain-handoff §3 ("only `exhaustion.py` = Guard 6") and the research-vs-shipped re-audit
checklist ("control rails R8? ... today: only exhaustion.py") are **stale**. Current code already
implements most of the SelfReinforcementGuard set:

| Guard | State | Where |
|---|---|---|
| 1 ProductiveTouchGate | DONE | `domain/self_reinforcement.py` (`resolve_touch`, `durable_heat_allowed`) |
| 2 SyntheticSupportCap | DONE | `self_reinforcement.is_synthetic_only` -> `warden.EvidenceAnchorWarden` |
| 3 MetaMemoryDepthBudget | DONE | `self_reinforcement.within_meta_depth_budget` -> warden FLAG |
| 5 EvidenceAnchorGate | DONE | `self_reinforcement.has_external_anchor` -> `warden.EvidenceAnchorWarden` |
| 6 RetrievalExhaustionPenalty | DONE | `domain/exhaustion.py` -> `warden.ExhaustionWarden` |
| **4 RetrievalDiversityGate** | **MISSING** | this plan, Task 1 + 3 |
| **7 ContradictionInterrupt** | **PARTIAL** (conflict already FLAGged by `OracleAdmissionWarden`/`CurrentnessWarden`; interrupt behavior absent) | this plan, Task 2 + 4 |

So R8's remaining surface is exactly two guards, not "1-5,7". Task 6 fixes the two stale docs.

## File Structure

- Create: `src/menhir/domain/diversity.py` — Guard 4 pure set-level functions.
- Create: `tests/test_diversity.py` — Guard 4 unit tests.
- Modify: `src/menhir/domain/warden.py` — add `ContradictionWarden` (Guard 7).
- Modify: `tests/test_warden.py` (or the existing warden test module) — add `ContradictionWarden` tests.
- Modify: `src/menhir/domain/retrieval_tuning.py` — add `enable_diversity_gate`,
  `enable_contradiction_interrupt` to `RetrievalTuningConfig`.
- Modify: `src/menhir/config/settings.py` — add `frontier_diversity_gate`,
  `frontier_contradiction_interrupt` + their `MENHIR_FRONTIER_*` env reads + map them in
  `retrieval_tuning()`.
- Modify: `src/menhir/services/assertion_pipeline.py` — `AssertionPipeline.__init__` gains
  `contradiction_interrupt: bool = False` that appends `ContradictionWarden` to the default chain.
- Modify: `src/menhir/services/recall_service.py` — `_apply_frontier` calls `diversify()` after the
  oracle-ranking reorder when `enable_diversity_gate`; passes `contradiction_interrupt` into the
  `AssertionPipeline`.
- Contract only (archolith-bench, NOT in this repo): a CE-willow self-reinforcement fixture +
  metrics + promotion gate (Task 5).

---

### Task 1: Guard 4 — `domain/diversity.py` (set-level diversity gate)

**Files:**
- Create: `src/menhir/domain/diversity.py`
- Test: `tests/test_diversity.py`

**Interfaces:**
- Produces: `family_distribution(families) -> dict[str,int]`, `dominance(families) -> float`,
  `normalized_entropy(families) -> float`, `is_collapsed(families, *, dominance_max, entropy_min)
  -> bool`, `diversify(items, family_of, *, max_per_family, dominance_max, entropy_min) -> list`.
  Generic over the item type `T`; `family_of: Callable[[T], str]` is supplied by the caller.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_diversity.py
from menhir.domain.diversity import (
    dominance, normalized_entropy, is_collapsed, diversify,
)

def test_dominance_and_entropy():
    fams = ["a", "a", "a", "a"]
    assert dominance(fams) == 1.0
    assert normalized_entropy(fams) == 0.0
    mixed = ["a", "b", "c", "d"]
    assert dominance(mixed) == 0.25
    assert normalized_entropy(mixed) == 1.0  # uniform over 4 -> max entropy

def test_is_collapsed_triggers_on_dominance_or_low_entropy():
    assert is_collapsed(["a","a","a","b"], dominance_max=0.6, entropy_min=0.5) is True
    assert is_collapsed(["a","b","c","d"], dominance_max=0.6, entropy_min=0.5) is False

def test_diversify_noop_when_not_collapsed():
    items = [("m1","a"), ("m2","b"), ("m3","c")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=2,
                    dominance_max=0.6, entropy_min=0.5)
    assert out == items  # unchanged, stable

def test_diversify_interleaves_deterministically_when_collapsed():
    # 4 of family a, 1 of b: collapsed -> round-robin by family, within-family order kept,
    # families ordered by first appearance. Deterministic.
    items = [("a1","a"),("a2","a"),("a3","a"),("a4","a"),("b1","b")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=99,
                    dominance_max=0.6, entropy_min=0.9)
    assert out == [("a1","a"),("b1","b"),("a2","a"),("a3","a"),("a4","a")]

def test_diversify_is_pure_stable_permutation():
    items = [("a1","a"),("a2","a"),("b1","b"),("c1","c")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=99,
                    dominance_max=0.0, entropy_min=1.1)  # force collapsed
    assert sorted(out) == sorted(items)  # same multiset, reordered only
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_diversity.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'menhir.domain.diversity'`.

- [ ] **Step 3: Write the implementation**

```python
"""Retrieval diversity gate (R8 / retrieval-control-rails.md Guard 4).

Set-level anti-spiral rail: when the top-of-list collapses onto one evidence family
(top_memory_dominance high or retrieval_entropy low), interleave families so one semantic
cluster cannot monopolize the context window. Unlike the per-candidate Wardens this reasons
over the whole ranked SET, so it lives here as a pure list->list transform and is called from
recall after the combiner, not inside the warden chain.

Determinism (control-rails non-goal: rails must not reorder nondeterministically): when NOT
collapsed the input is returned unchanged; when collapsed the reorder is a stable round-robin
by family, families ordered by first appearance, within-family order preserved. Same input ->
same output, always.
"""

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

# Trigger defaults (transparent starting point; the bench tunes them).
_DEFAULT_DOMINANCE_MAX = 0.6   # one family may hold up to 60% of the window before we act
_DEFAULT_ENTROPY_MIN = 0.5     # normalized family entropy below this is "collapsed"
_DEFAULT_MAX_PER_FAMILY = 3    # cap any single family's run in the interleave


def family_distribution(families: Sequence[str]) -> dict[str, int]:
    """Count candidates per evidence family (insertion order preserved)."""
    counts: OrderedDict[str, int] = OrderedDict()
    for fam in families:
        counts[fam] = counts.get(fam, 0) + 1
    return dict(counts)


def dominance(families: Sequence[str]) -> float:
    """Fraction of the set held by the single most-represented family (0 when empty)."""
    if not families:
        return 0.0
    counts = Counter(families)
    return max(counts.values()) / len(families)


def normalized_entropy(families: Sequence[str]) -> float:
    """Shannon entropy of the family distribution, normalized to [0, 1].

    1.0 = perfectly uniform across the families present; 0.0 = one family. A single distinct
    family (or empty) returns 0.0 (no diversity)."""
    if not families:
        return 0.0
    counts = Counter(families)
    if len(counts) <= 1:
        return 0.0
    total = len(families)
    h = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return h / math.log2(len(counts))


def is_collapsed(
    families: Sequence[str],
    *,
    dominance_max: float = _DEFAULT_DOMINANCE_MAX,
    entropy_min: float = _DEFAULT_ENTROPY_MIN,
) -> bool:
    """Guard 4 trigger: dominance over the cap OR entropy under the floor."""
    if not families:
        return False
    return dominance(families) > dominance_max or normalized_entropy(families) < entropy_min


def diversify(
    items: Sequence[T],
    family_of: Callable[[T], str],
    *,
    max_per_family: int = _DEFAULT_MAX_PER_FAMILY,
    dominance_max: float = _DEFAULT_DOMINANCE_MAX,
    entropy_min: float = _DEFAULT_ENTROPY_MIN,
) -> list[T]:
    """Return ``items`` reordered for family diversity, or unchanged if not collapsed.

    Deterministic, stable: families are queued in first-appearance order; the result is a
    round-robin draw across those queues (at most ``max_per_family`` consecutive from one
    family), preserving each family's internal order. A pure permutation of the input."""
    items = list(items)
    families = [family_of(it) for it in items]
    if not is_collapsed(families, dominance_max=dominance_max, entropy_min=entropy_min):
        return items

    queues: OrderedDict[str, list[T]] = OrderedDict()
    for it, fam in zip(items, families):
        queues.setdefault(fam, []).append(it)

    out: list[T] = []
    run_family: str | None = None
    run_len = 0
    while queues:
        progressed = False
        for fam in list(queues.keys()):
            if fam == run_family and run_len >= max_per_family:
                continue
            q = queues[fam]
            out.append(q.pop(0))
            if not q:
                del queues[fam]
            run_len = run_len + 1 if fam == run_family else 1
            run_family = fam
            progressed = True
        if not progressed:  # only remaining family is over its run cap -> drain it
            fam, q = next(iter(queues.items()))
            out.extend(q)
            del queues[fam]
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_diversity.py -q`
Expected: PASS (5 tests). Then `ruff check src/menhir/domain/diversity.py tests/test_diversity.py`.

- [ ] **Step 5: Commit**

```bash
git add src/menhir/domain/diversity.py tests/test_diversity.py
git commit -m "feat(r8): Guard 4 RetrievalDiversityGate (set-level, pure domain)"
```

---

### Task 2: Guard 7 — `ContradictionWarden` (assertion-side interrupt)

**Files:**
- Modify: `src/menhir/domain/warden.py` (add `ContradictionWarden`)
- Test: `tests/test_warden.py` (add cases; do not edit existing ones)

**Interfaces:**
- Consumes: `WardenContext` (`belief_score`, `oracle_packet`, `intent`) — already defined.
- Produces: `ContradictionWarden` (a `Warden`): REFUSE under `QueryIntent.CURRENT` when a
  contradiction is present; FLAG `conflict` otherwise; ADMIT when no contradiction.

**Boundary note (no contortion):** `OracleAdmissionWarden` already FLAGs oracle conflict and
`CurrentnessWarden` FLAGs `CONFLICT_SET`. `ContradictionWarden` is *stricter* (REFUSE on current
intent). The chain is most-restrictive-wins, so coexistence is correct without editing the other
wardens — REFUSE simply beats their FLAG, and under historical intent all three agree on FLAG
`conflict` (idempotent label). The Guard-1 productive-touch heat-freeze is **out of scope** (it
lives on the mutator/write side, which is not wired into recall yet).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_warden.py (append)
from menhir.domain.warden import ContradictionWarden, WardenContext, WardenDecision
from menhir.domain.belief import QueryIntent, RecallBucket, BeliefScore
from menhir.domain.oracles import OraclePacket, OracleResult, OracleTarget, OraclePolarity

def _packet_with_conflict():
    r = OracleResult(target=OracleTarget.CONFLICT, polarity=OraclePolarity.CONTRADICT, score=1.0)
    return OraclePacket(results=(r,), role_logits={}, combined_probability=0.5)

def test_contradiction_refuses_under_current_intent():
    w = ContradictionWarden()
    v = w.evaluate(WardenContext(candidate_id="m1", intent=QueryIntent.CURRENT,
                                 oracle_packet=_packet_with_conflict()))
    assert v.decision is WardenDecision.REFUSE

def test_contradiction_flags_under_historical_intent():
    w = ContradictionWarden()
    v = w.evaluate(WardenContext(candidate_id="m1", intent=QueryIntent.HISTORICAL,
                                 oracle_packet=_packet_with_conflict()))
    assert v.decision is WardenDecision.FLAG
    assert v.label == "conflict"

def test_no_contradiction_admits():
    w = ContradictionWarden()
    v = w.evaluate(WardenContext(candidate_id="m1", intent=QueryIntent.CURRENT))
    assert v.decision is WardenDecision.ADMIT
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_warden.py -q -k Contradiction`
Expected: FAIL with `ImportError: cannot import name 'ContradictionWarden'`.
(If `OraclePacket`/`OracleResult` constructor kwargs differ, fix the test helper to match
`domain/oracles.py` — confirm the real signature first; this is the one spike in this task.)

- [ ] **Step 3: Implement `ContradictionWarden`** (add after `OracleAdmissionWarden`)

```python
@dataclass
class ContradictionWarden:
    """Guard 7 (ContradictionInterrupt): a contradiction interrupts CURRENT assertion.

    Contradiction (oracle CONFLICT/CONTRADICT, or belief CONFLICT_SET bucket) means the
    candidate must not enter current-truth context until resolved -> REFUSE under current
    intent; historical intent still gets it, labeled 'conflict' (history is preserved). This
    is stricter than OracleAdmissionWarden's FLAG; most-restrictive-wins makes the REFUSE win
    when both fire. Assertion-side only -- the Guard-1 heat-freeze is deferred (mutator side).
    """

    name: str = "contradiction"

    def _contradicted(self, ctx: WardenContext) -> bool:
        if ctx.belief_score is not None and ctx.belief_score.bucket is RecallBucket.CONFLICT_SET:
            return True
        packet = ctx.oracle_packet
        if packet is not None:
            return any(
                r.target is OracleTarget.CONFLICT and r.polarity is OraclePolarity.CONTRADICT
                for r in packet.results
            )
        return False

    def evaluate(self, ctx: WardenContext) -> WardenVerdict:
        if not self._contradicted(ctx):
            return WardenVerdict(WardenDecision.ADMIT, self.name, reason="no contradiction")
        if ctx.intent is QueryIntent.CURRENT:
            return WardenVerdict(WardenDecision.REFUSE, self.name,
                                 reason="contradiction interrupts current assertion")
        return WardenVerdict(WardenDecision.FLAG, self.name,
                             reason="contradiction under historical intent", label="conflict")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_warden.py -q` (all warden tests, existing + new)
Expected: PASS. Then `ruff check src/menhir/domain/warden.py`.

- [ ] **Step 5: Commit**

```bash
git add src/menhir/domain/warden.py tests/test_warden.py
git commit -m "feat(r8): Guard 7 ContradictionWarden (assertion-side interrupt)"
```

---

### Task 3: Wire Guard 4 into the frontier path (gated, default-off)

**Files:**
- Modify: `src/menhir/domain/retrieval_tuning.py:90-108` (add flag)
- Modify: `src/menhir/config/settings.py:111-114,159-163,248-252` (field, env read, mapping)
- Modify: `src/menhir/services/recall_service.py:494-512` (call `diversify` after rank sort)

**Interfaces:**
- Consumes: `diversify` (Task 1), `RetrievalTuningConfig.enable_diversity_gate`.

- [ ] **Step 1:** Add `enable_diversity_gate: bool = False` to `RetrievalTuningConfig` (after
  `enable_warden_gate`), with a docstring line noting set-level, default-off.

- [ ] **Step 2:** In `settings.py`: add `frontier_diversity_gate: bool = False`; read
  `MENHIR_FRONTIER_DIVERSITY_GATE`; map `enable_diversity_gate=self.frontier_diversity_gate` in
  `retrieval_tuning()`.

- [ ] **Step 3:** In `_apply_frontier`, define a family mapping and apply `diversify` only when
  the flag is on, after the `enable_oracle_ranking` reorder:

```python
from menhir.domain.diversity import diversify

# evidence_kind -> diversity family (transparent; bench may refine)
_DIVERSITY_FAMILY = {
    "user": "user_log_test", "log": "user_log_test", "test": "user_log_test",
    "git": "git_temporal", "timestamp": "git_temporal",
    "file": "structure", "external": "external", "manual": "external",
    "agent_inference": "synthetic", "llm_summary": "synthetic",
    "retrieval_trace": "synthetic", "memory_call_hint": "synthetic",
}

def _family_for(uuid: str) -> str:
    kinds = metadata_by_uuid.get(uuid, {}).get("evidence_kinds") or ()
    for k in kinds:
        fam = _DIVERSITY_FAMILY.get(str(k))
        if fam:
            return fam
    return "semantic"
```

Then, after the `if tuning.enable_oracle_ranking:` reorder block and before/independent of the
warden-gate block:

```python
if tuning.enable_diversity_gate:
    result = diversify(result, family_of=lambda s: _family_for(s.uuid))
```

Add `"diversity_gate"` to the `portions` note list when `tuning.enable_diversity_gate`.

- [ ] **Step 4:** Verify the default-off invariant — with no env, `enable_diversity_gate` is
  False, so `result` is untouched. Run the recall_service tests:
  `python -m pytest tests/test_recall_service.py -q` (Expected: PASS, no behavior change).

- [ ] **Step 5: Commit**

```bash
git add src/menhir/domain/retrieval_tuning.py src/menhir/config/settings.py src/menhir/services/recall_service.py
git commit -m "feat(r8): wire Guard 4 diversity gate into _apply_frontier (default-off)"
```

---

### Task 4: Wire Guard 7 into the assertion pipeline (gated, default-off)

**Files:**
- Modify: `src/menhir/domain/retrieval_tuning.py` (add `enable_contradiction_interrupt`)
- Modify: `src/menhir/config/settings.py` (field + env + mapping, as Task 3)
- Modify: `src/menhir/services/assertion_pipeline.py:89-95` (constructor param)
- Modify: `src/menhir/services/recall_service.py:488-490` (pass the flag through)

- [ ] **Step 1:** Add `enable_contradiction_interrupt: bool = False` to `RetrievalTuningConfig`
  and `frontier_contradiction_interrupt` to settings (env `MENHIR_FRONTIER_CONTRADICTION_INTERRUPT`).

- [ ] **Step 2:** `AssertionPipeline.__init__` gains `contradiction_interrupt: bool = False`;
  when True and `wardens` is not explicitly supplied, append `ContradictionWarden()` to the
  default list:

```python
def __init__(self, combiner, *, executor=None, wardens=None,
             auto_intent=True, contradiction_interrupt=False):
    ...
    default = [ScopeWarden(), EvidenceAnchorWarden(), OracleAdmissionWarden()]
    if contradiction_interrupt:
        default.append(ContradictionWarden())
    self.wardens = wardens or default
    self.chain = WardenChain(self.wardens)
```

(import `ContradictionWarden` from `menhir.domain.warden`.)

- [ ] **Step 3:** In `_apply_frontier`, pass it through:

```python
pipeline = AssertionPipeline(
    LogSpaceOracleCombiner(),
    auto_intent=tuning.enable_intent_lens,
    contradiction_interrupt=tuning.enable_contradiction_interrupt,
)
```

- [ ] **Step 4:** Tests — add a pipeline test that a contradicted candidate lands in `refused`
  under current intent only when `contradiction_interrupt=True`; and that default (False)
  reproduces today's bucketing. Run:
  `python -m pytest tests/test_assertion_pipeline.py -q` (Expected: PASS).

- [ ] **Step 5: Commit**

```bash
git add src/menhir/domain/retrieval_tuning.py src/menhir/config/settings.py src/menhir/services/assertion_pipeline.py src/menhir/services/recall_service.py
git commit -m "feat(r8): wire Guard 7 ContradictionWarden into AssertionPipeline (default-off)"
```

---

### Task 5: Bench contract — CE-willow self-reinforcement fixture (archolith-bench)

**Not built in this repo.** archolith-bench is the falsification harness (chain-handoff §2); it
is not cloned in this session. This task is the **graduation contract** the bench must satisfy
before either flag flips on by default and before `retrieval-control-rails.md` moves to
`supported-by-eval`.

- [ ] Build the CE-willow fixture (E1-E6 from `retrieval-control-rails.md` "CE willow fixture"):
  texture-cache crash -> patch -> agent "patch fixed it" -> agent memory-call hint -> new
  evidence (load-order) -> load-order fix. A bad system keeps retrieving E3/E4 and asserts the
  patch fixed it; a good system retrieves E3/E4 as historical, surfaces E5/E6 for current truth,
  flags E4 as a stale memory-call hint.
- [ ] Metrics (research doc "Metrics"): `current_truth_accuracy`, `historical_preservation`,
  `stale_heat_leak`, `retrieval_entropy`, `context_mode_collapse_rate`, `synthetic_support_ratio`.
- [ ] Promotion gate: Guard 4 must lift `retrieval_entropy` / cut `context_mode_collapse_rate`
  with no `current_truth_accuracy` loss; Guard 7 must cut stale current-assertion of contradicted
  items while holding `historical_preservation`. A shuffle/ablation arm (guard off) must show the
  effect is the guard, not the fixture.
- [ ] Only on graduation: flip `MENHIR_FRONTIER_DIVERSITY_GATE` / `..._CONTRADICTION_INTERRUPT`
  defaults, and update `retrieval-control-rails.md` Status `speculative -> supported-by-eval`.

---

### Task 6: Reconcile the stale docs

**Files:**
- Modify: `.agent/plans/chain-handoff.md` (§3 ladder line "R8 ... planned" / "only exhaustion.py")
- Modify: `docs/research/process/research-vs-shipped-inventory.md` (re-audit checklist + Tier 3
  "control rails (R8)" line)

- [ ] Correct chain-handoff §3: R8 is no longer "planned (only Guard 6)" — Guards 1,2,3,5,6 are
  built (`self_reinforcement.py` + `exhaustion.py` + four wardens); only Guard 4 + Guard 7's
  interrupt remain (this plan).
- [ ] Correct the research-vs-shipped re-audit checklist line `control rails R8?` and the Tier 3
  `control rails (R8)` entry to match: guards 1-3,5,6 EXIST; 4 + 7-interrupt NEW.
- [ ] Refresh the chain-handoff "Last updated" line with the R8 reconciliation.
- [ ] Commit: `docs(r8): reconcile R8 guard status across handoff + inventory`.

---

## Self-Review

- **Spec coverage:** retrieval-control-rails.md Guards 4 and 7 are the only unbuilt SelfReinforcementGuard
  members (verified against `self_reinforcement.py`, `exhaustion.py`, `warden.py`); both are covered
  (Tasks 1-4), bench-gated (Task 5), and the stale docs reconciled (Task 6). CostAwareOracleScheduler
  (Object 1 of the doc) is a separate scheduler concern, intentionally **out of scope** for this rung.
- **Placeholder scan:** the one acknowledged spike is the exact `OraclePacket`/`OracleResult`
  constructor signature in Task 2's test helper — confirm against `domain/oracles.py` before
  writing the test. No other TBDs.
- **Type consistency:** `WardenDecision`/`WardenVerdict`/`WardenContext`/`QueryIntent`/`RecallBucket`/
  `OracleTarget`/`OraclePolarity` names match `warden.py`/`belief.py`/`oracles.py`. `diversify`'s
  `family_of` callable + `_family_for` call-site mapping are consistent. Flags
  `enable_diversity_gate`/`enable_contradiction_interrupt` are threaded settings -> tuning -> service
  identically to the existing `enable_warden_gate`.
- **Determinism:** `diversify` returns the input unchanged when not collapsed and a stable,
  first-appearance round-robin when collapsed — a pure permutation, satisfying the rails non-goal.
- **Default-off:** both new flags default False at every layer; with no env the recall path is
  unchanged (Task 3/4 Step 4 asserts this).
