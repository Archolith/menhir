# Belief-Gate Activation Implementation Plan

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Producer built and wired: `domain/belief_evidence.py`
> (`assemble_belief_evidence`/`score_candidate_belief`) feeds `AssertionPipeline` behind
> `belief_gate` (`assertion_pipeline.py:27,142-151`); `CurrentnessWarden` added to the chain
> (`recall_service.py:740-750`, requires `warden_gate`); env flag `MENHIR_FRONTIER_BELIEF_GATE`
> default-OFF (`settings.py:238`, tracked in `.agent/default-off-features.md`). Deferred items
> (git/structure staleness, richer provenance weighting) are the separate
> `menhir-belief-gate-git-staleness.md` plan, not unfinished work here. Owner approved archiving as
> code-complete; default-off is a deferred activation decision.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make menhir's already-written `CurrentnessWarden` functional by building the missing producer — a step that assembles `BeliefEvidence` and a `BeliefScore` per candidate and feeds them into the warden — then wire `CurrentnessWarden` into the assertion-pipeline chain behind a default-OFF feature flag, shadow-measured.

**Architecture:** The belief layer's decision logic is complete and tested: `CurrentnessWarden.evaluate()` reads `WardenContext.belief_score`, derives the currentness bucket by intent, and applies the full `_BUCKET_DECISION` map (ADMIT/FLAG/REFUSE). The gap is purely the producer: nothing populates `WardenContext.belief_score`/`.evidence`, so the warden always short-circuits to ADMIT "no belief score", and `CurrentnessWarden` is not in the default chain. This plan adds a pure adapter (`domain/belief_evidence.py`) mapping a recall candidate's metadata (provenance `evidence_kinds` + bi-temporal markers) to `BeliefEvidence` + a `BeliefScore` via the existing `BeliefScorer`, wires it into `AssertionPipeline` behind a `belief_gate` flag, plumbs a `MENHIR_FRONTIER_BELIEF_GATE` env flag (default OFF, mirroring the existing frontier flags), and sources the temporal markers from the rung1 fact fetch. Git/structure staleness evidence and richer provenance weighting are explicitly deferred (forward work).

**Tech Stack:** Python 3.12, pytest (`pythonpath = src`, `asyncio_mode = auto`), pure stdlib domain layer. No Neo4j: the producer is pure; pipeline/recall tests use stub candidates and a stubbed `graph_adapter`.

## Load-bearing design decisions (review these before implementing)

These choices are deliberate and reviewable — if any is wrong, fix the plan, not the code:

1. **Score only belief-bearing candidates.** `score_candidate_belief` returns `None` unless the candidate carries a temporal marker (`belief_superseded` or `belief_has_temporal`). Ordinary memories get no belief score, so `CurrentnessWarden` stays permissive (its built-in `score is None -> ADMIT`). The gate acts only where there is a real belief signal. This avoids forcing every memory through belief semantics built for CAUSE/FIX/REGRESSION/SUPERSESSION candidates.
2. **Candidate type:** `SUPERSESSION` when superseded, else `DEPENDENCY_STATE` (a neutral current state). No attempt to infer CAUSE/FIX/REGRESSION here — those need richer signals (forward).
3. **Evidence mapping is minimal and principled:** superseded -> `IS_EXPIRED` (CONTRADICTS); current temporal fact -> `IS_VALID_AT_QUERY_TIME` (SUPPORTS); each known provenance `evidence_kind` -> its external-anchor signal (SUPPORTS). `strength=1.0`; the scorer's `DEFAULT_SIGNAL_WEIGHTS` carry the per-signal weight.
4. **Git/structure staleness deferred:** `git_staleness.derive_structural_staleness` already returns `BeliefEvidence`, but it needs a `list[GitChange]` feed that is not modeled in the live recall path. Out of scope here; recorded as forward work in Task 5.

## Global Constraints

- **Default behavior is preserved.** With `MENHIR_FRONTIER_BELIEF_GATE` unset (default OFF), the warden chain and recall results are byte-for-byte identical to today: `CurrentnessWarden` is not appended and no belief scoring runs.
- **Permissive on absence.** A candidate with no belief signal yields no `BeliefScore`; `CurrentnessWarden` ADMITs it. Absence is never treated as superseded.
- **No live Neo4j.** Another process is ingesting into the live graph; all tests are pure/stub-based. Do not run `tests/test_recall_live.py` or any `online`/`replay`-marked test.
- **Reuse, do not reinvent.** Use the existing `BeliefScorer`, `BeliefEvidence`, `BeliefCandidate`, `currentness_bucket`, `is_superseded_belief`, `CurrentnessWarden`, and `_BUCKET_DECISION`. Do not duplicate their logic.
- **Mirror the existing frontier-flag pattern** (`enable_warden_gate` / `frontier_warden_gate` / `MENHIR_FRONTIER_WARDEN_GATE`) exactly for the new flag.
- Run tests from the repo root: `python -m pytest <path> -q`. The single `graphiti_core` Pydantic-V2 deprecation warning is third-party and benign.

