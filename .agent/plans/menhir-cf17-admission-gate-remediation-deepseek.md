# Menhir CF-17 — Admission Gate Contradiction Remediation Plan

**Status:** CONFIRMED · **Severity:** High (downgraded from Critical — see §1)
**Last verified:** 2026-08-18 — ACCURATE, remediation pending. `_has_negation` and `_has_polarity_mismatch` are 0 hits in `src/`; CONFIRMED refers to the finding, not a fix.
**Reporter's claim reviewed:** `.agent/reviews/REGISTER-SNAPSHOT.md:133-176`
**Verifier agent:** deepseek/deepseek-v4-flash · **Date:** 2026-08-12

---

## 1. Verdict

**CONFIRMED.** The token-overlap branch of `_text_grounded` admits claims asserting the
**opposite** of their cited source at the apex trust tier. Every executed contradiction case
in the reporter's claim reproduces, and I added four further independent cases that also
reproduce.

**Severity: High, not Critical.** The reporter graded Critical. I concur the defect is real,
production-wired, and corrupts governed provenance. I downgrade to High for three reasons
(§4):
- The blast radius is confined to **user/manual-sourced** ingests. Every production agent
  source (`claude-code`, `codex`, `opencode`) passes through the gate untouched
  (passthrough at `admission_gate.py:110`), so the exploit path requires a caller that
  *already* declares `source="user"` and supplies a valid `turn_evidence_uuid`. CF-4/CF-5
  make that attacker-influence story plausible, not proven.
- The practical false-admission rate is bounded: the caller must supply a genuine user turn
  whose text **differs from the claim only by a negation / numeral / antonym** while sharing
  ≥50% of significant tokens. That is a real but narrow mis-grounding window, not an open
  door for arbitrary text.
- The gate's other defenses (role check `:132`, session/namespace match `:147-161`,
  missing-evidence deny `:119`) still work; only the grounding test is defective.

It is a **genuine fail-open in a module whose contract is fail-closed** (`admission_gate.py:8`),
so it belongs above Medium regardless of the calibration.

**The reporter understated one thing and overstated nothing:** the defect is worse than "a
few fixed examples" — the overlap rule is *structurally* guaranteed to pass a negation/numeral
rewrite of any sentence with ≥5 significant tokens (shown in §3). The one case that *failed*
in my adversarial set (`"i did not like the movie"` vs `"i liked the movie"`) failed by the
arithmetic of token *count* (dilution), not by any negation awareness — and that same
construction flips to GROUNDED with a slightly different source (see §3).

---

## 2. Verification Evidence

All commands executed in the project venv (`.venv/Scripts/python.exe`) with
`PYTHONPATH=src`, against the **installed** module (not a re-implementation).

### 2a. Reporter's three cases — reproduced exactly

```
negation   claimed='the deploy failed on prod'      source='the deploy succeeded on prod'   GROUNDED=True
numeral    claimed='I own 100 coins'                source='I own 900 coins'                GROUNDED=True
antonym    claimed='the server is down'             source='the server is up'               GROUNDED=True
```

### 2b. Four additional adversarial cases I designed (all assert the opposite; all GROUNDED)

```
numeral      'set budget to $100'        vs 'set budget to $1000'        GROUNDED=True
numeral      'there were 5 people'       vs 'there were 50 people'       GROUNDED=True
numeral      'paid 300 dollars'          vs 'paid 600 dollars'           GROUNDED=True
numeral      'order 42 units'            vs 'order 77 units'             GROUNDED=True
negation     'the package never arrived' vs 'the package arrived'        GROUNDED=True
negation     'the server no longer works' vs 'the server works'          GROUNDED=True
polarity     'she refuses to sign'       vs 'she agrees to sign'         GROUNDED=True
antonym      'the server is up'          vs 'the server is down'         GROUNDED=True
subject-swap 'I own the house'           vs 'you own the house'          GROUNDED=True
subject-swap 'the red team won'          vs 'the blue team won'          GROUNDED=True
polarity     'we won the game'           vs 'we lost the game'           GROUNDED=True
```

### 2c. One adversarial case that correctly DENIED — and why it does not exonerate

```
negation     'i did not like the movie'  vs 'i liked the movie'          GROUNDED=False
```

This denial is **not** negation awareness. Token analysis (executed):
```
 claimed tokens: ['did', 'not', 'like', 'the', 'movie']   (5 significant)
 source tokens : ['liked', 'movie', 'the']                (3)
 overlap       : ['the', 'movie']  =>  2 >= 5*0.5 = 2.5?  False
```
It fails only because the two negation words `did`/`not` push the claimed count to 5, so the
2-token overlap falls below the 50% bar. The same construction with a shorter clause
(e.g. "the server did not fail" vs "the server failed": claimed significant = `['server',
'did','not','fail']` (4), overlap with source `{server, failed}` = `{server}` (1), `1 >= 2.0`?
False — borderline) or any source adding a shared content word re-crosses the bar. This is
**coincidental token-count dilution**, not a semantic guard, and it cannot be relied on.

### 2d. False-negative side (legitimate restatements that MUST keep grounding) — all GROUNDED today

```
exact        'I bought a bicycle'                       -> GROUNDED=True   (substring)
substring    'I bought a bicycle' in longer sentence    -> GROUNDED=True   (substring)
reorder      'I bought a bicycle' vs 'a bicycle I bought' -> GROUNDED=True (token overlap)
synonym      'I bought a bicycle' vs 'I purchased a bicycle' -> GROUNDED=True (overlap 50%)
case/ws      'I bought a bicycle' vs 'I   BOUGHT\n\ta  BICYCLE' -> GROUNDED=True (substring, normalized)
phrase-sub   'liked the movie' in 'i really liked the movie yesterday' -> GROUNDED=True
```

