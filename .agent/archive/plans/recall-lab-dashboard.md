# Recall Lab Dashboard

> **Archived 2026-08-11.** The read-only dashboard, concurrent experiment arms, privacy behavior,
> experiment persistence, and regression coverage are implemented.

## Why

- Recall tuning is currently exercised through one-off scripts, which makes it hard to compare several configurations on the same query.
- Operators need to see rank, score, source attribution, and latency together before changing a retrieval default.

## Scope

- Add a read-only Recall Lab page to the existing Explorer.
- Run selected production/A/B/C/D/E/F/G configurations concurrently through the runtime's canonical `RecallService`.
- Expose the active retrieval, content-vector, fact-edge, oracle, and warden controls that currently affect recall.
- Show ranked results, trace attribution, timing, overlap, and rank movement side by side.
- Keep database selection in server configuration; the browser cannot supply credentials or switch targets.

Out of scope: persisting experiments, changing defaults, writes/access reinforcement, automatic quality judgments, and enabling controls that are declared but not implemented.

## Proposed Design

- Extend the existing FastAPI/Jinja Explorer with `/explorer/recall-lab` and `/explorer/api/recall-lab/run`.
- Resolve `RecallService` from the already-started runtime (or an explicit test injection).
- Validate a bounded list of arm configurations, then execute them with `asyncio.gather`.
- Force `trace=True` and `update_access=False` for every arm. Return arm failures independently.
- Apply the Explorer's existing privacy redaction policy before returning memory text.
- Render a horizontally comparable result grid in a dedicated template and static JS/CSS.
- Preserve D as the original active-facet/warden/oracle control. E is the working copy: it starts
  from the same production candidate path and frontier flags but treats absent evidence anchors as
  unknown by disabling the evidence-anchor hard gate.
- Keep F and G as isolated diagnostic arms: F applies only active facet rank fusion to Production's
  candidate path, while G applies only oracle/intent ranking, with no facet or active warden gate.

## Alternatives Considered

- Separate dashboard process: rejected because it would duplicate runtime construction and database/auth configuration.
- Browser calls to the normal recall API: rejected because that API does not expose per-call tuning and would require multiple sequential client requests.
- Benchmark script only: retained for repeatable evaluation, but insufficient for interactive exploration.

## Risks

- Concurrent arms multiply embedding/search load; cap the number of arms and candidate counts.
- Trace/shadow options can add work and make latency comparisons configuration-dependent; show the exact configuration with every result.
- Rank comparison can be mistaken for answer quality; the UI reports retrieval behavior, not an automated correctness verdict.
- Privacy must be applied to JSON responses as well as rendered HTML.

## Invariants

- Dashboard runs never update access timestamps, reinforce edges, or schedule rehydration.
- Production recall behavior and defaults remain unchanged.
- Explorer authentication and loopback privacy rules continue to apply.
- One failed arm does not suppress successful arms.

## Validation

- Unit-test page rendering and request validation.
- Verify selected arms overlap in execution and all calls use `update_access=False` and `trace=True`.
- Verify tuning is passed exactly and a failed arm is isolated.
- Verify privacy mode masks result names/content.
- Run focused Explorer and retrieval tests.

## Docs To Update

- This plan is the operator/developer contract for the initial pass.
- No architecture or data-model change: no new durable type, store, or production recall default.