## File Structure

- `src/menhir/domain/belief_evidence.py` (NEW) — pure adapter: candidate metadata -> `BeliefEvidence` + `BeliefScore`. The only new logic in the plan.
- `src/menhir/services/assertion_pipeline.py` — add `belief_gate` flag: append `CurrentnessWarden`, populate `WardenContext.belief_score`/`.evidence`.
- `src/menhir/domain/retrieval_tuning.py`, `src/menhir/config/settings.py` — the `enable_belief_gate` flag and its `MENHIR_FRONTIER_BELIEF_GATE` env mapping.
- `src/menhir/services/recall_service.py` — thread `belief_gate` into both `AssertionPipeline` construction sites; attach temporal markers to candidate metadata when the gate is on; include the flag in `frontier_active`.
- `docs/research/belief-temporal/belief-layer.md` — record the belief-gate status, the deferred git-staleness work, and the bench-activation gate.
- Tests: new flat files under `tests/` plus extensions to `tests/test_assertion_pipeline.py`.

---

### Task 1: Pure belief-evidence assembly + scoring adapter

**Files:**
- Create: `src/menhir/domain/belief_evidence.py`
- Test: `tests/test_belief_evidence.py`

**Interfaces:**
- Consumes: `from menhir.domain.belief import BeliefCandidate, BeliefCandidateType, BeliefEvidence, BeliefScore, BeliefScorer, EvidencePolarity, EvidenceSignal`. `BeliefScorer().score(candidate: BeliefCandidate, evidence, *, head=BeliefHead.SUPPORTED, prior=0.5) -> BeliefScore`; `BeliefEvidence(signal, polarity, strength, note, source_id)`; `BeliefCandidate(id, statement, candidate_type, touched_entities)`.
- Produces: `assemble_belief_evidence(metadata) -> tuple[BeliefEvidence, ...]` and `score_candidate_belief(candidate_id, content, metadata) -> BeliefScore | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_belief_evidence.py
"""Pure tests for the belief-evidence assembly adapter (belief-gate producer)."""
from __future__ import annotations

from menhir.domain.belief import EvidencePolarity, EvidenceSignal
from menhir.domain.belief_evidence import assemble_belief_evidence, score_candidate_belief


def test_no_temporal_signal_yields_no_score():
    # ordinary memory: no belief marker -> None (warden stays permissive)
    assert score_candidate_belief("u1", "some memory", {"evidence_kinds": ("graphiti",)}) is None


def test_superseded_emits_is_expired_contradicts():
    ev = assemble_belief_evidence({"belief_superseded": True, "evidence_kinds": ("git",)})
    sigs = {(e.signal, e.polarity) for e in ev}
    assert (EvidenceSignal.IS_EXPIRED, EvidencePolarity.CONTRADICTS) in sigs
    assert (EvidenceSignal.SOURCE_IS_GIT, EvidencePolarity.SUPPORTS) in sigs


def test_current_temporal_emits_valid_at_supports():
    ev = assemble_belief_evidence({"belief_has_temporal": True})
    assert ev[0].signal is EvidenceSignal.IS_VALID_AT_QUERY_TIME
    assert ev[0].polarity is EvidencePolarity.SUPPORTS


def test_superseded_candidate_scores_and_buckets():
    score = score_candidate_belief("u1", "the patch fixed it", {"belief_superseded": True})
    assert score is not None
    # a superseded candidate must not score as plainly safe-to-assert
    assert score.bucket.name != "SAFE_TO_ASSERT"


def test_unknown_evidence_kinds_are_ignored():
    ev = assemble_belief_evidence({"belief_has_temporal": True, "evidence_kinds": ("bogus",)})
    assert all(e.note != "provenance:bogus" for e in ev)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_belief_evidence.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'menhir.domain.belief_evidence'`.

- [ ] **Step 3: Create the adapter module**

