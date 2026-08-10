# Rung 1 Temporal-Intent Reconciliation Implementation Plan

> **Status note 2026-07-11 (code-reconciled): ACTIVE / NOT WIRED — kept, not archivable.**
> Verified against `src/menhir`: the three domain pieces exist and are unit-tested, but the
> wiring this plan owns has NOT landed:
> - `classify_temporal_intent` (`domain/temporal_intent.py:43`) is still **never called** by any
>   service (only referenced in comments) — still dormant.
> - `matches_query` (`domain/temporal.py:100`) is still **unused** by any service.
> - `recall_service` still uses the hand-rolled binary `_filter_to_current_beliefs` driven by the
>   manual `include_invalidated` flag (`:148,1250`), not the canonical filter.
> (The separate intent-oracle work wired a *task*-intent lens via `task_intents_to_lens`; that is a
> different classifier and does not route fact-filtering through `matches_query`, so it does not
> satisfy this plan.) Remaining work = the plan as written: make the classifier the lens source and
> route filtering through `matches_query`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect menhir's three already-built but disconnected Rung 1 pieces so that recall infers its temporal lens from the query (instead of relying on a manual flag) and filters bi-temporal facts through the single canonical filter, giving correct current / belief-drift / as-of-world behavior.

**Architecture:** Three pieces of `belief-layer.md` "Rung 1: belief-aware recall policy" exist independently and none are wired together: (1) `domain/temporal_intent.classify_temporal_intent()` infers a `TemporalQuery` lens + `include_invalidated` + `as_of` from query text but is **never called**; (2) `domain/temporal.matches_query()` is the canonical deterministic 3-lens fact filter (`FactTemporal` over `valid_at/invalid_at/created_at/expired_at`); (3) the freshly-folded rung1 enrichment in `services/recall_service.recall()` annotates results with `TemporalFact`s and filters them with the hand-rolled binary `_filter_to_current_beliefs()` driven by a manual `include_invalidated: bool` caller flag. This plan makes the dormant classifier the source of the lens, routes fact filtering through `matches_query`, and keeps the explicit caller flag only as an override. No new domain logic is written — the classifier and the filter are already unit-tested in `tests/domain/`; this is wiring plus its tests.

**Tech Stack:** Python 3.12, pytest (`pythonpath = src`, `asyncio_mode = auto`), pure stdlib domain layer. No Neo4j: every step is exercised against stubbed `graph_adapter.fetch_temporal_facts` and pure classifier/filter calls.

## Global Constraints

- **Default behavior is preserved.** A query with no temporal cue AND no explicit caller flag must return current beliefs only (`expired_at IS NULL`) — identical to today. `classify_temporal_intent` already defaults to `CURRENT_BELIEF` / `include_invalidated=False`, so the default path is unchanged.
- **Explicit caller value wins.** `include_invalidated=True`/`False` passed by a caller overrides query inference. Inference happens only when the caller omits it. The sentinel for "omitted" is `None`.
- **Never leak history unless asked.** When the resolved lens is `CURRENT_BELIEF`, superseded beliefs (`expired_at` set) must be dropped.
- **No live Neo4j.** Another process is ingesting into the live graph; all tests are pure/stub-based. Do not run `tests/test_recall_live.py` or any `online`/`replay`-marked test.
- **Do not modify existing rung1 tests** (`tests/test_filter_current_beliefs_rung1b.py`, `tests/test_temporal_facts_rung1a.py`, `tests/test_formatter_temporal_rung1.py`). `_filter_to_current_beliefs` stays as the current-belief path so those tests stay green. `classify_temporal_intent` and `matches_query` already have their own tests in `tests/domain/` — do not duplicate them; test only the new wiring.
- Run tests from the repo root: `python -m pytest <path> -q`. The single `graphiti_core` Pydantic-V2 deprecation warning is third-party and benign.

## File Structure

- `src/menhir/services/recall_service.py` — owns the wiring: resolve the lens once in `recall()`, route fact-row filtering through `matches_query`, emit the lens decision into the result note. This is the brain of the change.
- `src/menhir/core/backend_protocol.py`, `src/menhir/core/backend_impl.py`, `src/menhir/mcp/tools/recall/recall_memories.py` — the plumbing edge: change `include_invalidated` from `bool` to `bool | None` so "omitted" (infer) is distinguishable from "explicitly False".
- `docs/research/belief-temporal/belief-layer.md` — record that Rung 1 is now unified across the three pieces and define the forward invariant for the gated warden path.
- Tests: new flat files under `tests/` mirroring the rung1 convention (`tests/test_*_rung1*.py`).

