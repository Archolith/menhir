# menhir — make namespace a storage invariant on :Todo

> **ARCHIVED 2026-08-10.** All three steps shipped in `c6b5e43`, including the live-graph
> backfill. This document is retained as the migration and invariant decision record.

Status: IMPLEMENTED in `c6b5e43` (all three steps). Backfill applied to the live graph.
Date: 2026-08-02

## Problem

`:Todo` is the only operational surface in menhir that ignores namespace silos.

Evidence from the live graph (231 todos):

- 166 nodes carry `namespace='default'`; 65 carry none. The three newest (2026-07-21..26)
  have none, so the property is no longer written at all.
- `add_todo` has **no** `namespace` parameter, unlike `add_memory` (`namespace: str = ""`).
- `create_todo` dispatches through the generic `_BACKEND_METHODS` path, so it never reaches
  `_resolve_namespace` (`routes_support.py:124`). The 166 populated nodes came from a writer
  that no longer exists.
- `list_todos` / `get_todo` accept no namespace argument and apply no filter.

Consequence: every client sees every todo regardless of silo. With 114 open todos spanning
archolith-context, yawn.market, yawn.seed and cth.harness, the list is unscopeable.

`group_id` is empty string on all 166 populated nodes — a separate vestigial field. It is
**not** part of this change and is left untouched.

## Decision

Namespace becomes an invariant on the stored node, not a required argument on the API.

Rejecting calls that omit `namespace` would break every existing caller (the hooks, and any
agent calling `add_todo` today) and would make `:Todo` the only entity type with a hard
requirement — `_resolve_namespace` explicitly documents `None` as "legacy global behavior"
for memories. So: **never-null in storage, defaulted at write.**

Resolution precedence mirrors memories: explicit argument -> `x-yawn-namespace` header ->
`'default'`.

## Steps (order is load-bearing)

1. **Backfill** the 65 null nodes to `'default'`.
2. **Write** namespace on create — `add_todo` gains the parameter; `create_todo` always
   persists a non-null value.
3. **Filter** on read — `list_todos` / `get_todo` gain an optional `namespace`.

Reversing 1 and 3 hides 65 todos, **41 of them open**, from every listing.

## Read-filter shape

All 231 existing todos are `default` or null. A strict-equality filter would show a client
pinned to another namespace (via `MENHIR_CLIENT_NAMESPACES`) **zero** todos.

Therefore the filter is `namespace IN [$requested, 'default']` — the requested silo plus the
shared bucket — and it is opt-in: omitting the argument preserves today's unfiltered
behavior. `get_todo` is a direct uuid lookup, so it reports the namespace in its output
always and only enforces the filter when one is supplied.

## Touch points

`todo_repository.py`, `memory_graph_adapter.py`, `backend_protocol.py`,
`backend_client_ops.py`, `backend_runtime_admin_ops.py`, `mcp/tools/ops/add_todo.py`,
`list_todos.py`, `get_todo.py`, `tests/test_todo.py`.

Docs: `.agent/endpoints.md`, `.agent/data_models.md` (`model.todo` node schema).

## Out of scope

The other findings from the same exploration, left for a follow-up: `CONCERNS` substring
false positives (57 of 77 `main` links are from "remaining"/"domain"/"maintenance"),
`REFERENCES_FILE` linking only 13 of 77 todos that carry a `code_ref`, the dead
`due_date`/`HAS_REMINDER` branch (0 of 231 nodes, 0 edges), the legacy `completed` property
on 51 nodes, and the absence of any inbound edge (supersedes / parent-child / blocked-by).
