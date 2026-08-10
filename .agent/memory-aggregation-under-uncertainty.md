# Write-time aggregation under uncertain perception

**A design reference for a class of problem, not a feature.** It describes what happens when a
memory system tries to compute and store a *trustworthy aggregate* — a total, a count, a running
tally — from natural-language episodes where the source is noisy, redundant, and probabilistic. The
specific mechanisms live in `services/perception.py`, `domain/fold_algebra.py`, and their plans; this
document is the *why* and the *shape*, abstracted so it transfers to the next aggregate we build.

---

## 1. The problem

A user's memory arrives as prose episodes over time: "I spent $40 on X", "I picked up another Y",
"I think I have about a dozen Z now". Downstream, we want to answer *aggregate* questions — "how much
total on X?", "how many Z?", "how many times W in the last month?" — with a single, rank-1,
query-sufficient fact rather than by re-reading a dozen scattered episodes at query time.

To do that we must, at write time, turn prose into a stored number. That number is an **aggregate over
events the model perceived from language**. Three things make it hard, and they compound:

1. **Perception is probabilistic.** The model that turns "I got new lights, $40" into a typed event
   can miscount, misattribute, hallucinate, or omit.
2. **The source is redundant.** People re-tell the same fact on different days. The same real-world
   event appears in several episodes, often worded differently and dated differently.
3. **The aggregation itself requires judgment.** Which items belong to one total? Is a mention a new
   event or a re-telling of an old one? Is a stated "I have N" a base to add to, or the answer?

The naive pipeline — extract events, sum them, store the sum — is wrong on all three axes at once,
and *confidently* wrong, which is the dangerous part (§2).

---

## 2. The governing asymmetry: a wrong aggregate is far worse than a missing one

A stored aggregate is not a neutral cache entry. It is a **state fact** that ranks authoritatively:
it looks like settled knowledge, surfaces at the top of recall, and out-ranks the raw episodes it was
derived from. So:

> **A wrong stored aggregate actively misleads. A missing one costs nothing** — recall simply falls
> back to the raw episodes, which were always there.

This asymmetry (false positives ≫ false negatives) is the single most important fact about the
problem, and it dictates the entire architecture:

- **The default is to abstain.** Materialize an aggregate only when confident; otherwise write
  nothing and let raw memory answer. Abstention needs *no fallback code* — the absence of the derived
  fact **is** the fallback. This makes "do nothing" both the safe choice and the free choice.
- **Precision is the constraint; recall is the free variable.** We tune every gate to a precision
  target ("never store a wrong current-state fact") and accept whatever recall falls out. A system
  that stores fewer aggregates but is never wrong is strictly better than one that stores more and is
  occasionally, authoritatively wrong.

---

## 3. The load-bearing separation: deterministic core, probabilistic boundary

The invariant that keeps the system analyzable:

> **Perception may be probabilistic. The fold and the stored fact must be deterministic.**

The model's *only* job is at the boundary: turn language into typed, dated, provenance-bearing events,
and make bounded judgment calls (§6). Once events exist, everything downstream — filtering, reducing,
deriving — is pure arithmetic over a small, lawful vocabulary (sum, extreme/latest, set, list; plus
window and derive). No probability ever crosses into the reducer or the stored value. Consequences:

- The aggregate is **unit-testable and reproducible** given its events.
- Correctness laws (ordering, replay/dedup, reset) are properties of the deterministic fold, provable
  once and for all, independent of the model.
- Any *confidence* signal is a **gate input** (decides whether to write) or a provenance **receipt**
  (audit metadata) — never a stored value and never a ranking signal. The moment a probability
  becomes a rankable field, it pollutes retrieval; the discipline forbids it.

---

## 4. A taxonomy of failure modes

These are the ways a naive aggregate goes wrong. They are worth enumerating because **each needs a
different guard**, and no single mechanism covers more than one.

### 4a. Confident bias (unanimous but wrong)
The model repeatedly, stably produces the *same wrong number* (e.g. a sum inflated by a double-count).
**Self-consistency cannot catch this** — sampling the extractor k times and checking agreement only
detects *variance*, not *bias*. A confidently-wrong extraction sails through an agreement check. This
is the failure mode people most often assume a confidence score would catch; it is exactly the one it
cannot, because any confidence derived from agreement is *anti-correlated with correctness* on these
cases — it stamps the wrong answer "high confidence".