### 2e. Baseline test suite (must remain green)

```
.venv/Scripts/python.exe -m pytest tests/domain/test_admission_gate.py -q
14 passed, 1 warning in 0.36s
```

---

## 3. Threshold Mechanics

The token-overlap rule is implemented in `_text_grounded`, `src/menhir/domain/truth/admission_gate.py:64-72`:

```python
64:    # Token overlap: split into non-empty words, require >= 50% of claimed tokens in source.
65:    claimed_tokens = [t for t in re.split(r"\W+", norm_claimed) if t and len(t) > 2]
66:    if not claimed_tokens:
67:        # Claimed is all short/empty tokens (e.g. "a b c") — require exact substring.
68:        return norm_claimed in norm_source
69:
70:    source_tokens = set(t for t in re.split(r"\W+", norm_source) if t and len(t) > 2)
71:    overlap = sum(1 for t in claimed_tokens if t in source_tokens)
72:    return overlap >= len(claimed_tokens) * 0.5
```

Answering the specific questions:

- **What counts as a "significant token"?** A token surviving `re.split(r"\W+", ...)` with
  `len(t) > 2`. Stopwords of length ≤2 (`a`, `to`, `be`, `in`, `on`, `or`, `at`, `by`) are
  dropped; longer stopwords (`the`, `and`, `for`, `that`) are **kept** because the filter is
  length-only, not a stopword list (`:65`). So "significant" is purely a length heuristic —
  `the` (3) is significant, `in` (2) is not.
- **Which text is the 50% computed over?** The **claimed** text. `overlap` counts how many
  claimed tokens appear in the source set, and compares to `len(claimed_tokens) * 0.5`
  (`:71-72`). The source contributes only as a set membership test (`:70`).
- **Structure of the OR:** a contiguous substring match short-circuits at `:61`
  (`if norm_claimed in norm_source: return True`). The token-overlap branch is consulted only
  when the claimed text is *not* a contiguous substring.
- **The core flaw (exact line):** `:72` `return overlap >= len(claimed_tokens) * 0.5` — a
  bare multiset-overlap count with no lexical polarity, negation, or numeric-equality handling.
  `100` and `900` are different tokens, so a numeral substitution does not reduce overlap at
  all. `succeeded` vs `failed` are different tokens, but in `"the deploy failed on prod"` they
  are the *only* differing tokens: claimed significant = `[deploy, failed, prod]` (3), source
  set = `{deploy, succeeded, prod}` (3), overlap = `{deploy, prod}` (2), `2 >= 1.5` → True.

**Structural guarantee of the fail-open:** for any claimed sentence with N significant tokens
where exactly one content token is negated/antonymed/numerically-substituted, overlap = N−1
(when the source keeps N−1 identical tokens), which is `>= N*0.5` for all `N >= 2`. So **every
single-word-polarity contradiction of a multiword sentence grounds** unless extra token count
pushes it below the bar — the arithmetic *incentivizes* admitting the closest false claim.

---

## 4. Reachability and Downstream Effect

The claim asserted two production call sites; I confirm **all four** (two per file) plus the
wider wiring:

**Import sites (matching the register `:161-162`):**
- `services/ingest_intake.py:84` — `from menhir.domain.truth.admission_gate import evaluate_user_tier_claim`
- `infrastructure/temporal_repository.py:64` — same import

**Call sites (the two the claim names, `ingest_intake.py:104,119` and `temporal_repository.py:78,93`):**
- `services/ingest_intake.py:104` and `:119` (both inside `queue_episode_for_enrichment`)
- `infrastructure/temporal_repository.py:78` and `:93` (both inside `create_temporal`)

**What the verdict controls — and it is NOT cosmetic:**

1. **Persisted source label → trust tier.** In `ingest_intake.py:130-143`, the episode is
   created with `source=effective_source` (`:136`) and `source_confidence=source_confidence_for(effective_source)` (`:137`). In `temporal_repository.py:137`, the node is created with `"source": effective_source`. `source_confidence_for` (`domain/utils.py:101-120`) maps `user`/`manual` → `SOURCE_CONFIDENCE_USER` **1.0** (`utils.py:79-80`, `truth/kinds.py:91`) and `agent_inference` → `SOURCE_CONFIDENCE_AGENT` **0.5** (`kinds.py:105`). Executed:
   ```
   user            -> 1.0
   agent_inference -> 0.5
   manual          -> 1.0
   ```
   So a GROUNDED contradiction is stored at **1.0**; a correctly-denied one at **0.5**. That is
   exactly the apex-vs-inference tier delta the claim describes. This tier is the input to the
   attestation ladder (`truth/attestation.py:84` gates `>= 0.7` → `AGENT_REVIEWED`) and to
   corroboration (`utils.py:218`).

2. **Provenance edge admission.** In `ingest_intake.py:172-173`:
   `admitted_on_uuid = verdict.turn_evidence_uuid if verdict.granted else None`. On a false
   grant the memory is linked to the user turn as if genuinely user-admitted, and the
   evidence projection is built from it (`:199-206`). The comment at `:161-167` states a denial
   means "the cited evidence did NOT support the claim, so drawing the edge would assert the
   foundation the gate just refused." A false grant draws that foundation.

3. **Audit trail.** Denials are recorded to `agent-status` (`:221-236`); routine grants are
   intentionally **not** audited (`:237-242`). A contradiction admitted as a "routine grant"
   therefore produces **no audit record** — the corruption is silent.

**Production reachability confirmed:**
- `queue_episode_for_enrichment` (the gated method) is called from `core/backend_runtime_data_ops.py:50` (the runtime `add_episode` op) and from `ingest_intake.py:333` (`add_episode`), both behind `IngestService` (`services/ingest_service.py:64`), which is built in `core/bootstrap.py:204` and exposed via MCP `add_memory` / `add_memory_and_track`.
- `create_temporal` is reached via `core/backend_runtime_admin_ops.py:497-521` → `memory_graph_adapter.py:1552` → `temporal_repository.py:29`, and via MCP `mcp/tools/ingest/add_memory.py:92`.