```python
# src/menhir/domain/belief_evidence.py
"""Assemble BeliefEvidence + a BeliefScore for a recalled candidate from its metadata.

Pure adapter between menhir's recall-candidate metadata (provenance evidence_kinds + bi-
temporal markers) and the belief-scoring model (belief.py). No I/O, no graph. Returns None
when the candidate carries no belief-relevant signal, so CurrentnessWarden stays permissive
(ADMIT) on ordinary memories and acts only on belief-bearing ones.
"""
from __future__ import annotations

from menhir.domain.belief import (
    BeliefCandidate,
    BeliefCandidateType,
    BeliefEvidence,
    BeliefScore,
    BeliefScorer,
    EvidencePolarity,
    EvidenceSignal,
)

# provenance evidence_kind -> external-anchor signal (SUPPORTS). Unknown kinds are ignored.
_KIND_TO_SIGNAL: dict[str, EvidenceSignal] = {
    "user": EvidenceSignal.SOURCE_IS_USER,
    "log": EvidenceSignal.SOURCE_IS_LOG,
    "git": EvidenceSignal.SOURCE_IS_GIT,
    "graphiti": EvidenceSignal.SOURCE_IS_GRAPHITI,
    "file": EvidenceSignal.FILE_CHANGED,
    "test": EvidenceSignal.TEST_PASSED,
}

_SCORER = BeliefScorer()


def assemble_belief_evidence(metadata: dict[str, object]) -> tuple[BeliefEvidence, ...]:
    """Map a candidate's metadata signals to BeliefEvidence. Pure, order-stable.

    Temporal: belief_superseded -> IS_EXPIRED (CONTRADICTS); else belief_has_temporal ->
    IS_VALID_AT_QUERY_TIME (SUPPORTS). Provenance: each known evidence_kind -> its anchor
    signal (SUPPORTS). strength=1.0; the scorer's DEFAULT_SIGNAL_WEIGHTS carry the weight.
    """
    ev: list[BeliefEvidence] = []
    if metadata.get("belief_superseded"):
        ev.append(BeliefEvidence(
            EvidenceSignal.IS_EXPIRED, EvidencePolarity.CONTRADICTS, 1.0,
            "superseded fact", "temporal"))
    elif metadata.get("belief_has_temporal"):
        ev.append(BeliefEvidence(
            EvidenceSignal.IS_VALID_AT_QUERY_TIME, EvidencePolarity.SUPPORTS, 1.0,
            "current fact", "temporal"))
    for kind in metadata.get("evidence_kinds", ()) or ():
        sig = _KIND_TO_SIGNAL.get(str(kind))
        if sig is not None:
            ev.append(BeliefEvidence(
                sig, EvidencePolarity.SUPPORTS, 1.0, f"provenance:{kind}", "graph"))
    return tuple(ev)


def score_candidate_belief(
    candidate_id: str, content: str, metadata: dict[str, object]
) -> BeliefScore | None:
    """Score a candidate's belief state, or None when it carries no belief-relevant signal.

    None unless a temporal marker is present, so CurrentnessWarden stays permissive on
    ordinary memories. candidate_type: SUPERSESSION when superseded, else DEPENDENCY_STATE.
    """
    if not (metadata.get("belief_superseded") or metadata.get("belief_has_temporal")):
        return None
    ctype = (
        BeliefCandidateType.SUPERSESSION
        if metadata.get("belief_superseded")
        else BeliefCandidateType.DEPENDENCY_STATE
    )
    touched = tuple(str(e) for e in (metadata.get("touched_entities", ()) or ()))
    candidate = BeliefCandidate(
        id=candidate_id, statement=content[:200], candidate_type=ctype, touched_entities=touched
    )
    return _SCORER.score(candidate, assemble_belief_evidence(metadata))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_belief_evidence.py -q`
Expected: PASS (5 passed). If `test_superseded_candidate_scores_and_buckets` fails, inspect the real `BeliefScore.bucket` the scorer assigns for an IS_EXPIRED-dominated candidate and assert against the actual non-safe bucket — do not weaken the scorer.

- [ ] **Step 5: Commit**

```bash
git add src/menhir/domain/belief_evidence.py tests/test_belief_evidence.py
git commit -m "feat: pure belief-evidence assembly + scoring adapter for the belief gate"
```

---

### Task 2: Wire the producer into AssertionPipeline behind a `belief_gate` flag

**Files:**
- Modify: `src/menhir/services/assertion_pipeline.py`
- Test: `tests/test_assertion_pipeline.py` (extend — structural addition, not a behavior rewrite of existing cases)