---

### Task 1: Infer the temporal lens in recall and filter facts through `matches_query`

**Files:**
- Modify: `src/menhir/services/recall_service.py` (the `recall()` signature ~line 589, and the temporal-enrichment block that currently calls `_filter_to_current_beliefs`)
- Test: `tests/test_temporal_intent_recall_rung1d.py` (new)

**Interfaces:**
- Consumes: `from menhir.domain.temporal_intent import classify_temporal_intent` returning `TemporalIntent(query: TemporalQuery, include_invalidated: bool, as_of: str | None, cue: str | None)`; `from menhir.domain.temporal import FactTemporal, matches_query` where `FactTemporal(valid_at, invalid_at, created_at, expired_at)` and `matches_query(fact, query, *, as_of=None) -> bool`; the existing module-level `_filter_to_current_beliefs(rows)` and `_build_temporal_facts(rows)`.
- Produces: `recall()` now accepts `include_invalidated: bool | None = None` and resolves the effective lens internally. No new public symbols.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_temporal_intent_recall_rung1d.py
"""Wiring tests: recall infers the temporal lens from the query and filters facts (Rung 1D).

classify_temporal_intent and matches_query are unit-tested in tests/domain/; here we only
assert that recall() routes through them correctly. Uses a stub graph_adapter -- no Neo4j.
"""
from __future__ import annotations

from menhir.services.recall_service import _resolve_temporal_filter


def _rows():
    return [
        {"node_uuid": "u1", "fact": "current", "valid_at": "2025-01-01",
         "invalid_at": None, "created_at": "2025-01-02", "expired_at": None},
        {"node_uuid": "u1", "fact": "superseded", "valid_at": "2024-01-01",
         "invalid_at": "2024-06-01", "created_at": "2024-01-02", "expired_at": "2025-01-02"},
    ]


def test_no_cue_no_flag_keeps_current_only():
    # query with no temporal cue, caller omits flag -> current beliefs only
    kept = _resolve_temporal_filter("what is the storage layout", None, _rows())
    assert [r["fact"] for r in kept] == ["current"]


def test_history_cue_includes_superseded():
    # "used to" is a history cue -> include_invalidated inferred True, no as_of -> all facts
    kept = _resolve_temporal_filter("what did we used to believe about storage", None, _rows())
    assert {r["fact"] for r in kept} == {"current", "superseded"}


def test_explicit_false_overrides_history_cue():
    # caller explicitly says False -> override inference, current only
    kept = _resolve_temporal_filter("what did we used to believe", False, _rows())
    assert [r["fact"] for r in kept] == ["current"]


def test_explicit_true_overrides_default():
    kept = _resolve_temporal_filter("plain query", True, _rows())
    assert {r["fact"] for r in kept} == {"current", "superseded"}