**Verdict is ignored nowhere.** `verdict.granted` and `verdict.effective_source` are both consumed at the two call sites (ingest: `:111`, `:173`, `:221`, `:228`; temporal: `:85`, `:152`). The gate is a live control, not a dead safety net.

---

## 5. Candidate Approaches

All three are evaluated against the module contract: **deterministic, LLM-free, offline,
fail-closed** (`admission_gate.py:8`). No new dependency is acceptable unless stated.

### (a) Drop the token-overlap branch; require contiguous-substring grounding

- **Fixes:** Removes the fail-open entirely. Only a literal normalized substring of the source
  (case/whitespace-normalized) admits a user claim. No contradiction can pass.
- **Breaks:** `test_grant_token_overlap` (`tests/domain/test_admission_gate.py:179-199`,
  `"I purchased a new bicycle yesterday"` vs claimed `"I bought a bicycle"`) flips to **deny**.
  This is an existing test that *asserts* the current overlap behavior — it must be updated.
- **False-negative cost:** High. Legitimate restatements that share vocabulary but not an exact
  substring (synonymy, reordering, extra words) are denied and downgraded to 0.5. That is the
  "over-tightened fix that denies true user statements" the task warns about. The module
  docstring (`:1-8`) frames a user claim as grounded in "actual user input"; an exact-substring
  reading is defensible but maximally strict.
- **Complexity:** Minimal — delete `:64-72`, make `_text_grounded` return the `:61` substring
  result (plus the `:57-58` empty guard). No new dependency.
- **Tradeoff verdict:** Cheapest and safest against the defect, but maximally hostile to
  legitimate paraphrase.

### (b) Keep token overlap, add negation/antonym/numeral detection

- **Fixes:** Would catch the demonstrated cases (negation via a `not`/`never`/`no`/`no longer`
  prefix scan; numerals by comparing numeric values extracted from both sides; antonymy via a
  fixed polarity-word table).
- **Breaks / cost:** This is a **semantic** problem wearing a regex costume. Numerals need
  number extraction + value comparison (`$100` vs `$1000` — current overlap gives 100% because
  `$` is stripped by `\W+`, leaving `100` vs `1000` as the differing tokens — a value-aware
  comparison is required, not a token one). Negation is ambiguous (`"I never said it failed"`),
  and antonymy needs a curated table that is incomplete by construction. Each rule is a new
  partial heuristic that can be gamed or misfire. LLM-free and offline **is** achievable (no new
  dependency), but it is the highest-complexity option and still not sound — a future unseen
  contradiction shape passes.
- **Tradeoff verdict:** Reduces false-negatives better than (a) but never reaches fail-closed;
  the module's stated precision-first contract is poorly served by adding more permissive
  heuristics. High complexity, ongoing maintenance.

### (c) REWRITE `_text_grounded` as a fail-closed **multi-condition AND** with a mandatory exact-substring anchor, keeping token-overlap only as a *narrow* tiebreak that is itself polarity-guarded (RECOMMENDED)

- **Fixes:** Admit only when grounding is *unambiguous*. Define grounded as:
  `exact_substring OR (token_overlap >= THRESH AND no_polarity_mismatch AND numeric_values_agree)`
  where `no_polarity_mismatch` denies if the claim contains a negation/counter-signal
  (`not`, `never`, `no`, `no longer`, `refus*`, etc.) absent from the source (or vice versa),
  and `numeric_values_agree` extracts all number tokens from both sides and requires the
  claimed values to be a subset of the source values (deny if `100` claims against source `900`).
- **Fixes:** All eleven reproduced contradictions: negation (`not`/`never`/`no longer`),
  polarity (`refuses`), numerals (`100/900`, `$100/$1000`, `5/50`, `300/600`, `42/77`), and
  antonymy via the polarity table. Keeps `test_grant_token_overlap` green (it has no
  contradiction signal: `"bought a bicycle"` vs `"purchased a new bicycle yesterday"` shares no
  negation and no numeral, so it still grounds by overlap).
- **Breaks / false-negative cost:** Low-to-moderate, strictly less than (a): legitimate
  restatements with no polarity/number conflict still ground by overlap. The remaining
  false-negatives are only those that *also* trip a guard (e.g. a genuine paraphrase that
  happens to use the word "not" — rare and, under fail-closed, the correct call).
- **Complexity:** Moderate. ~25-35 lines: a negation detector, a number-extraction + value
  comparator, and a small polarity token table. All deterministic, stdlib-only
  (`re`, `math`). No new dependency; remains LLM-free and offline.
- **Tradeoff verdict:** Best precision/recall balance; genuinely fail-closed on every
  demonstrated class while preserving legitimate grounding.

---

## 6. Recommendation

**Adopt (c): rewrite `_text_grounded` to require that any token-overlap grant is free of
negation/polarity and numeric contradictions, with the exact-substring anchor unchanged.**

Decisive reason first: **(a) is sound but over-tight (it fails the module's stated
precision-first *and* the practical requirement to admit real user statements — the task
explicitly warns an over-tightened fix is its own defect), and (b) is unsound by construction**
(it grows more heuristics for a semantic problem and still cannot reach fail-closed). Only (c)
preserves the module's intent — *deny when in doubt, admit real restatements* — by making the
overlap branch refuse exactly the two contradiction classes the evidence shows it currently
admits (negation/polarity and numerals), while leaving all legitimate restatements grounding.
It stays deterministic, stdlib-only, and offline, honoring `admission_gate.py:8`. If the team
prefers zero nuance and maximal strictness, (a) is the acceptable fallback — but it changes
existing green-test behavior and denies genuine paraphrase, so it should be a deliberate
product decision, not the default.

