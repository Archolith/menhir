# Guardrails for MCP tools

Use this for any new tool and for any change to a tool's name, arguments, output, errors or
access. **Must** rules block a merge. **Should** rules need a stated reason in the PR to skip.

Menhir registers 62 permanent tools (`ALL_TOOLS` in `src/menhir/mcp/tools/__init__.py`), plus
4 snapshot-staging tools behind a flag. Measured production use concentrates on five of them
([#432]). Every tool added makes the others harder to find, so the first question is always
whether a new tool is needed at all.

## Before you add a tool

- **Must:** check whether an existing tool can do the job with one more argument or mode. If
  it can, extend it ([#436]).
- **Must:** name who calls it: an agent during ordinary work, an operator fixing the system, or
  an administrator changing authority. That decides its tier, and its profile once profiles
  exist ([#435]).
- **Must:** make it reachable in every deployment that registers it. If a human has to act on
  its output, the action must also exist there ([#431]). If it stays deliberately unexposed,
  record why and keep it out of agent docs ([#430]).
- **Should:** name the issue or evaluation that shows someone needs it.

## Name

- **Must:** `verb_object` in snake_case, using the verbs already in use: `list_`, `get_`, `add_`,
  `close_`, `recall_`, `resolve_`, `link_`.
- **Must:** use the reader's noun (memory, todo, artifact, conflict, project), not an internal
  component name.
- **Must not:** reuse a name across two surfaces with different meanings, or give one action two
  names.

## Title and description

The curated `description` class attribute is the line a model reads when choosing a tool. The
endpoint docstring follows it in the registered description and carries the argument details
(`registered_description` in `src/menhir/mcp/contracts.py`).

- **Must:** the description's first sentence says *when to use the tool*, in terms of the
  reader's goal. "Find recorded decisions, reasons, incidents, preferences and constraints" works.
  "Search memories by semantic similarity" describes the mechanism and does not help a model choose.
- **Should:** when a sibling tool is the better choice for a nearby job, say so in one clause
  ("for time-ordered history, use ...").
- **Must:** document every argument in the docstring, including every accepted value of a string
  enum and what an empty value means.
- **Must not:** open the docstring by restating the description; it is dropped as a repeat and
  adds nothing.

## Arguments

- **Must:** keep required arguments to what the tool cannot work without. Everything else has
  a safe default.
- **Must:** clamp limits server-side and say the maximum in the docstring (`list_todos` clamps to
  200 and says so).
- **Should:** make identifiers (`namespace`, `workspace`, `project`) optional, and resolve them from
  the caller's configuration or pin where one exists. Never ask the model to guess one.
- **Must:** when an omitted identifier widens or narrows the result, say so in the output (see
  "Output").

## Output

- **Must:** lead with the answer. Diagnostics, breakdowns and traces come after it, or only on
  request (`compact`, `trace`).
- **Should:** keep the default output small enough to use often. A recall with full source
  excerpts is a large block of context ([#434]).
- **Must:** say what was searched (effective scope), what was cut (truncation, limits) and what
  is not ready yet (pending enrichment, stale or incomplete index).
- **Must:** label every number by kind. A ranking score is not a similarity or a confidence;
  only fields documented as calibrated may be used as thresholds ([#433]).
- **Must:** keep field names stable. Renaming a field is a breaking change for every agent
  prompt that reads it.
- **Should:** when output is truncated, name the tool or argument that returns the rest
  (`list_todos` points to `get_todo`).

## Errors and refusals

- **Must:** refuse on purpose with `McpToolRefusal` (`src/menhir/mcp/telemetry/tracker.py`). The
  client then gets an MCP error result, so a refusal cannot be mistaken for data.
- **Must:** say what failed, whether anything was changed, and the next step. Infrastructure
  failures already get an actionable message from `_diagnose_failure`.
- **Must not:** turn "could not check" into an empty result.

## Safety

- **Must:** set `read_only_hint`, `destructive_hint` and `open_world_hint` truthfully. Clients
  use them to decide when to ask the user.
- **Must:** put destructive and system-control tools (delete, scheduler, lease, orphan recovery,
  client and namespace administration) at operator tier or above. Keep them out of the default
  set for chat clients ([#435]).
- **Should:** offer inspect-before-apply for repairs (as `repair_stale_enrichment` does) and a
  size gate for bulk deletes (as `delete_namespace` does).

## Lifecycle

- **Must:** when a tool is renamed or merged, keep the old name as a deprecated alias for at least
  one release, and check its use before removing it ([#432]).
- **Must:** a tool that ships disabled or behind a flag is listed in
  `.agent/default-off-features.md` with how to turn it on.

## Docs to update in the same PR

- `.agent/mcp-tools.yaml`: the tool's group.
- `.agent/tasks-mcp.md`: when an agent should pick it.
- `README.md` "Selected MCP tools": if it is part of the everyday set.
- `CHANGELOG.md`: if users would notice.

## Already checked at startup

`register_all_tools` refuses to start when a tool lacks any of these, so you will hear about
them without reading this list:

- a declared tenancy `scope` that matches the endpoint signature (`assert_tool_scopes_declared`);
- a non-empty `title` and `description`, boolean safety hints, and `oauth_scopes` that match
  `required_tier` (`validate_tool_metadata`).

[#430]: https://github.com/Archolith/menhir/issues/430
[#431]: https://github.com/Archolith/menhir/issues/431
[#432]: https://github.com/Archolith/menhir/issues/432
[#433]: https://github.com/Archolith/menhir/issues/433
[#434]: https://github.com/Archolith/menhir/issues/434
[#435]: https://github.com/Archolith/menhir/issues/435
[#436]: https://github.com/Archolith/menhir/issues/436
