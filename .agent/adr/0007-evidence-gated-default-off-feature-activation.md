# ADR 0007 — Evidence-Gated, Default-Off Feature Activation

- **Status:** ACCEPTED (2026-09-21). This formalizes the rollout rule already used by Menhir's
  retrieval, perception, consolidation, and authority features.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/default-off-features.md`, `.agent/memory-governance.md`,
  `.agent/plans/menhir-research-execution-ladder.md`,
  `.agent/plans/menhir-feature-flag-registry.md`

## Context

Menhir contains several production-capable paths whose measured value, precision, or operating
envelope has not earned default enablement. Earlier read-side retrieval work was built successfully
but measured neutral-to-negative on LongMemEval. Other paths can change which claims become current,
which evidence reaches a caller, or whether a model-derived proposal gains authority.

The repository therefore maintains a default-off ledger and uses shadow/observe modes, benchmark
gates, and explicit owner decisions. Without an ADR, “implemented,” “shipped,” and “active” can
still be conflated.

## Decision drivers

- Code completion is not evidence of production value or acceptable false-positive cost.
- Recall, authority, and consolidation changes can be wrong while remaining internally consistent.
- Operators need to know which behavior is live in each deployment.
- Measurements must be comparable to a pinned baseline and leave diagnostic receipts.
- Dormant flags must have owners and exit conditions instead of becoming permanent ambiguity.

## Decision

Features that materially change retrieval selection/ranking, memory admission, derived current
state, authority, automated consolidation, or destructive lifecycle behavior ship **default-off**
until they earn activation.

Activation requires:

1. **A named owner and decision surface.** The feature, flag, gate location, source design, and
   current evidence are recorded in the activation ledger or its successor registry.
2. **A pinned baseline and success rule.** The evaluation states what is compared, which metric can
   justify promotion, and what regression blocks it.
3. **Representative evidence.** Synthetic unit correctness is necessary but does not substitute for
   a relevant benchmark, corpus, shadow run, or operational acceptance check.
4. **A safe observation phase where practical.** Shadow/observe mode records bounded, non-semantic
   diagnostics without changing returned results or durable authority.
5. **Explicit owner activation.** Passing a gate makes a feature eligible; it does not silently flip
   the default. The activation date and deployment scope are recorded.
6. **One canonical configuration value per process.** Runtime consumers use the immutable settings
   snapshot rather than rereading environment variables and disagreeing about whether the feature
   is on.
7. **A retirement path.** When a feature becomes default-on or is abandoned, its ledger entry moves
   to an activated/retired state and stale flags are removed deliberately.

## Important exception: safety and correctness fixes

This policy does not require known security, privacy, corruption, or correctness defects to remain
available behind an off switch while an A/B runs. A fix whose desired behavior is already defined by
an accepted invariant should ship fail-safe and receive direct regression/acceptance evidence. The
default-off gate applies when the behavior or value is still an empirical product/architecture
choice, not when the old behavior is known to violate the contract.

## Considered alternatives

### Enable every code-complete feature

Rejected. Build success proves implementation, not retrieval value, authority precision, or an
acceptable operating envelope.

### Let each module choose its own default and documentation

Rejected. A deployment could run a combination no one can reconstruct, and stale flags would have
no owner.

### Keep uncertain features permanently in shadow mode

Rejected. Observation without a promotion or retirement decision turns flags into unowned
architecture.

### Flip defaults automatically when a benchmark threshold passes

Rejected. Bench evidence is an input to an owner decision; it does not know deployment risk,
corpus differences, or unresolved operational dependencies.

## Consequences

- “Shipped” documentation must state separately whether the feature is active.
- Plans and changelogs must not claim production behavior from code presence alone.
- New high-impact flags add maintenance cost and need an evidence and retirement story.
- Operators may enable an eligible feature per deployment without changing the repository default,
  provided the activation and evidence are recorded.
- Safety fixes and contract-preserving hardening remain able to ship enabled.

## Evidence in the repository

- `.agent/default-off-features.md`
- `.agent/memory-governance.md`
- `src/menhir/config/settings_model.py`
- `src/menhir/domain/retrieval_tuning.py`
- `tests/test_settings.py`
- `tests/test_settings_event_history.py`
- `tests/test_settings_scalar_deterministic_shadow.py`

## Non-goals

This ADR does not approve the current default-off features, freeze a particular benchmark forever,
or require a feature flag for every internal refactor.