---

## 7. Implementation Steps (ordered)

1. **Rewrite the grounding predicate** in `src/menhir/domain/truth/admission_gate.py`.
   - Keep `_normalize_text` (`:35-39`) and `_text_grounded`'s signature (`:42`).
   - Replace the body's tail `:60-72`. Keep the empty guards `:57-58` and the exact-substring
     anchor `:61-62`.
   - Add two module-private helpers in the same file, below `_normalize_text`:
     - `_has_polarity_mismatch(norm_claimed, norm_source) -> bool` — scans both normalized
       strings for a counter-signal token set (`not`, `never`, `no longer`, `never`, `refuse`,
       `refuses`, `refused`, `deny`, `denies`, `won't`, `cannot`, `can't`, `no`) and returns
       True when a counter-signal appears on exactly one side. (A curated, expandable
       `frozenset` — no dependency.)
     - `_numeric_values(norm) -> set[str]` — extract all numeric runs via
       `re.findall(r"\d[\d,.]*", norm)`. Return the normalized value set.
   - Rework `:64-72` to:
     ```
     if _has_polarity_mismatch(norm_claimed, norm_source):
         return False
     claimed_nums = _numeric_values(norm_claimed)
     if claimed_nums and not (claimed_nums <= _numeric_values(norm_source)):
         return False
     # ... existing token-overlap block (:65-72) unchanged ...
     ```
     Order matters: run the guards *before* the overlap computation so they short-circuit to
     deny. The substring branch (`:61`) is intentionally left **above** these guards so an
     exact verbatim match still grants (a verbatim negation in both source and claim is
     grounded, not a contradiction).
2. **Do not change `evaluate_user_tier_claim` (`:75-178`)** — the denial paths, role check,
   session/namespace match, and the passthrough for non-user sources are all correct.
3. **Update the single conflicting test** `tests/domain/test_admission_gate.py:179-199`
   (`test_grant_token_overlap`) — it must keep passing under (c); add a comment that the
   overlap grant is allowed because the pair carries no polarity/numeric contradiction.
4. **Add regression tests** for the §8 matrix to `tests/domain/test_admission_gate.py`.
5. **Run** `pytest tests/domain/test_admission_gate.py -q` and the wider admission-adjacent
   suite (`tests/test_episode_admission_link.py`, `tests/test_episode_admission_endpoint.py`,
   `tests/test_temporal_repository.py`).
6. **Update the module docstring** (`:1-8`) to state the token-overlap grant is now
   negation- and numeral-guarded — this prevents a future reviewer from re-trusting the
   previous "conservative" claim (the comment-lies pattern this codebase has nine instances of).

---

## 8. Test Matrix

Expected verdicts under the recommended (c) fix. `DENY` = downgrade to `agent_inference`.

| # | claimed | source (turn evidence) | today | after (c) | class |
|---|---------|------------------------|-------|-----------|-------|
| 1 | the deploy failed on prod | the deploy succeeded on prod | GRANT (bug) | **DENY** | negation |
| 2 | the server is down | the server is up | GRANT (bug) | **DENY** | antonym |
| 3 | the server is up | the server is down | GRANT (bug) | **DENY** | antonym |
| 4 | I own 100 coins | I own 900 coins | GRANT (bug) | **DENY** | numeral |
| 5 | set budget to $100 | set budget to $1000 | GRANT (bug) | **DENY** | numeral |
| 6 | there were 5 people | there were 50 people | GRANT (bug) | **DENY** | numeral |
| 7 | paid 300 dollars | paid 600 dollars | GRANT (bug) | **DENY** | numeral |
| 8 | order 42 units | order 77 units | GRANT (bug) | **DENY** | numeral |
| 9 | the package never arrived | the package arrived | GRANT (bug) | **DENY** | negation |
| 10 | the server no longer works | the server works | GRANT (bug) | **DENY** | negation |
| 11 | she refuses to sign | she agrees to sign | GRANT (bug) | **DENY** | polarity |
| 12 | I own the house | you own the house | GRANT (bug) | **DENY** | subject-swap* |
| 13 | the red team won | the blue team won | GRANT (bug) | **DENY** | subject-swap* |
| 14 | we won the game | we lost the game | GRANT (bug) | **DENY** | polarity/antonym |
| 15 | i did not like the movie | i liked the movie | DENY (coincidental) | **DENY** | negation |
| 16 | I bought a bicycle | I bought a bicycle | GRANT | **GRANT** | must-still-ground: exact |
| 17 | I bought a bicycle | Today I bought a bicycle for $200 | GRANT | **GRANT** | must-still-ground: substring |
| 18 | I bought a bicycle | a bicycle I bought | GRANT | **GRANT** | must-still-ground: reorder |
| 19 | I bought a bicycle | I purchased a bicycle | GRANT | **GRANT** | must-still-ground: synonym (no polarity/number) |
| 20 | I bought a bicycle | I   BOUGHT\n\ta  BICYCLE | GRANT | **GRANT** | must-still-ground: case/ws |
| 21 | liked the movie | i really liked the movie yesterday | GRANT | **GRANT** | must-still-ground: phrase-substr |

*Rows 12-13 are subject-swap, not negation/numeral. Under fix (c) they may still GRANT if the
overlap bar is met (`I own the house` vs `you own the house`: claimed significant `{own,
house}` (2), source `{you, own, house}` → overlap 2, `2>=1` → grants unless subject-swap is
added to the polarity table). I flag them as **known residual gap** under (c); if the team
wants subject-swap denied too, extend `_has_polarity_mismatch` with a first-person-vs-
second-person pronoun check (rows 12) — antonym verbs like `won`/`lost` (13) are caught by
adding `won`/`lost` to the polarity table. Open decision, not a blocker.

