# Plan: Capability separation for user-tier writes

<!-- Filename convention: <feature>-plan.md -->

**Status:** backlog — proposed 2026-07-11, **REVISED 2026-07-12** (design session with ctharvey;
supersedes the original Step 1-4 "Path" below, kept for history under "Superseded original path").
**Gap source:** `docs/research/privacy/trusted-memory-admission.md` (the "now-ish, cheap"
capability-separation rung of the user-identity ladder)
**Related:** `foundation-typed-admission-plan.md` (basis-class gate — the complementary write-time
admission facet), `identity-keying-layer-plan.md` (deterministic identity).

---

## REVISED design (2026-07-12) — turn-evidence-grounded admission, not a human-only UI

The original plan (below) assumed the fix needed a new human-direct write surface (e.g. an
explorer capability token) distinct from the agent/MCP path. Investigation found a better-grounded
answer already half-built in the codebase.

### Pipeline trace: what already exists (both halves ungated today)

**Pipeline A — real human keystrokes, but capped below "user" tier.** `UserPromptSubmit` fires only
on an actual human keystroke in Claude Code (the agent cannot forge this event) -> `POST
/api/turn-evidence` -> `:TurnEvidence {role:'user', declarant:'user'}` (`turn_evidence_repository.py`,
shipped, see `archive/plans/turn-capture-claude-hook.md`). A scheduled job,
`consolidate_personal_memory` (`services/scheduler_tasks.py:376`), later folds these turns through
`perceive_and_fold(..., source="perception")` into **Views**, which `view_repository.py:217` writes
with a **hardcoded `source_confidence=0.6`** — completely independent of `source_confidence_for()`.
Real, unforgeable human-authored input never reaches the "user" (1.0) tier through this path, and it
never will need to (Views are a distinct, additive-aggregate use case — out of scope for this plan,
per ctharvey 2026-07-12).

**Pipeline B — `add_memory`/`ingest_service`, ungated caller-declared string.**
`mcp/tools/ingest/add_memory.py:12` exposes `source: str = "claude-code"`; `ingest_service.py:467`
threads it straight through with no validation; `domain/utils.py:source_confidence_for()` (line 61)
maps **two** strings to the apex 1.0 tier: `"user"` **and `"manual"`** (the plan's original text only
ever mentions `"user"` — `"manual"` is the same ungated hole under a different name and must be
closed in the same pass). This is the actual laundering vector the plan exists to close.

### The fix: gate Pipeline B using Pipeline A's grounding pattern, not its timing

Pipeline A already contains the right *pattern* for proving a claim, in `services/perception.py`:
- `_stated_value_grounded()` / `_price_token_count()` (lines 759-804): deterministic, LLM-free
  "is this claimed value literally present in the cited source span?" check — no match, no commit.
- Every `Event` carries `episode_uuid` provenance back to its source span — claim-to-evidence linking
  is already a first-class field, not bolted on.
- Design principle stated at the top of the file: fail closed / abstain-is-safe, never block the
  fallback path.

**Take the pattern, not the timing.** Views are computed by a *deferred, scheduled* fold because a
missed View is merely an annoying recall gap. Trust-tier admission is a **security boundary**: if the
grounding check ran on a delay, an ungrounded `source="user"` claim would sit in the graph at full 1.0
confidence — recallable and actionable — until the next batch pass corrected it. That is the exact
hole this plan closes, just deferred instead of prevented. So the check must run **synchronously,
inline in `ingest_service.py`'s write path, before the node is ever persisted at 1.0.**

### Design

1. `add_memory` / `add_memory_and_track` gain an optional `turn_evidence_uuid` param.
2. In `ingest_service.py`, before calling `source_confidence_for(source)`: if the caller requests
   `source in ("user", "manual")`, a valid `turn_evidence_uuid` is **required**. A small, pure gate
   function (borrowing `_stated_value_grounded`'s deterministic span-containment pattern, not the
   k-sample/LLM machinery — this is a single yes/no check per call, not an aggregate) verifies:
   - the cited `:TurnEvidence` node exists and `role == "user"`
   - it belongs to the same `session_id`/`namespace` as the current call (no citing an unrelated turn)
   - the claimed memory text is span-grounded in the turn's raw text (cheap containment check,
     deterministic, no LLM)
   Confidence is an **output of this gate, never a caller-supplied input** — same "compute, don't
   trust" principle Pipeline A already applies to Views.
3. On any failure (missing uuid, node not found, session/namespace mismatch, ungrounded text) ->
   fail closed to `agent_inference` tier, or route to the existing `proposed_user_info` candidate
   review loop (already shipped, no new mechanism needed) — never a hard reject.
4. One audit row per admission attempt (requested tier, final tier, `turn_evidence_uuid` or null,
   reason) — **reuse View/`GateDecision`-shaped storage** (`view_repository.py`) instead of a new
   schema/table. A `GateDecision` already carries `veto`, `reason`, and provenance uuids; an admission
   verdict is the same shape (verdict + reason + linked evidence uuid), just non-numeric. This also
   means the explorer's existing View-browsing surface picks up admission audit rows for free.
