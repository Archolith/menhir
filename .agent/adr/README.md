# Architecture Decision Records

Accepted Menhir architecture decisions, in sequence:

| ADR | Status | Decision |
|---|---|---|
| [0001](0001-conversation-turn-capture-surface.md) | Accepted target; implementation followed | Capture selective user-authored evidence as `:TurnEvidence`, separate from memory. |
| [0002](0002-menhir-production-ingress-ownership.md) | Corrected | Cloudflared is the sole Menhir production ingress. |
| [0003](0003-event-fold-view-projection-boundary.md) | Accepted retrospectively | Preserve evidence and derive rebuildable Views through deterministic folds. |
| [0004](0004-single-runtime-owner-and-backend-first-access.md) | Accepted retrospectively | `menhir serve` owns the runtime; MCP and REST use the backend contract. |
| [0005](0005-core-enforced-namespace-isolation.md) | Accepted retrospectively | Enforce configured namespace isolation below transport-specific code. |
| [0006](0006-recoverable-sagas-for-cross-store-mutations.md) | Accepted retrospectively | Journal cross-store mutation intent before side effects and reconcile interrupted work. |
| [0007](0007-evidence-gated-default-off-feature-activation.md) | Accepted | Activate high-impact features only after evidence and an explicit owner decision. |
| [0008](0008-separate-identity-embodiment-and-locator.md) | Accepted retrospectively | Keep semantic identity separate from embodiments, locators, and declarations. |

ADRs record durable decisions and their tradeoffs. Live operational shape remains in
[`../architecture.md`](../architecture.md), exact graph/storage contracts in
[`../data_models.md`](../data_models.md), and procedures in [`../workflows/`](../workflows/).

When a decision changes, add or correct an ADR explicitly; do not make an old accepted decision
silently mean something different. ADR 0002 is the model for recording a factual correction.