**False-negative guard (non-negotiable):** rows 16-21 must remain GRANT. If any of them flips
to DENY after implementation, the fix is over-tight and must be reworked.

---

## 9. Blast Radius and Existing Test Coverage

**Production callers (the only ones affected by changing `_text_grounded`):**
- `services/ingest_intake.py:104,119` (import `:84`) — `queue_episode_for_enrichment`
- `infrastructure/temporal_repository.py:78,93` (import `:64`) — `create_temporal`
- `_text_grounded` has **no other callers** (verified: only definition `admission_gate.py:42`
  and its single use `:164`). So the fix is contained to these two ingest paths plus anything
  that builds episodes from them.

**Existing test files that exercise this surface** (named; all must stay green or be updated
as documented):
- `tests/domain/test_admission_gate.py` — 14 unit tests, the direct coverage. `test_grant_token_overlap` (`:179`) asserts the old overlap behavior and is the only one needing review under (c).
- `tests/test_episode_admission_link.py` — asserts the provenance-edge behavior (`:44,102,119-120,200`) and that a denied user claim "beats the caller supplied id" (`:200`). Depends on the gate's tier decision.
- `tests/test_episode_admission_endpoint.py` — end-to-end admission via the runtime op; ingest through `queue_episode_for_enrichment`.
- `tests/test_temporal_repository.py` — `create_temporal` path (uses `source="claude-code"`, which passes through the gate untouched, so lower risk).
- `tests/test_episode_admission_link_live` / `test_episode_admission_endpoint_live` — live Neo4j variants (opt-in; not run in the routine unit suite).

**What could regress:** any test that relies on token-overlap granting a paraphrase now
guarded as a contradiction; and, conversely, a too-aggressive polarity/numeral guard denying
a legitimate restatement (guarded by rows 16-21). The corroboration/attestation tier math
itself (`domain/utils.py`, `domain/truth/attestation.py`) is untouched and unaffected.

---

## 10. Effort and Risk

- **Effort:** Small. ~25-35 lines of new code in one file plus ~15 test cases. Single-file
  change; no schema, no migration, no new dependency. Estimate **0.5–1 engineer-day** including
  tests and the docstring update.
- **Risk:** **Low-to-moderate.**
  - Low structural risk: `_text_grounded` is a pure function with a single caller site; the
    change is fully unit-testable and the blast radius is two ingest paths.
  - Moderate behavioral risk: getting the polarity/numeral guards wrong in either direction.
    Mitigated by the explicit test matrix (rows 1-15 must DENY, 16-21 must GRANT).
  - Zero deploy-time risk: this is a pure Python logic change; existing stored data and the
    DB are untouched. Live Neo4j tests are opt-in and not part of the routine suite.

---

## 11. Open Questions

1. **Subject-swap / pronoun scope (rows 12-13).** Should `I own` vs `you own` and `red team`
   vs `blue team` be denied? Under the recommended (c) they may still ground by overlap. If the
   team treats subject identity as part of the claim's grounding, `_has_polarity_mismatch`
   needs pronoun (`I`/`you`/`we`/`they`) and entity-name awareness — a larger scope. This is an
   explicit scope decision, not an implementation error.
2. **The `$100` vs `$1000` numeric guard semantics.** My comparator requires claimed numeric
   values to be a subset of source values (deny if the claim asserts a number not in source).
   It does **not** currently treat `100` vs `1000` as "different magnitudes" — it treats any
   numeric divergence as ungrounded. That is the correct fail-closed reading but worth
   confirming the team agrees value-equality is the bar (vs. magnitude similarity).
3. **Legitimate "not" usage.** A genuine user statement containing "not" when the source also
   contains "not" still grounds (the mismatch detector fires only on one-sided presence). A
   genuine paraphrase introducing "not" on the claim side but not the source will be denied.
   Under fail-closed this is correct, but it is a behavior change worth noting to product.
4. **Whether the polarity token table should be extended** to verb-level antonym pairs
   (`won`/`lost`, `agree`/`refuse`) now or in a follow-up; rows 11 and 14 already demonstrate
   the class.

---

## REVISION 2 (2026-08-13) — Recommendation (c) of Revision 1 is WITHDRAWN

The reviewer's three objections are correct and each is reproduced by executed evidence. This
section supersedes §5/§6/§7/§8 of Revision 1. The original is preserved above for the record;
it fails because it kept the token-overlap branch.

### R2.0 — Why Revision 1 (c) failed (reproduced, not argued)

The reviewer correctly identified that my (c) = token-overlap + negation/numeral guards was
silent on antonymy because `failed`/`succeeded`, `down`/`up` carry no negation token. Executed
against the exact spec I wrote in §7:

```
COUNTER = {'not','never','no longer','refuse','refuses','refused','deny','denies',"won't",'cannot',"can't",'no'}
def has_neg(t): return any(tok in t for tok in COUNTER)
def nums(t): return set(re.findall(r"\d[\d,]*", t))
def c_rev1(claimed, source):
    if claimed in source: return True            # substring ABOVE guards (bug 2)
    if has_neg(source) and not has_neg(claimed): return False
    if nums(claimed) and not (nums(claimed) <= nums(source)): return False
    return overlap_ok(claimed, source)            # THE bug: keeps overlap

'the deploy failed on prod'  vs 'the deploy succeeded on prod'  -> True   (still admitted)
'i own 100 coins'            vs 'i own 900 coins'               -> False  (numeral guard works)
'the server is down'         vs 'the server is up'              -> True   (still admitted)
'the deploy failed'          vs 'i did not say the deploy failed' -> True  (new hole, substring above guards)
```

