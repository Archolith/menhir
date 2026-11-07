# Usability charter

Menhir should be usable by as many people and agents as possible, not only by the person who
built it. This folder sets the bar and the guardrails that keep new work at that bar.

Read this page first. Then use the guide that matches what you are changing:

| You are changing | Read |
|---|---|
| An MCP tool: a new tool, its arguments, output, errors or access | [tool-design.md](tool-design.md) |
| Text an agent reads: server instructions, tool descriptions, the AGENTS template, hook output | [agent-facing-text.md](agent-facing-text.md) |
| Install, setup, connection, defaults, anything a new user meets first | [first-run.md](first-run.md) |
| Any document under `docs/`, `.agent/` or the README | [documentation.md](documentation.md) |

## Who Menhir is for

Design for every one of these. A change that helps one and breaks another needs a stated reason.

| Who | What they see | What "usable" means for them |
|---|---|---|
| **Evaluator**: trying Menhir for the first time | README, `menhir setup` / `menhir up`, the first tool list | From install to a useful recall in one sitting, without hand-editing files or learning internal terms |
| **Daily developer**: one person, a coding agent, a local install | Their agent's behaviour, occasional CLI output | Memory helps without being managed: the agent recalls and stores at the right moments, and failures are visible |
| **Operator**: runs a hosted deployment for several clients | OAuth clients, policy, health, release tooling | Each client gets the right tools, the operator can tell what is deployed, and fixing something never needs guesswork |
| **Chat-app user**: ChatGPT or Claude web connected to Menhir | Only what the model says. They never read our docs | The tool list and its output are the whole product. The model must be able to pick the right tool and explain the result |
| **The agent**: the model calling the tools | Server instructions, tool names, descriptions, arguments and output | It can choose correctly from what it is given, read results without our docs, and never mistake "could not check" for "nothing there" |
| **Contributor**: changes Menhir | This folder, `AGENTS.md`, `CONTRIBUTING.md` | The rules are written down and, where possible, checked by code |

The agent is the largest audience by volume. Tool names, descriptions and output are user
interface, and they get the same care as anything a person reads.

## Principles

Each principle has the rule, the reason, and an example from Menhir.

1. **The default path is the small, safe path.** Defaults give the fewest tools, the most compact
   output and the least authority that still does the job. More is opt-in.
   *Why:* most users never change a default. *Example:* a keyless local install currently gives
   its agent the operator tier and every registered tool ([#439]).

2. **Nothing fails silently.** An empty result, a partial result and "could not check" must look
   different. Say which scope was searched, what was truncated and what is still pending.
   *Why:* a silent gap reads as a confident answer. *Example:* omitting `workspace` returns only
   general pins with no warning (docs call it the most common mistake), and `query_structure`
   marks a project stale when the server cannot see the client's disk ([#104]).

3. **One obvious way per job.** Do not add a tool that differs from an existing one by a single
   option. Add the option.
   *Why:* every near-duplicate is a choice the agent can get wrong. *Example:* `add_memory` and
   `add_memory_and_track`; `close_todo`, `resolve_todo` and `supersede_todo` ([#436]).

4. **Every feature is reachable where it ships.** If a feature writes something a human must act
   on, the action must exist in every supported deployment.
   *Why:* a queue nobody can drain is worse than no queue. *Example:* `add_candidate` stages
   claims that can only be approved in the explorer, which production does not serve ([#431]).

5. **Every error names the next step.** Say what failed, whether anything changed, and what to do.
   *Why:* the reader is often an agent that will otherwise retry or give up. *Example of the
   standard:* the timeout message in `mcp/telemetry/tracker.py` names the operation and the limit,
   gives the likely cause, and points at server health. It should also say that a write may or
   may not have been applied.

6. **Use words the reader already knows.** Internal names (RRF, View, Warden, scalar, frontier,
   EPISODIC_PENDING) stay internal, or are explained where they appear.
   *Why:* jargon turns a working feature into an unusable one. *Example:* recall output labels a
   ranking score `score` and `sim`, which readers take for similarity ([#433]).

7. **Measure use before judging a feature.** Every tool's use must be visible per tool and per
   client, including zero use.
   *Why:* without it, pruning and prioritising are guesses. *Example:* `get_memory_stats` shows
   18 named operations and hides the rest ([#432]).

8. **Docs match the code.** Every tool, command, setting and port named in a document exists.
   Every statement of what is on or off by default is current.
   *Why:* a wrong name costs an agent a failed call and a person their trust. *Example:* agent
   docs that recommend tools which do not exist or which no client can call ([#430], [#442]).

9. **Agent-facing text is evaluated like a feature.** Changing the default instructions,
   template or a core tool description is a behaviour change, and it needs evidence.
   *Why:* in a matched agent evaluation, agents given Menhir's earlier one-line instructions
   skipped recall in half the runs; with the current text every run recalled (commit 33fec61a).

## How the guardrails are enforced

A rule that is only written down drifts. Prefer a check in code; fall back to the PR checklist.

| Guardrail | Enforced by | Status |
|---|---|---|
| Every tool declares its tenancy scope, and the scope matches its signature | `assert_tool_scopes_declared` (`src/menhir/mcp/contracts.py`), at startup | Enforced |
| Every tool has a title, a description, boolean safety hints, and OAuth scopes that match its tier | `validate_tool_metadata` (same file), at startup | Enforced |
| User-facing changes are checked against this folder | PR template checklist | Enforced by review |
| Every registered tool belongs to a profile, or is marked internal | Profiles ([#435]) | Not yet |
| Tool and command names in docs resolve | Docs name check ([#442]) | Not yet |
| Per-tool, per-client usage is visible | `menhir tool-usage` ([#432], [#480]) | Done |
| The served instructions and tool metadata match the release | `scripts/release/check_served_metadata.py` ([#429]); the remaining gaps are [#498] | Partly |

When a "Not yet" row lands, update this table in the same pull request.

[#104]: https://github.com/Archolith/menhir/issues/104
[#429]: https://github.com/Archolith/menhir/issues/429
[#430]: https://github.com/Archolith/menhir/issues/430
[#431]: https://github.com/Archolith/menhir/issues/431
[#432]: https://github.com/Archolith/menhir/issues/432
[#433]: https://github.com/Archolith/menhir/issues/433
[#435]: https://github.com/Archolith/menhir/issues/435
[#436]: https://github.com/Archolith/menhir/issues/436
[#439]: https://github.com/Archolith/menhir/issues/439
[#442]: https://github.com/Archolith/menhir/issues/442
[#480]: https://github.com/Archolith/menhir/pull/480
[#498]: https://github.com/Archolith/menhir/issues/498
