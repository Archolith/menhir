# Guardrails for documentation

A wrong document costs more than a missing one: an agent follows it and fails, and a person stops
trusting the rest. These rules apply to `README.md`, `docs/`, and the agent docs under `.agent/`.

## Rules

1. **Every name resolves.** Each tool, argument, CLI command, setting, environment variable and
   port named in a document exists in the current code. Before merging, search the code for any
   name you added or kept. A check in CI is planned ([#442]).
2. **One home per fact.** Each fact has one owning document, and other documents link to it
   instead of restating it:

   | Fact | Owner |
   |---|---|
   | Install, configure and connect | `README.md` quick start, then `docs/post-install.md` |
   | What agents should do | `docs/agent-usage.md` and `docs/templates/AGENTS.menhir.md`, consistent with `src/menhir/mcp/instructions.py` |
   | Which tool for which job | `.agent/tasks-mcp.md` |
   | What is on or off by default | `.agent/default-off-features.md` |
   | Usability rules | this folder |

3. **State the default, not the history.** Say what happens on a fresh install today. History
   belongs in `CHANGELOG.md`, not in setup instructions.
4. **Describe what ships, and link what is planned.** A planned change is written as a link to its
   issue, never as current behaviour.
5. **Date reconciliations.** A document that summarises the code's state (feature ledgers,
   status pages) says which commit it was last checked against.
6. **Lead with the task.** Open a guide with what the reader is trying to do and the command that
   does it. Background goes after.
7. **Numbers go stale.** Avoid counts (tools, tests, files) in prose unless a check keeps them
   current, or say "about" and the date.
8. **Plain words.** Follow the vocabulary rule in the [charter](README.md#principles). When an
   internal term is unavoidable, define it where it first appears or link `.agent/glossary.md`.

## When you change code

Update the owning document in the same PR when you change any of these:

- a tool's name, arguments, output fields, tier or availability;
- a CLI command or flag;
- a default, a setting or an environment variable;
- what a new user has to do to install, connect or adopt Menhir.

[#442]: https://github.com/Archolith/menhir/issues/442