All three of the reviewer's problems reproduced:
1. **Negation ≠ antonymy.** `failed` vs `succeeded`, `down` vs `up` have no token in `COUNTER`,
   so `has_neg` never fires on either side and the overlap branch grants as before.
2. **Substring anchor above the guards.** `"the deploy failed"` is a literal substring of
   `"i did not say the deploy failed"`; the anchor returns `True` before `has_neg` runs.
3. **(c) was (b).** Two curated word lists, same "grows heuristics, cannot reach fail-closed"
   objection the reviewer levied at (b).

### R2.1 — Answer to reviewer question A: the honest limit

**No deterministic, offline, stdlib-only predicate can distinguish antonym substitution from
legitimate paraphrase.** This is not a design gap; it is a consequence of the module's own
constraints (`admission_gate.py:8`: deterministic, LLM-free). To separate `"the server is down"`
(antonym, must DENY) from `"I purchased a bicycle"` (paraphrase, arguably should GRANT), a
predicate would need lexical/semantic knowledge of *polarity* — that `down` is the opposite of
`up` while `bought` is a near-synonym of `purchased`. That is exactly the kind of model /
word-vector / dependency-parsing knowledge the module is prohibited from using. Any finite
curated table (option b, option c) is incomplete by construction and is precisely the "more
heuristics for a semantic problem" that cannot reach fail-closed. **State plainly: no such
predicate exists under the module's constraints. The defect cannot be fixed while retaining
token-overlap grounding. It can only be fixed by removing token-overlap grounding.**

### R2.2 — Answer to reviewer question B: the real choice, and the cost asymmetry

With (A) settled, the genuine choice is between:
- **(a)** contiguous-substring-only grounding — denies genuine paraphrase (reorder, synonymy),
- **(c) the claim cannot admit paraphrase safely at all** — because any paraphrase-accepting
  rule (overlap) is exactly the rule that admits antonyms.

The cost asymmetry is decisive and not close:

| Failure | Consequence | Recoverability |
|---|---|---|
| **False positive** (admit contradiction as user) | Agent-authored claim stored at **1.0**, linked to the user's turn (`ingest_intake.py:173`), drawn into evidence projection (`:199-206`), no audit (grant suppressed, `:237-242`). Feeds attestation ladder (`truth/attestation.py:84`) and corroboration (`utils.py:218`). | **None.** Once the 1.0 edge exists, `scalar_view_has_user_foundation` and every downstream read treat it as genuine user admission. Silent, undetectable, and exactly the promise the module exists to protect. |
| **False negative** (deny true paraphrase) | True user statement downgraded to `agent_inference` (0.5). The fact still persists at 0.5 — no data loss, the text is still in the graph, just at lower tier. | **Total.** The tier is the ceiling; a stronger observation can re-admit it. A downgrade is recoverable, visible, and fail-closed per the docstring. |

A false negative costs a tier on a true statement; a false positive manufactures trust in a
false statement. These are **not comparable**. Under fail-closed / precision-first
(`admission_gate.py:8`), the asymmetry mandates: **DENY the paraphrase.** Accept that a user's
word reordering or synonym restatement drops to 0.5 rather than ever admit an antonym at 1.0.

### R2.3 — Answer to reviewer question C: guards above the anchor

**Place the guards above the substring anchor.** Concrete justification for why a verbatim
substring inside a denial must NOT ground:

```
claimed = 'the deploy failed'
source  = 'i did not say the deploy failed'
```

`"the deploy failed"` is a verbatim substring of the source, yet the source asserts the exact
opposite of the claim. Extracting the substring and persisting it at 1.0 as a *user* admission
is the core CF-17 corruption — an agent taking a negated fragment and storing it as an
affirmative user fact. There is **no legitimate reading** under which a fragment carved out of
a denial grounds a user-tier claim. Therefore the one-sided negation guard MUST sit above the
anchor and short-circuit it. The same ordering argument applies to the numeral guard: `$100` is
a verbatim substring of `$1000` (prefix collision), yet `"set budget to $100"` asserts a
different value than `"set budget to $1000"` — the numeral guard must run before the anchor too.

### R2.4 — The corrected predicate (RECOMMENDED)

This is **option (a), contiguous-substring-only**, with exactly two leak-plug guards placed
**above** the anchor. It is NOT (b)/(c): there is no token-overlap branch at all, so antonymy
cannot pass — an antonym is a *different string*, and different strings are not substrings. The
two guards only close the two residual leaks that *substring matching itself* would otherwise
admit (denial-extraction, numeric prefix-collision). They do not attempt to detect antonymy.

```python
NEG_TOKENS = ("not","never","no","no longer","never","refuse","refuses","refused",
              "deny","denies","won't","cannot","can't","did not","didn't","do not",
              "don't","does not","doesn't")

def _normalize(text): return " ".join(str(text or "").lower().split())

def _has_negation(text): t = _normalize(text); return any(tok in t for tok in NEG_TOKENS)

def _nums(text): return set(re.findall(r"\d[\d,]*", _normalize(text)))

def _text_grounded(claimed, source_span) -> bool:
    c, s = _normalize(claimed), _normalize(source_span)
    if not c or not s: return False
    # Guard 1 (ABOVE anchor): one-sided negation — source denies, claim doesn't. Rejects
    # 'the deploy failed' carved out of 'i did not say the deploy failed'.
    if _has_negation(source_span) and not _has_negation(claimed):
        return False
    # Guard 2 (ABOVE anchor): claimed numeral must be present in source. Rejects '$100' in
    # '$1000' (prefix collision) and '100' in '900'.
    if _nums(claimed) and not (_nums(claimed) <= _nums(source_span)):
        return False
    # Anchor LAST: contiguous substring is the sole semantic test.
    return c in s
```

### R2.5 — Answer to reviewer question D: the full matrix EXECUTED