**Interfaces:**
- Consumes: `score_candidate_belief`, `assemble_belief_evidence` (Task 1); `CurrentnessWarden` from `menhir.domain.warden`.
- Produces: `AssertionPipeline(..., belief_gate: bool = False)`; when True, `CurrentnessWarden` is in the chain and every `WardenContext` carries `belief_score` + `evidence`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_assertion_pipeline.py
import pytest
from menhir.domain.oracle_combiner import LogSpaceOracleCombiner
from menhir.domain.oracles import CandidateMemory, QueryContext
from menhir.services.assertion_pipeline import AssertionPipeline
from menhir.domain.warden import CurrentnessWarden


def test_belief_gate_off_excludes_currentness_warden():
    p = AssertionPipeline(LogSpaceOracleCombiner(), belief_gate=False)
    assert not any(isinstance(w, CurrentnessWarden) for w in p.wardens)


def test_belief_gate_on_includes_currentness_warden():
    p = AssertionPipeline(LogSpaceOracleCombiner(), belief_gate=True)
    assert any(isinstance(w, CurrentnessWarden) for w in p.wardens)


@pytest.mark.asyncio
async def test_belief_gate_on_refuses_superseded_under_current_intent():
    p = AssertionPipeline(LogSpaceOracleCombiner(), belief_gate=True, auto_intent=False)
    q = QueryContext(text="what is true now", intent="current")
    cands = [CandidateMemory(id="u1", content="the patch fixed it",
                             metadata={"similarity": 0.9, "belief_superseded": True})]
    outcome = await p.run(q, cands)
    # a superseded belief must not land in admitted-as-current
    assert all(r.candidate_id != "u1" for r in outcome.admitted)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest tests/test_assertion_pipeline.py -q -k belief_gate`
Expected: FAIL (`AssertionPipeline` has no `belief_gate` kwarg).

- [ ] **Step 3: Add the flag, the warden, and the producer wiring**

Add imports:

```python
from menhir.domain.belief_evidence import assemble_belief_evidence, score_candidate_belief
from menhir.domain.warden import CurrentnessWarden
```

In `__init__`, add `belief_gate: bool = False` to the signature and build the chain:

```python
        default = [ScopeWarden(), EvidenceAnchorWarden(), OracleAdmissionWarden()]
        if belief_gate:
            default.append(CurrentnessWarden())
        if contradiction_interrupt:
            default.append(ContradictionWarden())
        self.wardens = wardens or default
        self.belief_gate = belief_gate
```

In `run()`, populate the belief fields on the per-candidate context:

```python
            belief_score = (
                score_candidate_belief(cid, candidate.content, candidate.metadata)
                if self.belief_gate else None
            )
            evidence = (
                assemble_belief_evidence(candidate.metadata) if self.belief_gate else ()
            )
            ctx = WardenContext(
                candidate_id=cid, intent=intent,
                belief_score=belief_score, evidence=evidence,
                query_scope=q_scope, candidate_scope=_candidate_scope(candidate),
                support_profile=_support_profile(candidate), oracle_packet=packet,
            )
```

- [ ] **Step 4: Run the pipeline suite to verify pass + no regression**

Run: `python -m pytest tests/test_assertion_pipeline.py -q`
Expected: PASS, including the pre-existing cases (belief_gate defaults False, so the existing chain is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/menhir/services/assertion_pipeline.py tests/test_assertion_pipeline.py
git commit -m "feat: belief_gate wires CurrentnessWarden + belief scoring into AssertionPipeline"
```

---

### Task 3: Plumb the `MENHIR_FRONTIER_BELIEF_GATE` flag (default OFF)

**Files:**
- Modify: `src/menhir/domain/retrieval_tuning.py` (`RetrievalTuningConfig`)
- Modify: `src/menhir/config/settings.py` (field + env read + mapping)
- Modify: `src/menhir/services/recall_service.py` (pass `belief_gate` into both `AssertionPipeline` constructions; include the flag in `frontier_active`)
- Test: `tests/test_recall_service.py` (extend)

**Interfaces:**
- Consumes: the `belief_gate` kwarg from Task 2.
- Produces: `RetrievalTuningConfig.enable_belief_gate: bool = False`; `settings.frontier_belief_gate`; env `MENHIR_FRONTIER_BELIEF_GATE`.

- [ ] **Step 1: Add the config field**

