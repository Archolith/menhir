# Menhir Module Map

Exhaustive, disjoint partition of `src/menhir/` for audit scoping. Every file
belongs to exactly one module. Counts verified by
`menhir_audit_probe.py` at commit `eebf6d6dd83f15083167bf847b639d24b953fdc9`.

| # | Module | Path scope | Files | Lines |
|---|--------|-----------|------:|------:|
| M1 | Domain algebra and types | `domain/` | 58 | 12,107 |
| M2 | Transport, API, OAuth | `api/` | 24 | 5,565 |
| M3 | MCP surface | `mcp/` | 70 | 7,222 |
| M4 | Core runtime and backend | `core/` + root `*.py` | 23 | 5,097 |
| M5 | Config and settings | `config/` | 7 | 1,394 |
| M6 | Perception and scalar typing | `services/` subset | 21 | 11,126 |
| M7 | Ingest, lifecycle, scheduling | `services/` subset | 31 | 9,633 |
| M8 | Recall, retrieval, events | `services/` subset | 23 | 8,549 |
| M9 | Persistence and infrastructure | `infrastructure/` | 73 | 31,626 |
| M10 | Explorer and research labs | `explorer/` | 29 | 10,988 |
| M11 | CLI and pipeline | `cli/` + `pipeline/` | 10 | 2,485 |

Total: 369 files, 105,792 lines.

The `services/` directory is split three ways because it holds 75 files across
three unrelated concerns. The other modules map to a single directory and can be
passed to the probe directly.

## M6 - Perception and scalar typing (21)

`assertion_pipeline` `compositional_scalar_identity` `correction_resolver`
`deterministic_scalar_extractor` `deterministic_scalar_router` `perception`
`perception_report` `quantstate_consolidator` `research_scalar_adapter`
`research_scalar_clause_isolator` `research_scalar_dependency_bridge`
`research_scalar_dependency_rules` `research_scalar_isolated_adapter`
`scalar_consolidation` `scalar_state_service` `structural_scalar_composer`
`typed_scalar_perception` `typed_scalar_persistence`
`typed_scalar_proposer_reviewer` `typed_scalar_rules` `typed_scalar_service`

## M7 - Ingest, lifecycle, scheduling (31)

`artifact_reconciliation_service` `artifact_service` `candidate_service`
`change_log_provider` `delete_coordinator` `enrichment_failures`
`enrichment_steps` `failure_counter_bridge` `ingest_gate` `ingest_intake`
`ingest_models` `ingest_queue` `ingest_service` `ingest_worker`
`instability_counter_bridge` `legacy_unmerge_coordinator` `lifecycle_conflicts`
`lifecycle_consolidation` `lifecycle_decay` `lifecycle_models`
`lifecycle_service` `maintenance_scheduler` `merge_coordinator`
`merge_recoverability` `metric_write_coordinator` `project_ingest`
`scheduler_lease` `scheduler_protocols` `scheduler_tasks` `unmerge_coordinator`
`verifier_sync`

## M8 - Recall, retrieval, events (23)

`__init__` `context_builder` `correlation_service` `event_consolidation`
`event_fold` `event_history_authority` `event_history_perception`
`event_history_recall` `event_history_service` `hybrid_retrieval`
`memory_oracle_service` `oracle_executor` `recall_pipeline` `recall_policies`
`recall_service` `recall_support` `retrieval_oracles` `scoring_service`
`shadow_context_composition` `stale_labeling` `view_entropy` `windowed_fold`
`windowed_recall`

## Note on line counts

Three of these figures were originally estimates and were wrong: M6 by 2,474
lines, M7 by ~270, M8 by ~750. Estimated scope in a brief invites a lane to
"reconcile" against the estimate rather than the tree. Always have the lane
measure and report its own total; the numbers above come from the probe.