All candidates executed in the project venv against the installed-style predicate module
`candidate_final` (the predicate above), `candidate_a` (substring-only, no guards), `candidate_c`
(substring + negation guard only), and `candidate_c_rev1` (the withdrawn Revision-1 (c)). Real
output:

```
row  case               exp   (a) substr  (c-rev2)   final   (c-rev1 old)  result
#1   negation           DENY  deny        deny       deny    GRANT         PASS old=GRANT
#2   antonym down       DENY  deny        deny       deny    GRANT         PASS old=GRANT
#3   antonym up         DENY  deny        deny       deny    GRANT         PASS old=GRANT
#4   numeral 100/900    DENY  deny        deny       deny    deny          PASS
#5   numeral $100       DENY  GRANT       GRANT      deny    GRANT         FAIL a=GRANT; cr=GRANT; old=GRANT
#6   numeral 5/50       DENY  deny        deny       deny    deny          PASS
#7   numeral 300/600    DENY  deny        deny       deny    deny          PASS
#8   numeral 42/77      DENY  deny        deny       deny    deny          PASS
#9   negation never     DENY  deny        deny       deny    GRANT         PASS old=GRANT
#10  no longer          DENY  deny        deny       deny    GRANT         PASS old=GRANT
#11  polarity refuse    DENY  deny        deny       deny    GRANT         PASS old=GRANT
#12  subject swap       DENY  deny        deny       deny    GRANT         PASS old=GRANT
#13  team swap          DENY  deny        deny       deny    GRANT         PASS old=GRANT
#14  win/loss           DENY  deny        deny       deny    GRANT         PASS old=GRANT
#15  not dilution       DENY  deny        deny       deny    deny          PASS
#16  STILL exact        GRANT GRANT       GRANT      GRANT   GRANT         PASS
#17  STILL substring    GRANT GRANT       GRANT      GRANT   GRANT         PASS
#18  STILL reorder      GRANT deny        deny       deny    GRANT         FAIL (by design)
#19  STILL synonym      GRANT deny        deny       deny    GRANT         FAIL (by design)
#20  STILL case/ws      GRANT GRANT       GRANT      GRANT   GRANT         PASS
#21  STILL phrase       GRANT GRANT       GRANT      GRANT   GRANT         PASS
#22  denial-extract     DENY  GRANT       deny       deny    GRANT         FAIL a=GRANT; old=GRANT
```

Analysis of the only non-PASS rows:

- **#5 (`$100` vs `$1000`)** — `candidate_a` (substring-only, no guards) and `candidate_c`
  GRANT it because `"set budget to $100"` is a verbatim **prefix substring** of
  `"set budget to $1000"`. Only `candidate_final`'s numeral guard catches it. **This is the
  proof that plain option (a) alone is insufficient — it needs the numeral guard above the
  anchor.**
- **#22 (denial-extract)** — `candidate_a` GRANTs it (`"the deploy failed"` ⊂
  `"i did not say the deploy failed"`). Only `candidate_final`'s negation guard (above the
  anchor) catches it. **This is the proof that plain (a) alone also needs the negation guard.**
- **#18 (reorder) and #19 (synonym)** — `candidate_final` DENYs them, as every substring-only
  predicate must. These are the deliberate false negatives of §R2.2: denying genuine paraphrase
  to avoid ever admitting antonymy. Under the cost asymmetry they are the correct trade.
  **`candidate_c_rev1` GRANTs them precisely because it kept token-overlap — which is the bug.**

