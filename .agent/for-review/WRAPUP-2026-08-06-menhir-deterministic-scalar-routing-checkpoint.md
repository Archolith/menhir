# WRAPUP — Menhir deterministic scalar routing checkpoint

**Date:** 2026-08-06
**Agent:** Codex orchestrator with Luna implementation/review agents
**Model:** GPT-5 (Codex desktop)
**Status:** PARTIAL
**Plan / Ticket:** In-thread approved deterministic scalar routing plan; no standalone ticket
**Worktree:** `C:\Users\you\IdeaProjects\projects\archolith\menhir`
**Branch:** `main`
**Commits:** `cdeb43bcec8d251db43758f0c76ff178224db27f`
**Verification Scope:** Menhir implementation commit `cdeb43bcec8d251db43758f0c76ff178224db27f`; gold-free offline measurement used the preserved 78-task fixture at `C:\Users\you\IdeaProjects\projects\archolith\archolith-bench\results\lme-ku-buildout\scalar-current-code-recall-v2-20260806\fixture-knowledge_update_subset-bba252a302e7.json`
**Docs Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-08-06-menhir-deterministic-scalar-routing-checkpoint.md`
**Changelog Updated:** Not done; existing unrelated changelog edits were intentionally excluded from this checkpoint

---

## Before Writing

The approved end state was checked backwards from the required safety properties: a class may bypass the LLM only when the current deterministic extractor produced a fully covered, validated proposal; every admitted class in that episode is explicitly promoted; and every error, ambiguity, zero-proposal episode, mixed promoted/unpromoted episode, or conversion inconsistency falls back to the existing LLM path. The configuration chain, service integration, audit output, adversarial receipt validation, focused tests, full suite, and 78-task measurement were all checked. The unresolved gap is extractor coverage, not routing safety.

---

## Summary

Added a default-off deterministic scalar router with a default-empty per-class promotion allowlist. Production extraction runs once; externally supplied extraction receipts are replay-verified. The router preserves episode order, sends ambiguous and unpromoted episodes through the existing batched LLM flow, and fails closed to all-LLM processing on extraction, contract, or decision-conversion errors. Audit telemetry reports route counts, reviewed episode counts, bounded failures, promoted classes, and route-by-class counts without episode content.

The preserved 78-task fixture showed that the current deterministic extractor is not ready for authority: 915 user episodes produced one typed proposal and zero deterministic bypasses, even when all known classes were hypothetically promoted. The approved production promotion set therefore remains empty. This checkpoint provides the safe promotion and fallback framework; improving dependency-based extraction coverage is the next track.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\deterministic_scalar_router.py` | Pure fail-closed router, class allowlist, receipt validation, route telemetry, and deterministic decision conversion. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\typed_scalar_service.py` | One-pass extraction, reviewed-subset LLM fallback, decision merge, shadow scoping, and truthful router audit. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\config\settings_model.py` | Default-off router flag and default-empty promoted-class configuration. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\core\runtime.py` | Runtime settings forwarding. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\api\routes_handlers.py` | API construction-path settings forwarding. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\maintenance_scheduler.py` | Scheduler configuration forwarding. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\scheduler_tasks.py` | Scalar pass wiring and bounded promotion metadata. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\scalar_consolidation.py` | Consolidation configuration and service construction wiring. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\tests\test_deterministic_scalar_router.py` | Routing, promotion, mixed-class, replay, malformed receipt, derived-class, and fail-closed tests. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\tests\test_deterministic_scalar_routing_service.py` | Service subset routing, fallback, audit, shadow reuse, and conversion-integrity tests. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\tests\test_settings_scalar_deterministic_router.py` | Default settings and every configuration hop. |

## Verification

- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin -p pytest_timeout tests/test_deterministic_scalar_router.py tests/test_deterministic_scalar_routing_service.py tests/test_settings_scalar_deterministic_router.py tests/test_gate_relaxations.py::test_service_forwards_reconcile_attribute_to_the_gate tests/test_deterministic_scalar_shadow.py tests/test_settings_scalar_deterministic_shadow.py -q` — `PASS` — `66 passed, 1 warning in 2.01s`.
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin -p pytest_timeout tests -q` — `PASS` — `5077 passed, 180 skipped, 16 warnings in 216.40s`.
- `C:\Users\you\AppData\Local\Programs\Python\Python312\Scripts\ruff.exe check --ignore E731,F401,F841 <router/wiring/test files>` — `PASS` — `All checks passed!`.
- `.\.venv\Scripts\python.exe -m py_compile <router/wiring modules>` — `PASS` — no output.
- `git diff --check` — `PASS` — no output.
- `.\.venv\Scripts\python.exe -u C:\tmp\measure_scalar_router_78.py` — `PASS` — fixture SHA-256 `bba252a302e7b257a0f7457fe97411f7de144aafd1c6b44c98a0e88ee8570907`; 78 tasks, 915 user episodes, 527 marked fully covered, one proposal, zero deterministic routes with an empty allowlist, zero deterministic routes with all known classes promoted, zero LLM calls, and zero graph writes.
- Independent Luna adversarial review of the final router/service/settings diff — `PASS` — no remaining concrete fail-open, skipped-episode, misleading-audit, or wiring defect found.
- `artifact_validate(artifact_type="wrapups", filename="WRAPUP-2026-08-06-menhir-deterministic-scalar-routing-checkpoint.md")` — `NOT RUN` — the wrapup artifact validator is not available in this Codex tool context; status remains `PARTIAL` as required by the wrapup workflow.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`

## Completion Checklist

- Plan / acceptance criteria completed: `yes` for the safe routing checkpoint; extractor coverage remains a separate follow-up
- Docs updated as required: `yes`
- Changelog updated as required: `no`; unrelated existing changelog edits were intentionally not absorbed
- Work committed: `yes`

## Assumptions

1. Deterministic routing remains disabled unless `MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_ROUTER` is explicitly enabled.
2. No class is granted deterministic authority unless it is explicitly listed in `MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_CLASSES`.
3. The current safe production recommendation is an empty promoted-class list.

## Risks / Gaps

1. The current template extractor has effectively no natural LongMemEval coverage: the 78-task fixture produced one proposal and zero bypass-eligible episodes.
2. This checkpoint provides no demonstrated LLM cost reduction yet.
3. The implementation commit is local and was not pushed as part of this checkpoint.
4. Separate uncommitted dependency-research and review files remain in the worktree and were intentionally excluded from the commit.

## Follow-Up Tasks

1. Resume the dependency-based detector work beginning with `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\domain\scalar_dependency_evidence.py` and the `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\research_scalar_*.py` adapters.
2. Evaluate the detector on independent natural-language examples that include misspellings, fragments, and poor grammar without adding benchmark-specific phrases.
3. Build class-level precision/coverage evidence and only then add a class to the promotion allowlist.
4. Keep the router in shadow/default-off mode until a class meets the agreed promotion threshold.
5. Push implementation commit `cdeb43bcec8d251db43758f0c76ff178224db27f` when remote publication is desired.
6. Run the wrapup artifact validator in a tool context that exposes it, then promote this document to `READY FOR REVIEW` if it passes.

## Notes

- Search anchor: `Menhir deterministic scalar routing checkpoint`, commit `cdeb43b`, date `2026-08-06`.
- This wrapup is the canonical restart point for the paused routing track.