def test_as_of_world_filters_to_pivot():
    # "as of 2024-03-01" -> AS_OF_WORLD, as_of pivot -> only facts valid at that world time
    kept = _resolve_temporal_filter("what was true as of 2024-03-01", None, _rows())
    assert [r["fact"] for r in kept] == ["superseded"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_temporal_intent_recall_rung1d.py -q`
Expected: FAIL with `ImportError: cannot import name '_resolve_temporal_filter'`.

- [ ] **Step 3: Add the `_resolve_temporal_filter` helper and imports**

Add imports near the other domain imports at the top of `recall_service.py`:

```python
from menhir.domain.temporal import FactTemporal, matches_query
from menhir.domain.temporal_intent import classify_temporal_intent
```

Add the helper next to `_filter_to_current_beliefs` (module level):

```python
def _resolve_temporal_filter(
    query: str,
    include_invalidated: bool | None,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Filter temporal fact-rows by the query's temporal lens.

    The lens is inferred from the query (classify_temporal_intent) unless the caller
    passes an explicit include_invalidated, which overrides inference. Resolution:
      - effective_include = include_invalidated if not None else intent.include_invalidated
      - effective_include False -> current beliefs only (_filter_to_current_beliefs)
      - effective_include True + as_of -> matches_query per-row (world/known-at pivot)
      - effective_include True + no as_of -> all history (belief-drift queries)
    """
    intent = classify_temporal_intent(query)
    effective_include = (
        intent.include_invalidated if include_invalidated is None else include_invalidated
    )
    if not effective_include:
        return _filter_to_current_beliefs(rows)
    if include_invalidated is None and intent.as_of:
        return [
            r for r in rows
            if matches_query(
                FactTemporal(
                    valid_at=r.get("valid_at"),
                    invalid_at=r.get("invalid_at"),
                    created_at=r.get("created_at"),
                    expired_at=r.get("expired_at"),
                ),
                intent.query,
                as_of=intent.as_of,
            )
        ]
    return rows
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `python -m pytest tests/test_temporal_intent_recall_rung1d.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Wire the helper into `recall()` and widen the signature**

Change the signature default:

```python
        include_invalidated: bool | None = None,
```

In the temporal-enrichment block, replace:

```python
                if not include_invalidated:
                    fact_rows = _filter_to_current_beliefs(fact_rows)
```

with:

```python
                fact_rows = _resolve_temporal_filter(query, include_invalidated, fact_rows)
```

- [ ] **Step 6: Run the recall regression + new suite**

Run: `python -m pytest tests/test_recall_service.py tests/test_temporal_facts_rung1a.py tests/test_filter_current_beliefs_rung1b.py tests/test_temporal_intent_recall_rung1d.py -q`
Expected: PASS, no regression (existing rung1 tests still green because `_filter_to_current_beliefs` is unchanged and still runs on the current-belief path).

- [ ] **Step 7: Commit**

```bash
git add src/menhir/services/recall_service.py tests/test_temporal_intent_recall_rung1d.py
git commit -m "feat: infer recall temporal lens from query, filter facts via matches_query"
```

---

### Task 2: Thread the `None` sentinel out to the MCP edge and surface the lens in the note

**Files:**
- Modify: `src/menhir/core/backend_protocol.py` (recall signature)
- Modify: `src/menhir/core/backend_impl.py` (recall signature; already forwards `include_invalidated=include_invalidated`)
- Modify: `src/menhir/mcp/tools/recall/recall_memories.py` (both `recall_memories()` and `RecallMemoriesTool.endpoint()` signatures + docstrings)
- Modify: `src/menhir/services/recall_service.py` (append the resolved lens cue to the result `note`)
- Test: `tests/test_recall_temporal_note_rung1d.py` (new)

**Interfaces:**
- Consumes: `_resolve_temporal_filter` and `classify_temporal_intent` from Task 1.
- Produces: `include_invalidated: bool | None = None` end-to-end; when omitted, the lens is inferred. The result `note` gains a ` | temporal lens: <LENS> (cue: '<cue>')` suffix only when a cue matched.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recall_temporal_note_rung1d.py
from __future__ import annotations

from menhir.services.recall_service import _temporal_note


def test_note_emitted_only_when_cue_matched():
    assert _temporal_note("plain orientation query") is None
    note = _temporal_note("what did we used to believe")
    assert note is not None
    assert "AS_KNOWN_AT" in note
    assert "used to" in note
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_recall_temporal_note_rung1d.py -q`
Expected: FAIL with `ImportError: cannot import name '_temporal_note'`.

- [ ] **Step 3: Add `_temporal_note` and append it in `recall()`**

Module-level helper in `recall_service.py`:

```python
def _temporal_note(query: str) -> str | None:
    """Explainability: the inferred temporal lens + the cue that triggered it, or None."""
    intent = classify_temporal_intent(query)
    if intent.cue is None:
        return None
    return f"temporal lens: {intent.query.name} (cue: {intent.cue!r})"
```

In `recall()`, after the existing `frontier_note` merge into `note`, append the temporal note using the same ` | ` convention, but only when the caller did NOT override (so an explicit flag does not advertise an unused inference):

```python
        if include_invalidated is None:
            _tnote = _temporal_note(query)
            if _tnote:
                note = f"{note} | {_tnote}" if note else _tnote
```

- [ ] **Step 4: Widen the plumbing signatures (default `None`)**

In `backend_protocol.py` and `backend_impl.py`, change `include_invalidated: bool = False` to `include_invalidated: bool | None = None`. `backend_impl` already forwards `include_invalidated=include_invalidated` to `recall_service.recall` — leave the forwarding as is.

In `recall_memories.py`, change `include_invalidated: bool = False` to `include_invalidated: bool | None = None` in BOTH `recall_memories()` and `RecallMemoriesTool.endpoint()`, leave the existing pass-throughs, and update each docstring line to:

```
        include_invalidated: Omit (default) to let menhir infer from the query ("what did we used to..." surfaces history; "as of <date>" pivots to that world time). Pass True to force history, False to force current-only.
```

- [ ] **Step 5: Run the affected suites**

Run: `python -m pytest tests/test_recall_temporal_note_rung1d.py tests/test_recall_service.py tests/test_backend_roundtrip.py tests/test_provider_backends.py -q`
Expected: PASS. If a `test_recall_service.py` note assertion now fails because its query contains a temporal cue, update that assertion to include the lens suffix — this is correct new behavior, not a contortion; do not weaken the implementation to preserve the old string.

- [ ] **Step 6: Commit**

```bash
git add src/menhir/core/backend_protocol.py src/menhir/core/backend_impl.py src/menhir/mcp/tools/recall/recall_memories.py src/menhir/services/recall_service.py tests/test_recall_temporal_note_rung1d.py
git commit -m "feat: thread include_invalidated None sentinel to MCP edge; surface temporal lens in note"
```

---

### Task 3: Record the unified Rung 1 and the forward warden-intent invariant

**Files:**
- Modify: `docs/research/belief-temporal/belief-layer.md` (the `## Implementation ladder` section, `### Rung 1` subsection)

**Interfaces:**
- Consumes: nothing at runtime — documentation only.
- Produces: a recorded reconciliation so the next session does not re-discover the three-pieces split or rebuild duplicate logic.

- [ ] **Step 1: Update the Rung 1 subsection**

Under `### Rung 1: belief-aware recall policy`, add a "Status (implemented)" note recording exactly these facts:
- The lens is inferred by `domain/temporal_intent.classify_temporal_intent()` (now wired into `recall()`), overridable by an explicit `include_invalidated`.
- Fact filtering goes through the canonical `domain/temporal.matches_query()` / `FactTemporal`; the binary `_filter_to_current_beliefs()` remains only as the `CURRENT_BELIEF` fast-path and is a behavioral subset of `matches_query(..., CURRENT_BELIEF)` (flag it as a candidate for later de-duplication, non-blocking).
- The belief `QueryIntent` (`belief.py`: CURRENT / HISTORICAL / CONFLICT) used by the R8 `ContradictionWarden` is a SEPARATE classifier resolved inside `AssertionPipeline._resolve_intent`, gated behind `enable_warden_gate`. Today it cannot contradict the temporal filter because the warden path is off by default.

- [ ] **Step 2: Record the forward invariant (Rung 2 boundary)**

Add a short "Forward" note stating the invariant to enforce when `enable_warden_gate` activates: the temporal lens (`TemporalQuery`) and the belief `QueryIntent` must be derived consistently (a `CURRENT_BELIEF` query must not be admitted as `HISTORICAL` by the warden, and vice versa), and that the genuine next rung is promoting temporal facts from output-only annotation to a staleness/contradiction signal the oracle/warden can read. Mark this as design-only and out of scope for this plan (it touches the gated warden path and needs an eval gate).

- [ ] **Step 3: Commit**

```bash
git add docs/research/belief-temporal/belief-layer.md
git commit -m "docs: record unified Rung 1 temporal-intent wiring + forward warden-intent invariant"
```

---

## Self-Review

- **Spec coverage:** Task 1 activates the dormant classifier and routes filtering through `matches_query` (current / belief-drift / as-of-world). Task 2 makes inference reachable through the MCP edge and explains the decision. Task 3 records the reconciliation and the forward boundary. The three disconnected pieces are now connected.
- **Placeholder scan:** none — every code step contains complete code; every run step names the file and expected result.
- **Type consistency:** `include_invalidated: bool | None` is used identically across `recall()`, `backend_protocol`, `backend_impl`, and both MCP entry points. `_resolve_temporal_filter(query, include_invalidated, rows)` and `_temporal_note(query)` signatures match their call sites. `FactTemporal` is constructed with exactly its four fields; `matches_query(fact, query, *, as_of=)` matches the real signature.
- **Default-path safety:** existing rung1 tests are untouched and stay green because `_filter_to_current_beliefs` still serves the `CURRENT_BELIEF` path; the only behavior change for callers is opt-in inference when they omit the flag.