**Conclusion of D: `candidate_final` is the only predicate of the four that passes every
canonical contradiction case (#1-15, #22) AND every must-still-ground verbatim case (#16, #17,
#20, #21). Its only DENYs are #18/#19, which are intended.**

Additional executed probes of `candidate_final` (false-negative side, must be honest):

```
legit affirmative in sentence w/ unrelated not    final=False   <- negation-guard false negative
verbatim negation both sides (should GRANT)       final=True
negation claim-only (reverse)                     final=False
legit exact with not both sides                   final=True
numeral legit present                             final=True
$200 in source                                    final=True
```

The `final=False` on `'the deploy failed'` vs `'the deploy failed and i did not regret it'` is
a **false negative**: the claimed affirmative is genuinely present, but Guard 1 fires because the
source also contains `not`. This is the documented cost of the one-sided negation guard — it
over-denies when a source sentence contains an unrelated negation. It errs in the safe (DENY)
direction, which the §R2.2 asymmetry says is correct, but it is a real recall loss on legitimate
affirmative fragments co-occurring with a negation. **This is an explicit, accepted tradeoff, not
hidden.**

### R2.6 — Answer to reviewer question E: verdict on the revised fix

**Every canonical contradiction case passes.** `candidate_final` DENYs all of #1-15 and #22. It
does not fail any canonical case. The two remaining DENYs (#18 reorder, #19 synonym) are not
canonical contradictions — they are legitimate paraphrases that this fix *deliberately* denies,
which is the correct fail-closed outcome under §R2.2. I state this plainly: the fix **cannot**
admit paraphrase; that is the point, not a hidden failure.

The one honest, disclosed residual is the **negation-guard false negative** (R2.5 probe 1):
a legitimate affirmative substring co-occurring with an unrelated `not` in the source is denied.
This errs in the safe direction. It is the only known false negative beyond the intended
paraphrase denials.

### R2.7 — Revised implementation steps

1. Rewrite `_text_grounded` (`src/menhir/domain/truth/admission_gate.py:42-72`): **delete the
   token-overlap branch entirely** (`:64-72`). Replace with: empty-guards (`:57-58`), then the
   two guards above the anchor (one-sided negation, numeral-presence), then `return c in s`.
   Keep `_normalize_text` (`:35-39`) and the empty guards.
2. Add the two helpers (`_has_negation`, `_nums`) below `_normalize_text` (same module,
   stdlib-only: `re` is already imported at `:13`). No new dependency.
3. **Do not change** `evaluate_user_tier_claim` (`:75-178`); its denial paths and passthrough
   are correct.
4. Update tests:
   - `tests/domain/test_admission_gate.py:179-199` (`test_grant_token_overlap`) — **must now
     expect DENY**. It asserts the withdrawn overlap behavior ("purchased a bicycle" vs "bought
     a bicycle"); under substring-only that is a false-admission the fix removes. Rewrite it as
     `test_deny_token_overlap_without_substring`.
   - Add the R2.5 matrix as parametrized cases (rows #1-22) plus the negation-guard
     false-negative probe as a documented DENY.
5. Run `pytest tests/domain/test_admission_gate.py -q` and the admission-adjacent suite
   (`tests/test_episode_admission_link.py`, `tests/test_episode_admission_endpoint.py`,
   `tests/test_temporal_repository.py`).
6. Update the module docstring (`:1-8`) and `_text_grounded` docstring to state grounding is
   **contiguous-substring-only**, guard-above-anchor, and that token-overlap grounding is
   deliberately absent because it cannot be made fail-closed.

### R2.8 — Revised test matrix (expected verdicts under `candidate_final`)

| # | claimed | source | expected | rationale |
|---|---------|--------|----------|-----------|
| 1 | the deploy failed on prod | the deploy succeeded on prod | **DENY** | antonym, no shared substring |
| 2 | the server is down | the server is up | **DENY** | antonym |
| 3 | the server is up | the server is down | **DENY** | antonym |
| 4 | I own 100 coins | I own 900 coins | **DENY** | numeral guard |
| 5 | set budget to $100 | set budget to $1000 | **DENY** | numeral guard (prefix collision) |
| 6 | there were 5 people | there were 50 people | **DENY** | numeral guard |
| 7 | paid 300 dollars | paid 600 dollars | **DENY** | numeral guard |
| 8 | order 42 units | order 77 units | **DENY** | numeral guard |
| 9 | the package never arrived | the package arrived | **DENY** | negation (claim-only) |
| 10 | the server no longer works | the server works | **DENY** | negation (claim-only) |
| 11 | she refuses to sign | she agrees to sign | **DENY** | antonym, no shared substring |
| 12 | I own the house | you own the house | **DENY** | subject swap, no shared substring |
| 13 | the red team won | the blue team won | **DENY** | subject swap |
| 14 | we won the game | we lost the game | **DENY** | antonym |
| 15 | i did not like the movie | i liked the movie | **DENY** | negation (claim-only) |
| 16 | I bought a bicycle | I bought a bicycle | **GRANT** | verbatim |
| 17 | I bought a bicycle | Today I bought a bicycle for $200 | **GRANT** | verbatim substring |
| 18 | I bought a bicycle | a bicycle I bought | **DENY** | reorder — paraphrase, intentionally denied |
| 19 | I bought a bicycle | I purchased a bicycle | **DENY** | synonym — paraphrase, intentionally denied |
| 20 | I bought a bicycle | I   BOUGHT\n\ta  BICYCLE | **GRANT** | verbatim after normalize |
| 21 | liked the movie | i really liked the movie yesterday | **GRANT** | verbatim substring |
| 22 | the deploy failed | i did not say the deploy failed | **DENY** | denial-extract, negation guard above anchor |

Must-still-ground set (non-negotiable): **#16, #17, #20, #21** (verbatim / substring after
normalization). Rows #18/#19 are **expected DENY** by design, not regressions.

### R2.9 — Revised blast radius and risk

- **Blast radius unchanged** from §9: `_text_grounded` is called only at `admission_gate.py:164`;
  the two production call sites are `ingest_intake.py:104,119` and `temporal_repository.py:78,93`.
- **Behavior change to flag:** the `test_grant_token_overlap` case ("purchased a bicycle" →
  "bought a bicycle") now DENYs. This is a **user-visible downgrade** of genuine paraphrase:
  an agent claiming a user said "I bought a bicycle" when the user said "I purchased a bicycle"
  is now stored at 0.5. This is the intended cost of §R2.2 and must be surfaced to product.
- **Risk: Low-moderate, unchanged** but with one honest addition: the negation guard
  (R2.5 probe 1) can deny a legitimate affirmative fragment when the source sentence also
  contains an unrelated `not`. Acceptable under fail-closed, but it is a real false-negative
  and should be in the review notes.

### R2.10 — Revised effort

~40-50 lines (two helpers + rewritten `_text_grounded` + ~24 test cases) in one file plus the
one rewritten test. No schema/migration/new dependency. Estimate **0.5-1 engineer-day**.

### R2.11 — Revised open questions

1. **Accepting paraphrase loss.** Rows #18/#19 (reorder, synonymy) now DENY. Product must
   confirm it prefers a 0.5 downgrade over any chance of a 1.0 antonym admission. Per §R2.2 the
   correct engineering answer is yes, but it is a visible behavior change.
2. **Negation-guard false negative.** `'the deploy failed'` vs `'the deploy failed and i did not
   regret it'` DENYs. If this recall loss matters, a scoped refinement (negation only when it
   precedes/attaches to the matched span) is possible but adds a heuristic — and the reviewer's
   own objection 3 cautions against growing exactly that. Current recommendation: keep the
   simple guard, accept the safe-direction false negative.
3. **Guard token list completeness.** `NEG_TOKENS` and `_nums` are curated. `_nums` is robust
   (any digit run); `NEG_TOKENS` is an expandable frozenset with the inherent "not a complete
   grammar of negation" limitation that all offline approaches share. Any negation shape not in
   the list is not caught — but substring-only grounding means an unrecognized-negation fragment
   still only grounds if it is a verbatim substring, which sharply bounds the residual risk.