In `retrieval_tuning.py`, add to `RetrievalTuningConfig` (next to `enable_warden_gate`):

```python
    enable_belief_gate: bool = False
```

- [ ] **Step 2: Add the settings field + env mapping**

In `settings.py`, mirror the `frontier_warden_gate` triple exactly: add a `frontier_belief_gate` field, read `MENHIR_FRONTIER_BELIEF_GATE` with the same truthy parsing as the other frontier flags, and map it into `retrieval_tuning()` as `enable_belief_gate=self.frontier_belief_gate`.

- [ ] **Step 3: Thread the flag into recall_service**

In `recall_service.py`, at BOTH sites that construct `AssertionPipeline` (the shadow pass `_run_assertion_shadow` and the active `_apply_frontier`), pass `belief_gate=tuning.enable_belief_gate`. Add `tuning.enable_belief_gate` to the `frontier_active` disjunction so the frontier metadata (and shadow) path activates when only the belief gate is on:

```python
        frontier_active = (
            tuning.enable_oracle_ranking
            or tuning.enable_warden_gate
            or tuning.enable_belief_gate
            or (trace and tuning.enable_assertion_shadow)
        )
```

- [ ] **Step 4: Write the failing test**

```python
# add to tests/test_recall_service.py
def test_belief_gate_flag_defaults_off_and_maps_through():
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig
    assert RetrievalTuningConfig().enable_belief_gate is False
```