5. Agent workflow in practice: "user says X" this turn -> next turn, agent calls
   `add_memory(source="user", turn_evidence_uuid=<the turn that said X>)` -> gate verifies -> 1.0
   tier granted with real, checkable provenance attached instead of bare assertion.

### Explicitly out of scope

- Pipeline A / Views themselves — not touched, not re-tiered. They stay at their existing 0.6 and
  serve a different (aggregate/counting) use case.
- Crypto/signing/OAuth/DPoP — same parked frontier rung as the original plan (see Non-goals below).
- No new schema/table for the audit trail — reuse View storage (see Design step 4).

---

## Superseded original path (2026-07-11, kept for history)

---

## The gap (one line)

The `user` trust tier (`source_confidence = 1.0`) is **caller-declared and ungated** — any caller,
including an agent, can pass `source="user"` and launder an inference into a durable high-trust
user-fact.

## Current default (what menhir does today, code-anchored)

- `services/ingest_service.py` takes `source: str` as a plain parameter and threads it straight
  through (`source=source`) with **no entitlement check** on whether the caller may claim that source.
- `domain/truth/kinds.py` + `attestation.py` then **tier** the claim by that label
  (USER 1.0 / STRUCTURAL 0.9 / AGENT 0.5) and feed the warden/belief assertion gate.
- Agent harnesses (`claude-code`, `codex`) default to `agent_inference`, so an agent is *down-tiered
  by default* — **but nothing prevents a caller from explicitly passing `source="user"`** to claim the
  1.0 tier. The user tier is established by convention/honesty, not by proof or capability.

Net: the consumption side of provenance is shipped; the *establishment* of a genuine user-tier write
is not. At single-user scale the realistic failure is non-adversarial (an agent over-eagerly
self-tagging `source="user"`), not an external impersonator.

## Promotion criteria (default → gated)

The default flips from **"honor the caller-declared `source`"** to **"the user tier requires a
human-direct surface; the agent/MCP path cannot assign it."**

- **supported-by-spike** when all hold:
  1. A caller on the agent/MCP write path **cannot** set `source="user"` / tier 1.0 — the request is
     rejected or down-tiered (to `agent_inference`, or routed to `proposed_user_info`).
  2. A **human-direct write path** (privileged non-agent surface — explorer/dashboard or a local UI)
     *can* set the user tier, via a capability the agent path cannot forge (server-assigned, not
     request-body-declared).
  3. An **admission audit row** records requested-vs-final tier + reason for every user-tier attempt.
- **supported-by-eval** when an archolith-bench contamination fixture shows a **zero false durable
  `user_info` admission rate** for agent/tool/external claims, **without** dropping genuine
  user-asserted facts.

## Path (how to get there)

1. **Confirm the human-direct surface.** Verify whether menhir's explorer/review surface (or
   `yawn.dashboard`) offers a non-agent write path that a capability boundary can key on. If none
   exists, the first deliverable is that surface (out of scope here beyond identifying it).
2. **Capability boundary at ingest.** In `ingest_service` (and the MCP `add_memory` path), gate
   user-tier assignment on a **server-side capability flag/token** set only by the human-direct
   surface — never readable/settable from the agent request body. Agent-path `source="user"` →
   down-tier to `agent_inference` or route to a `proposed_user_info` candidate.
3. **Admission audit.** Add one audit row (requested tier, final tier, reason_code, actor/surface).
   Reuse the existing candidate/audit plumbing rather than a new store.
4. **Contamination fixture.** archolith-bench: agent attempts `source="user"` → down-tiered; a
   human-direct write → admitted at 1.0; measure false-admission rate + genuine-fact retention.

## Non-goals

- **No crypto / signing / OAuth / DPoP.** Cryptographic user-signing is the *parked* frontier rung in
  the source doc (needs a signing surface at the human-input boundary); this plan is the cheap
  capability-boundary rung for the current non-adversarial threat.
- No multi-user / hosted authz. No change to the shipped source-confidence tiers themselves.

## Risks

- **MCP client-identity limit.** The agent *is* the client; the boundary must live where the agent
  cannot spoof it (server-assigned capability from the human-direct surface). If the only write path
  is the agent MCP, capability separation is not enforceable and this collapses back to the parked
  signing rung — hence Step 1 gates the whole plan.
- Over-restriction: a legitimate user fact arriving via the agent path is down-tiered to
  `proposed_user_info`; mitigated by the promotion/candidate review loop (already shipped).

## Source

`docs/research/privacy/trusted-memory-admission.md` — "If revived, start here (the user-identity
ladder)", rung: *"Now-ish, cheap: capability separation — agents cannot set the user tier; a
human-direct write path can."* Code state confirmed 2026-07-11 (`ingest_service.py` ungated `source`).
