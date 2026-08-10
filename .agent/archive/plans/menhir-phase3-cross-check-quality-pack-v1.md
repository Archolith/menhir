# Plan: phase3-cross-check-quality-pack-v1

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Shipped, live-characterized, and promoted ON by
> default: `personal_memory_consolidation_sum_grounding = True` (`settings.py:218`), flag
> `MENHIR_PERSONAL_MEMORY_SUM_GROUNDING`, implementation wired across `perception.py`,
> `scheduler_tasks.py`, `runtime.py`, `maintenance_scheduler.py`. Archived per owner rule (a).

**STATUS: EXECUTED + LIVE-CHARACTERIZED + PROMOTED 2026-07-08.** Deterministic SUM-grounding
implemented (`MENHIR_PERSONAL_MEMORY_SUM_GROUNDING`); **default PROMOTED to ON** after live gate met.
20 safety unit tests + settings/threading tests, bench SUM phrasing matrix. Live throwaway :8099
(gpt-4o-mini), OFF vs ON, N=5/variant (ON run 2x):

| variant | OFF commit | ON commit | wrong (OFF/ON) |
|---------|-----------|-----------|-----------------|
| two-episode  | 40%  | 100% | 0 / 0 |
| one-sentence | 40%  | 100% | 0 / 0 |
| worded       | 100% | 80%* | 0 / 0 |
| sequential   | 100% | 100% | 0 / 0 |
| list         | 60%  | 60%  | 0 / 0 |

`wrong_view_writes=0` in EVERY cell (item 5 promotion gate MET). Mechanism confirmed: ON grounded
commits skip the holistic call (`llm_calls` 7->6) and rescue the OFF holistic-veto abstentions
(`llm_calls`=4). *worded 80% = a `self_consistency` abstention (veto-1, UPSTREAM of the grounding at
veto-4) — grounding cannot cause it; N=5 stochastic noise. `list` unchanged (its vetos are
count_floor/self_consistency/verification, none touched by grounding). menhir full suite 2108 passed.
Real :8090 untouched.


Branch: `feat/phase3-cross-check-quality-pack-v1` (menhir) + fixture/evidence in archolith-bench.

**Goal (not "make SUM always commit"):** reduce FALSE abstentions on fold-SUM **without increasing
wrong current-state Views.** Hard gate: promote only if live `wrong_view_writes == 0`.

## Grounding (live + code, 2026-07-08)

- Live characterization: fold-SUM commits ~5/10; EVERY abstention fires on
  `perception_abstained_cross_check` (veto-4, Lever B holistic) at `llm_calls=4` — the pipeline stops
  at the blind holistic re-derivation BEFORE the verifier (veto-5) runs.
- Gate order (perception.py `gate`): veto-1 self-consistency -> 2 count-floor -> 2b coref -> 3
  triangulation (user-stated total) -> **4 cross_check (holistic LLM, `extract_stated_total`)** -> **5
  verification (Lever C4, `verify_candidate`, audits the LINKED items for membership/double-count/
  arithmetic)** -> 6 stated-span grounding.
- veto-4 is a BLIND re-derivation (noisy by the code's own comments); veto-5 is a sharper
  evidence-based audit. `_stated_value_grounded` already sets the precedent for a deterministic
  digit-in-source-span check (used for STATED measures).
- `Event` carries `value` (price), `episode_uuid`, `what`. Episodes are available to the gate when
  `enable_stated_span_guard=True` (prod wires it on).

## The core idea (item 4 — "arithmetic is not a belief")

When a SUM's summed amounts are each an EXPLICIT price literally present in their source span, the
arithmetic (50+75=125) is DETERMINISTICALLY provable — no LLM belief needed. Deterministic proof is
strictly stronger than the blind holistic guess, so for that case we can drop the noisy veto-4 without
weakening safety, and let the SHARPER veto-5 verifier remain the membership/double-count backstop.

### `_sum_arithmetic_grounded(value, events, episodes) -> bool` (new, deterministic)
True IFF, for a `reducer=="sum"` group:
1. every summed event has a numeric `value`, AND
2. each event's price token appears literally (digits) in ITS source episode span (reuse the
   `_stated_value_grounded` digit-matching approach, per-event), AND
3. anti-double-count: the multiset of summed amounts is covered by DISTINCT explicit price-token
   occurrences across the spans (N events summing to the value need N distinct price mentions; a
   `$40` counted twice but written once fails -> not grounded), AND
4. `sum(values)` equals the candidate `value` to the cent.
Any miss -> False (fall through to the existing holistic veto-4, unchanged). So it can NEVER rescue a
hallucinated-price, double-counted, or mis-summed candidate.

### Gate wiring (precision-preserving)
In veto-4, BEFORE calling the holistic `cross_check`: if `reducer=="sum"` and
`_sum_arithmetic_grounded(...)` is True, set `triangulated=True` with a deterministic-corroboration
audit reason and SKIP the holistic veto (continue to veto-5). Everything else unchanged. Opt-in behind
`enable_deterministic_sum_grounding` (default OFF) so precision is provably unchanged until live-proven.

**Why wrong_view_writes cannot increase:** the new path only lets a SUM SKIP the blind holistic veto
when its arithmetic is proven from distinct source-price tokens; membership is still audited by veto-5
(verifier: "does every item belong to the measure?"), grouping still needs self-consistency (veto-1).
Mis-categorization is caught by veto-5, not veto-4, so dropping veto-4 for grounded SUMs loses no
membership protection. Ungrounded/hallucinated/double-counted SUMs still hit the unchanged veto-4.

## Items

1. **Instrument cross-check decisions** — surface `value`, `cross_total`, and the margin on the
   `cross_check` abstention (structured audit/receipt), not just a count. Deterministic.
2. **Capture why cross-check vetoes bike SUM** — the reason already carries `sum=X vs holistic Y`;
   add the per-veto detail to the receipt/report so it's legible without logs. Deterministic.
3. **SUM phrasing fixture matrix** — extend `scripts/probe_phase3_sum_rate.py` (bench) with variants:
   `"$50 and $75"`, `"50 dollars and 75 dollars"`, `"spent $50, then $75 later"`, `"two bikes: $50, $75"`,
   `"$50 for one, $75 for another"`. Measure commit rate + wrong writes per variant, retries 0.
4. **Precision-preserving cross-check adjustment** — the deterministic SUM-grounding above (opt-in).
5. **Promote only if wrong stays 0** — live 2x with the flag on; flip default only if
   `wrong_view_writes == 0` across all variants. Else keep opt-in + keep the instrumentation.

## Decisions (see AskUserQuestion)

- **DECISION A — safety envelope when arithmetic is grounded.** A1 (recommended): skip the blind
  holistic veto-4, KEEP the veto-5 verifier as the membership/double-count backstop. A2: trust the
  deterministic arithmetic fully and skip veto-5 too (higher commit rate, drops membership audit —
  riskier). A1 preserves the wrong-write envelope.
- **DECISION B — rollout.** Land opt-in (default OFF), live-characterize, promote default only if
  wrong=0 (matches item 5 + the verify_retries precedent) vs default-on immediately.

## Verification
menhir `pytest -q` (new deterministic-grounding unit tests, incl. safety: hallucinated price /
double-count / mis-sum all stay abstaining). Live: throwaway :8099 (runbook), the extended probe over
the fixture matrix, flag OFF vs ON, 2x. Invariants `wrong=0 silent=0 dup=0`. Real :8090 untouched.