(Extend with the project's existing settings-env test pattern to assert `MENHIR_FRONTIER_BELIEF_GATE=1` flips `retrieval_tuning().enable_belief_gate` True, matching how `MENHIR_FRONTIER_WARDEN_GATE` is tested.)

- [ ] **Step 5: Run the recall + settings suites**

Run: `python -m pytest tests/test_recall_service.py -q`
Expected: PASS; default path unchanged (flag defaults False).

- [ ] **Step 6: Commit**

```bash
git add src/menhir/domain/retrieval_tuning.py src/menhir/config/settings.py src/menhir/services/recall_service.py tests/test_recall_service.py
git commit -m "feat: MENHIR_FRONTIER_BELIEF_GATE flag (default off) threads belief_gate into recall"
```

---

### Task 4: Source temporal markers into candidate metadata when the gate is on

**Files:**
- Modify: `src/menhir/services/recall_service.py` (`_attach_frontier_metadata`, or a sibling step it calls)
- Test: `tests/test_belief_gate_metadata_rung.py` (new)

**Interfaces:**
- Consumes: the existing `graph_adapter.fetch_temporal_facts(uuids)` (from the rung1 fold) and `_filter_to_current_beliefs`.
- Produces: for each candidate uuid, `metadata_by_uuid[uuid]["belief_superseded"]` (True when the candidate has a superseded fact) and `metadata_by_uuid[uuid]["belief_has_temporal"]` (True when it has any temporal fact). These are the markers Task 1 reads.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_belief_gate_metadata_rung.py
"""The belief-gate temporal-marker attach step is pure over fetched fact-rows (stub adapter)."""
from __future__ import annotations

from menhir.services.recall_service import _belief_markers_from_facts


def test_superseded_and_current_markers():
    rows = [
        {"node_uuid": "u1", "expired_at": "2025-01-02"},   # superseded
        {"node_uuid": "u2", "expired_at": None},            # current only
    ]
    markers = _belief_markers_from_facts(rows)
    assert markers["u1"] == {"belief_superseded": True, "belief_has_temporal": True}
    assert markers["u2"] == {"belief_superseded": False, "belief_has_temporal": True}


def test_no_rows_no_markers():
    assert _belief_markers_from_facts([]) == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_belief_gate_metadata_rung.py -q`
Expected: FAIL with `ImportError: cannot import name '_belief_markers_from_facts'`.

- [ ] **Step 3: Add the pure marker-builder and attach it (gated)**

Module-level helper in `recall_service.py`:

```python
def _belief_markers_from_facts(
    rows: list[dict[str, object]],
) -> dict[str, dict[str, bool]]:
    """Per-uuid belief markers from temporal fact-rows: has-any-temporal, and superseded
    when any fact for that uuid has expired_at set (belief invalidation)."""
    markers: dict[str, dict[str, bool]] = {}
    for row in rows:
        uuid = str(row.get("node_uuid") or "")
        if not uuid:
            continue
        m = markers.setdefault(uuid, {"belief_superseded": False, "belief_has_temporal": True})
        if row.get("expired_at") is not None:
            m["belief_superseded"] = True
    return markers
```

In `_attach_frontier_metadata` (or immediately after it, in `recall()`), when `tuning.enable_belief_gate`, fetch temporal facts for the eligible uuids and merge the markers into `metadata_by_uuid`:

```python
        if tuning.enable_belief_gate and eligible_uuids:
            fact_rows = await asyncio.to_thread(
                self.graph_adapter.fetch_temporal_facts, eligible_uuids
            )
            for uuid, marks in _belief_markers_from_facts(fact_rows).items():
                metadata_by_uuid.setdefault(uuid, {}).update(marks)
```

- [ ] **Step 4: Run the new + recall suites**

Run: `python -m pytest tests/test_belief_gate_metadata_rung.py tests/test_recall_service.py -q`
Expected: PASS. The fetch is gated by `enable_belief_gate`, so the default path issues no extra query.

- [ ] **Step 5: Commit**

```bash
git add src/menhir/services/recall_service.py tests/test_belief_gate_metadata_rung.py
git commit -m "feat: attach belief temporal markers to candidate metadata when belief gate on"
```

---

### Task 5: Record belief-gate status, deferred work, and the bench-activation gate

**Files:**
- Modify: `docs/research/belief-temporal/belief-layer.md`

**Interfaces:**
- Consumes/Produces: documentation only.

- [ ] **Step 1: Record the implemented status**

Under the implementation ladder, add a "Belief gate (implemented, default-OFF)" note recording: `CurrentnessWarden` is now reachable via `AssertionPipeline(belief_gate=True)`, gated by `MENHIR_FRONTIER_BELIEF_GATE` (default OFF); the producer is `domain/belief_evidence.py`; temporal markers are sourced from the rung1 `fetch_temporal_facts`; the gate scores only belief-bearing (temporal-marked) candidates and is permissive otherwise.

- [ ] **Step 2: Record deferred work and the activation gate**

Add a "Forward / before activation" note: (a) git/structure staleness evidence is NOT yet wired — `git_staleness.derive_structural_staleness` returns `BeliefEvidence` but needs a `list[GitChange]` feed not modeled in the live recall path; (b) richer provenance weighting and CAUSE/FIX/REGRESSION candidate-type inference are deferred; (c) the gate must be validated on the archolith-bench belief-drift fixtures (`ce_willow_belief_drift`, `auth_payload_refactor_stale_memory`, ...) and shadow-measured before `MENHIR_FRONTIER_BELIEF_GATE` is turned on in any environment — menhir proposes, archolith-bench proves.

- [ ] **Step 3: Commit**

```bash
git add docs/research/belief-temporal/belief-layer.md
git commit -m "docs: record belief-gate activation status, deferred git-staleness, bench gate"
```

---

## Self-Review

- **Spec coverage:** Task 1 builds the missing producer (the only new logic). Task 2 makes `CurrentnessWarden` functional and chains it behind a flag. Task 3 plumbs the default-OFF env flag through tuning/settings/recall (shadow included). Task 4 feeds the producer real temporal input from the rung1 fetch. Task 5 records status + the bench gate. Together they activate the belief layer's decision path without changing default behavior.
- **Placeholder scan:** none — every code step is complete; Task 3 steps 2/4 reference the existing `frontier_warden_gate` pattern as the exact template to copy rather than restating it.
- **Type consistency:** `belief_gate: bool` is used identically across `AssertionPipeline.__init__`, `RetrievalTuningConfig.enable_belief_gate`, `settings.frontier_belief_gate`, and both recall construction sites. `score_candidate_belief(candidate_id, content, metadata) -> BeliefScore | None` and `assemble_belief_evidence(metadata) -> tuple[BeliefEvidence, ...]` match their call sites in Task 2. `BeliefEvidence(signal, polarity, strength, note, source_id)` and `BeliefCandidate(id, statement, candidate_type, touched_entities)` match the real dataclasses.
- **Default-path safety:** with the flag off, no `CurrentnessWarden` is appended, no belief scoring runs, and no temporal fetch is issued — the path is identical to today. Permissive-on-absence is preserved by `score_candidate_belief` returning None and `CurrentnessWarden`'s built-in `score is None -> ADMIT`.
- **Interaction with rung1:** the gate reuses the rung1 `fetch_temporal_facts`; the temporal-recall filter (output annotation) and the belief gate (admission decision) are complementary and both honor the temporal lens — consistent with the forward invariant recorded in the Rung 1 reconciliation plan.