### 4b. Fragmentation / heterogeneous keying
The aggregate spans *different kinds of things* under one theme (spend across a hobby: several
unlike purchases). The extractor keys each item to its own bucket and never groups them, so no single
stored fact can hold the total. Asking the model to make the grouping decision *globally* ("put these
all under one key") fails — it prefers to itemize.

### 4c. Cross-source double-counting
One real-world event narrated in several episodes becomes several events. A per-source "already
folded" ledger cannot catch it — the duplicates are distinct sources describing one occurrence, often
with different wording and different inferred dates.

### 4d. Re-narration vs recurrence ambiguity
The deterministic shadow of 4c: two same-value events on different days are *indistinguishable by
rule* from a recurring habit (a daily $5 purchase). Only the narrative resolves it, so no
deterministic signature can decide — the decision requires reading meaning.

**A caution the implementation learned the hard way.** A coreference *signature* (the deterministic
key that decides "same occurrence") must not silently resolve this ambiguity by a **directional
merge**. Keying the signature coarsely — e.g. by category rather than the exact item — will collapse
two *genuinely distinct* same-day, same-value, same-category purchases into one, committing a
**wrong-low** total with no judge involved. "Undercount is the safe error" is a *false* comfort here:
§2 defines safety as **abstention**, not a directional bet — a wrong-low authoritative total misleads
exactly as a wrong-high one does. The signature must merge only the *certain* case (identical mention);
any coarser collapse is the 4d ambiguity and belongs to the judge (the "determinism proposes, model
judges" pattern, §6b) or the interval rung (§9), never to a silent deterministic merge. The same
warning applies to the judge itself: a *biased-but-unanimous* judge merges wrongly (4a applies to
judges too), which is why the merge decision stays confidence-gated and abstains when unsure.

### 4e. Anchor + delta (the reset corner)
A user states a total ("I have N"), then reports incremental changes ("got another") without
re-stating. The current value is `anchor + changes-after-the-anchor`, which no single reducer
computes; it requires reconciling a stated base against later events, ordered by world time.

### 4f. Noisy verification
The obvious guard against 4a is a second, independent derivation to cross-check the first. But a
*blind* re-derivation (re-read everything, produce the total again) is itself noisy on hard inputs,
and will sometimes veto a *correct* aggregate because the second guess missed an item. A noisy guard
false-vetoes as readily as it catches.

---

## 5. The architectural response: a conjunctive veto-gate

The aggregate is committed only if it clears a chain of **independent, abstain-only vetoes**. The
shape matters as much as the contents:

- **Conjunctive, not scored.** Any single red flag abstains. There is no weighted confidence score,
  because (a) calibrating weights needs far more labeled data than these problems ever have, and (b) a
  scalar hides exactly the bias 4a exploits. Orthogonal vetoes each catch a different failure; you
  raise precision by *adding a veto*, not by tuning a number.
- **Abstain-only.** A veto may block a commit; it may never *rescue* a value an earlier gate rejected.
  Gates run on the commit path only, so their composition can never manufacture a write.
- **Missing signals never veto.** A guard that doesn't apply (no stated total to triangulate against,
  no items to audit) is a no-op, not a failure. This keeps each layer independently shippable and
  precision-monotonic: adding a guard can only remove wrong writes, never add them.

The vetoes, mapped to the failures they catch:

| guard | catches | mechanism |
|---|---|---|
| **self-consistency** | extraction *variance* (4-none directly; the primary noise filter) | k-sample the extractor; commit only if the derived value is concentrated (near-unanimous). |
| **floor** | trivial non-aggregates | a count/total below a meaningful threshold carries no aggregation (it is the raw fact); don't materialize it. |
| **triangulation (stated)** | 4a, when the user stated a total | the itemized fold must agree with the user's own stated total, or abstain. |
| **triangulation (derived)** | 4a, generally | an *independent* derivation of the same scalar (by a different method) must agree. Catches bias — but is itself subject to 4f. |
| **verification** | 4a, 4c | a focused audit of the assembled candidate against *its own constituent items* — all on-topic? none double-counted? arithmetic sound? Reviews the evidence rather than re-guessing, so it is a sharper second opinion than blind derivation. |
| **reconcile** | 4e | when a stated anchor and later events coexist, compute `anchor + reduce(events after the anchor)`. |

Two structural patterns recur inside these and deserve their own section.

---

## 6. Two design patterns worth naming

### 6a. Decompose a global judgment into a local judgment + deterministic composition
When you need the model to make a decision it is *bad* at, check whether it is bad at the whole
decision or only at the *global* framing. Grouping heterogeneous items (4b) fails as a global ask
("put these all under one key") but succeeds when **decomposed**: ask the model the *local* question
per item ("what theme does this one thing belong to?"), which is stable and independent, then do the
grouping **deterministically** by that tag. The model does classification (which it is good at and
stable on); the system does aggregation (which it must do deterministically anyway). The general
lesson: *push the LLM toward local, self-contained judgments; keep composition in the deterministic
core.*

### 6b. Determinism proposes, the model judges, confidence gates
For the ambiguities determinism genuinely cannot resolve (4d — is this the same event or a recurrence?),
the pattern is a three-stage hand-off:

1. **Determinism finds the candidates** — cheaply narrow the space to the ambiguous cases (same value,
   same theme, different day). This bounds cost and keeps the model out of the easy cases.
2. **The model makes the judgment call** — on each candidate, and *only* each candidate, ask the
   semantic question determinism couldn't answer.
3. **Confidence gates the action** — sample the judgment; act only when it is stable (self-consistent).
   An unsure judgment declines to act, and an earlier/later veto covers the un-acted case.

This keeps the expensive, fallible model use *surgical* (a handful of judged candidates, not a re-read
of everything) and *fail-safe* (act only on confidence, abstain otherwise). It is the general recipe
for "determinism can't, but the model can, and we don't fully trust the model."

**Evaluated external approach (2026-07-04, not adopted): TabFM.** Google's zero-shot tabular
foundation model (row-column attention + in-context learning, open weights) was assessed against this
problem. It is a *non-fit for the aggregation itself*: it predicts a target column, it does not
cluster, dedup, or abstain, and a learned point estimate is exactly the weighted score §7 forbids —
it has no "unsure → fall back to raw episodes" path, so it breaks the §2 asymmetry. The *one* place it
plausibly fits is **stage 2 of this hand-off**: the coreference merge/separate/unsure call is a
classification over tabular features `(kind, value, day, wording-similarity, category)`, and TabFM's
class probabilities map onto the tri-state gate (mid-band → `unsure` → the `unresolved_coreference`
veto). It would sit *under* the deterministic fold and the veto, never replacing them, and would ship
only via shadow → flag → oracle-slice A/B vs the current LLM judge. Recorded, not scheduled. (TabFM's
strongest workspace fit is elsewhere — supervised card-price / pull-rate regression in yawn.market,
which carries none of these precision-asymmetry objections.)

A note on verification (§5, and the sharper form of triangulation): **auditing assembled evidence
beats blind re-derivation.** When you want a second opinion on a computed aggregate, showing the judge
the *actual constituent items* and asking "is this right?" is lower-variance than asking it to
independently recompute the answer from scratch — the audit constrains the model to the evidence,
where the re-derivation lets it wander.

---

## 7. Confidence is a gate, never a stored score

A recurring temptation is to attach a confidence number to the stored aggregate. Resist it:

- On the failure that matters most (4a, confident bias), any confidence derived from the system's own
  agreement is **anti-correlated with correctness** — it is highest exactly when the system is
  confidently wrong. Storing it would make the ranking *prefer* the wrong facts.
- A *fitted* confidence needs calibration data these problems never have enough of, and would fit
  noise.
- A stored probability inevitably becomes a ranking signal, violating §3.

Keep the deterministic *evidence* of the decision (agreement fraction, which guards fired, the
constituent provenance) as an **audit receipt** — useful for debugging and observability, explicitly
*not* a ranking input and *not* a soft gate. The commit decision stays binary.

---

## 8. Knowing when to stop: irreducible difficulty vs a missing mechanism

The hardest discipline in this problem is distinguishing two situations that *look* identical from the
outside (an aggregate that won't commit):

- **A missing mechanism** — there is a real failure mode with no guard, and building the guard fixes a
  whole class of inputs. Worth doing.
- **Irreducible difficulty** — the input is genuinely ambiguous (re-narration with conflicting dates,
  heterogeneous items, a noisy second opinion), every applicable mechanism is present and correct, and
  the residual is *stochastic variance on a hard case*. Here the aggregate safely abstains, and that
  is the **correct** outcome.

The tell: when you add a mechanism and the failure **moves** rather than disappears — from "wrong
value" to "unconfirmable value" to "value that varies across samples" — you are chasing irreducible
difficulty, and each further push buys a single hard case at rising risk to the whole. The precision
guarantee holds throughout (nothing wrong is ever written); forcing the commit would mean *lowering a
threshold* or *trusting an uncorroborated value* — trading the guarantee for a marginal recall gain on
one input. **Do not.** A safe abstain on a genuinely ambiguous input is the system working, not
failing.

Corollary — **do not build to the measurement.** An evaluation corpus is an instrument for detecting
missing mechanisms, not a target to fit. The firewall: keep all corpus-specific knowledge in the
harness; the production code must contain zero of it — no identifiers, no gold values, no
example vocabulary drawn from the eval. Validate every mechanism on invented inputs the corpus never
contained; if it only works on the corpus, it is overfitting wearing the costume of a feature.

---

## 9. Buying recall back without touching precision

§8 leaves a bind: the veto-chain buys precision by sacrificing recall, and we forbid buying recall
back by loosening a gate. Every legitimate way "around" an abstention must therefore add recall
through a channel that **does not touch the precision guarantee**. There are exactly two such axes,
and every safe channel is one of them:

- **Axis A — weaken the *claim*** so a "wrong" value is either not wrong or not authoritative.
- **Axis B — feed the *unchanged gate* better, later, or more-independent evidence.**

A proposed "recall fix" that fits neither axis is a disguised gate-softening. Reject it.

### The organizing law: corroboration independence
A corroborator can only catch errors it is **causally independent of**. This single principle explains
§7 (why self-agreement can't catch bias) *and* ranks every evidence channel. But independence is
**two-dimensional**, mirroring §4a's variance/bias split: independence from the extraction's *random
draw* (needed to catch variance) and independence from the extractor's *mechanism* (needed to catch
bias). Score every corroborator on both:

| corroborator | draw-independent (variance) | mechanism-independent (bias) |
|---|---|---|
| self-consistency (k-sample of one extractor) | yes — that is all it is | no — same model, same prompt |
| 2nd-model derivation / evidence audit | yes | partial — shared family and failure modes |
| committed-store invariants (cross-aggregate) | yes | **weak — the same pipeline built the parent** |
| later user re-statement (deferred re-gate) | yes | yes — model-free; residual is user error |
| direct user answer | yes | yes — gold, but rate-limited |

The easy mistake is rating store invariants "largely exogenous": they are exogenous to *this*
extraction's noise, but the parent total came from the same model and prompts — a systematic
double-count inflates parent and child alike, and the invariant passes. On the bias column, a store
invariant ranks *below* a second-model derivation.

The top rungs carry a different subtlety: a user statement is exogenous to model error but not to
*user* error. That is acceptable for a reason beyond independence — §2's asymmetry is about
misleading the user, and a value the user themselves asserted cannot mislead them the same way.
User-sourced corroboration quietly converts the correctness question into a faithfulness question,
which is why it is terminal, not merely strongest.

Rank Axis-B channels by these two columns — not by leverage or cost. Only a mechanism-independent
source can catch what self-agreement structurally cannot.

### The channels (Axis A: weaker claim)
Axis A is not a bag of separate tricks but one **claim-strength lattice**:

> authoritative point value → certified interval/bound → provisional value + evidence →
> evidence-set only → nothing

The gate chain's output is not commit/abstain; it is **the strongest rung it can certify**. Each rung
keeps its own abstain-only vetoes — a candidate failing a rung's gates falls to the next weaker rung,
never gets rescued upward — so the precision guarantee becomes rung-relative: *never store a claim
stronger than its certification.* Claim strength also has a **scope dimension**: an aggregate that
fails as "total spend on hobby X" may pass as "total spend on lights" — narrower key, cleaner
evidence. Committing certified sub-facts (and separately, usually never, the partition claim that
they are exhaustive — that is 4b again) is recall bought entirely inside this axis.

- **Provisional tier.** A second storage class for gate-failed candidates that (a) never out-ranks
  raw episodes, (b) always renders its constituent items alongside the value, (c) is phrased as a
  hedge. A wrong provisional value costs little *if the reader sees the evidence beside the claim*.
  **This is a rendering-contract decision, not a storage one** — humans anchor on a number regardless
  of the hedge word, so safety lives in a genuinely non-anchoring render (items first, number as a
  subordinate summary), not in a storage flag. And here the first reader of a recalled fact is not a
  human but the answering model — **LLMs flatten hedges harder than humans do** — so the rendering
  contract must bind the answer-composition prompt (constituents inline, number subordinate), not
  just a UI. Hard invariant: nothing may promote a provisional fact to authoritative except the real
  gate chain.
- **Weaker-but-certain claims (intervals / bounds).** When a veto fires on a *bounded, enumerable*
  ambiguity (dedup-or-not is $40 vs $80), fold *both* branches deterministically and store the
  interval ("$40–$80") or the certified bound ("≥ $90"). Not a hedge score — a point-precise,
  unit-testable claim about a *set*, which cannot be wrong if the branch enumeration is exhaustive.
  Three hard guards: (1) exhaustiveness is load-bearing — a non-exhaustive branch set reintroduces
  confident bias *at the interval level*; (2) intervals **compose badly** — combining correlated
  ambiguities by naive interval arithmetic manufactures false width (it discards the correlation that
  makes the true range narrower); (3) branch sets must come from the **deterministic proposer** (§6b
  step 1) — dedup-or-not is two branches by construction; letting the model *imagine* the branches
  makes exhaustiveness unverifiable, and exhaustiveness is the entire soundness argument. Intervals
  also inherit the provisional tier's rendering contract: any consumer that summarizes "$40–$80" as
  "~$60" has silently converted a certain claim into a false point value. Use only for small,
  enumerable, non-composing ambiguities.
- **Evidence-set only (the weakest useful rung).** Materialize the *selection* without any folded
  value: the 6a-tagged shortlist of constituent episodes for the theme. It asserts only "these
  episodes are the evidence for this question" — it cannot be authoritatively wrong about a value,
  and it turns query-time fallback from "re-read everything" into "fold this shortlist". The cheapest
  claim on the lattice, and often the right landing spot for candidates killed by self-consistency
  variance: no value is stable, but the evidence set is.

### The channels (Axis B: more/independent evidence to the same gate)
- **Deferred re-gating.** Park a gate-failed *intent* ("an aggregate for theme Y is wanted,
  unconfirmed") — **never the candidate value**. A retry that can see the previous value is anchored
  by it, which destroys the independence the retry exists to provide; the leak-no-prior rule below
  applies to future-self as much as to the user. When a new relevant episode arrives, re-derive blind
  and re-run the *unchanged* chain — a later user re-statement is exactly the exogenous stated-total
  signal the gate lacked at first attempt. Free precision-wise (gates unchanged); needs a TTL/eviction
  policy and idempotent re-gating (re-running must not duplicate). One humility: later evidence is not
  monotonically better — time brings re-narrations (4c fuel) along with corroborations, so a commit on
  attempt three earns no more trust than one on attempt one.
- **Ask the user.** For a candidate failing exactly one veto on an enumerable ambiguity, surface one
  budgeted question at a natural moment. The maximally-independent oracle. Construct the question to
  leak no prior ("were these the same purchase?", never "these were the same, right?"). The answer
  enters as a new event type into the *existing* deterministic fold — no new trust path.
- **Cross-aggregate consistency (accounting invariants).** Check a candidate against already
  committed aggregates that constrain it: a category total exceeding its committed parent total is
  vetoed. **Strictly veto-only** — consistency is a no-op, never a corroboration that substitutes for
  a missing signal elsewhere in the chain; otherwise a wrong committed parent *launders* a wrong child
  through, and gate composition manufactures a write (§5). Deterministic and zero model calls, but see
  the independence table: the parent came from the same pipeline, so this catches variance-class and
  keying errors, never a bias the parent shares. Two structural cautions: (1) **temporal skew** — the
  parent was committed at t₁, the child folds events through t₂ > t₁; a child exceeding a stale parent
  is evidence of parent *staleness*, not child error, so compare same-horizon aggregates only and
  treat a violation as a trigger to re-gate the parent (composing with deferred re-gating), never as a
  permanent child veto. (2) **Coupling** — invariants make committed facts load-bearing for future
  commits: under veto-only semantics a wrong parent costs recall (it blocks correct children), never
  precision — an acceptable trade, but it makes repairing wrong parents urgent rather than optional.
- **Ingest-time linking (fix perception early).** Do the §6b judgment (is this the same occurrence as
  a recent same-theme event?) at *ingest*, while conversational context is fresh and the judgment is
  easiest, so events arrive pre-linked and the fold sees fewer ambiguities. **Critical constraint:
  this fix must itself abstain.** A confidently-wrong link made at ingest is *worse* than a fold-time
  double-count — it wears a provenance badge, becomes indistinguishable from real structure, and no
  later gate can tell it was a guess. Early perception repair inherits the entire abstain-only
  discipline and defaults to "unlinked". One recoverable loss remains: the ingest judge sees
  conversational context that then evaporates, so an abstention destroys the evidence that made the
  judgment easy. Split **evidence capture from judgment** — even when linking abstains, persist the
  *observations* that informed it ("anaphoric 'those lights' referring back", "narrated in past
  perfect") as provenance annotations. Annotations are evidence, not links: the fold-time proposer
  (§6b step 1) consumes them to narrow candidates, at no ingest-time commitment.

### Which channel, when: let the receipts decide
Channels are not built speculatively — that would be §8's build-to-the-measurement wearing a recall
costume. The audit receipts (§7) already record which veto killed each candidate; bucket real
abstentions by firing veto and build the channel that converts the dominant bucket:

| firing veto | best conversion channel |
|---|---|
| self-consistency (variance) | often irreducible — deferred re-gate; else land on the evidence-set rung |
| floor | none — correct abstention by design |
| triangulation (stated), signal missing | deferred re-gate (a stated total may yet arrive); then ask-user |
| triangulation (derived), disagreement | evidence audit is the sharper form; interval rung if the disagreement is an enumerable branch |
| verification, suspected double-count | interval/bound rung or ask-user; ingest-time linking reduces incidence prospectively |
| reconcile, anchor conflict | ask-user or interval rung |

### Anti-patterns (recall fixes that reintroduce bad values)
- **Commit-then-retract** ("store now, recompute later, delete if it drifts"): there is a window where
  a wrong value serves authoritatively — the exact harm §2 forbids. Retraction is repair, not
  prevention.
- **Softening the conjunctive gate into a weighted score** — hides the bias failure (§7) and needs
  calibration data that does not exist.
- **Tuning k or agreement thresholds on the evaluation corpus** — §8's build-to-the-measurement.

---

## 10. Summary — the principles, portable

1. A wrong stored aggregate is far worse than a missing one; **abstention is the safe and free
   default.** Tune to precision; let recall fall out.
2. **Perception probabilistic, fold deterministic.** No probability in the reducer or the stored value.
3. Guard with a **conjunctive chain of orthogonal, abstain-only vetoes**, not a scalar confidence.
   Raise precision by adding a veto.
4. **Self-consistency catches variance, not bias.** Bias needs an independent check; a *focused audit
   of the evidence* beats a *blind re-derivation*.
5. **Decompose global judgments** the model is bad at into local judgments it is good at, composed
   deterministically.
6. For irreducible ambiguity: **determinism proposes candidates, the model judges, confidence gates
   the action.** Keep model use surgical and fail-safe.
7. Confidence is a **gate and a receipt, never a stored score.**
8. When a failure **moves** under new mechanisms instead of resolving, it is **irreducible difficulty**
   — abstain and stop. Never trade the precision guarantee for one hard input. Never build to the
   measurement.
9. Buy recall back only through channels that don't touch the gate: **weaken the claim** — commit the
   strongest rung of the claim-strength lattice the gates will certify (point value → interval →
   provisional → evidence-set) — or **feed the same gate more-independent evidence**. Score
   corroborators on *both* independence axes (draw/variance and mechanism/bias); only user-sourced
   signals are independent on both. Every corroborator is veto-only — consistency never rescues — and
   a retry must never see its own prior candidate. Fixing perception earlier only helps if the early
   fix *also* abstains. Let abstention receipts, bucketed by firing veto, decide which channel is
   worth building.
