# Guardrails for agent-facing text

An agent learns how to use Menhir only from text we give it. That text is the product's interface
for the largest group of users, and it gets reviewed and tested like code.

## What counts

| Text | Where it lives | Who reads it |
|---|---|---|
| Server instructions | `SERVER_INSTRUCTIONS` in `src/menhir/mcp/instructions.py`, served by the stdio and remote servers | Every MCP client, at connect |
| Tool titles and descriptions | Each tool class and its endpoint docstring | The model choosing a tool |
| Tool output | Each tool's formatter | The model, and through it the user |
| Agent instructions template | `docs/templates/AGENTS.menhir.md` | Agents in repositories that paste it |
| Agent operating contract | `docs/agent-usage.md` | People writing agent instructions |
| Hook-injected context | `src/menhir/cli/hook.py` and `menhir.cli.bootstrap` | Claude-compatible hosts with Menhir hooks installed |

The first, fourth and fifth must agree with each other. The `instructions.py` docstring already
requires this. Change them in the same PR.

## Rules

1. **Purpose before mechanics.** Open with when memory is worth consulting and what it holds.
   Identifiers, argument names and edge cases come after.
2. **Every rule is something the agent can do.** "Memory contains claims, not truth" works when
   it is followed by what to do about it (check provenance, dates, supersession).
3. **Every name exists.** Each tool, argument and value named must be registered and reachable
   for the clients that receive the text. Text that names a tool a client cannot call teaches
   that client to fail.
4. **No identifier the agent has to guess.** Give the value (the template's placeholders, filled
   in by the user or by tooling ([#416])), or let the server resolve it. Never "use the right
   namespace".
5. **Cover the empty case.** A new project's memory is empty. The text must say what to do then,
   not only that empty is not proof of absence ([#441]).
6. **Spend words carefully.** `SERVER_INSTRUCTIONS` is about 290 words and every client pays for
   it in every session. A change that adds a rule should remove or shorten another, or explain
   in the PR why it cannot.
7. **One rule per sentence, imperative mood.** Explain why only where the reason changes what the
   agent does.
8. **Plain words.** Follow the vocabulary rule in the [charter](README.md#principles).

## Evidence for a change

- **Must:** a change to `SERVER_INSTRUCTIONS`, the template, or the description of a tool in the
  everyday set comes with a before/after agent evaluation on the same tasks and model. 33fec61a
  is the precedent: the old one-line instructions had agents skip recall in half the runs; the
  rewrite had every run recall.
- **Should:** wording-only fixes (a wrong name, a typo) can skip the evaluation. Say so in the PR.
- **Must:** say in the PR and CHANGELOG that connected clients see the change only after they
  reconnect. Clients read instructions and the tool list at connect; Claude Code's `/clear` does
  not reconnect ([#429]).

## Output is text too

The tool-output rules in [tool-design.md](tool-design.md#output) apply. The two that matter most
for agents:

- lead with the answer, and keep diagnostics behind `compact` or `trace`;
- never label a ranking score in a way that invites a threshold ([#433]).

[#416]: https://github.com/Archolith/menhir/issues/416
[#429]: https://github.com/Archolith/menhir/issues/429
[#433]: https://github.com/Archolith/menhir/issues/433
[#441]: https://github.com/Archolith/menhir/issues/441
