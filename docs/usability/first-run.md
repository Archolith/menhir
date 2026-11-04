# First-run and adoption standards

Most people decide whether Menhir is worth it in their first hour. This page lists the journeys a
new user takes, the standard each one should meet, and where it falls short today. Keep the
"Today" column honest: update it when a gap closes, in the same PR.

## Standards for every journey

1. **No hand-edited files except secrets.** Writing an API key or a Neo4j password into `.env` is
   fine. Writing MCP client JSON, hook settings or agent instructions by hand is not.
2. **Every step ends with a check.** The user can see that the step worked before taking the next
   one. `menhir setup --check`, `menhir up --check`, `menhir diagnostics` and `menhir check` already
   do this for configuration.
3. **The first tool list is small.** A new agent sees the everyday tools, not the operator
   toolbox ([#435], [#439]).
4. **The first session is useful even with empty memory.** The agent knows to record the
   project's conventions and decisions instead of concluding memory is useless ([#441]).
5. **Nothing has to be invented.** The user is never asked to make up a namespace, workspace key
   or project key and copy it into several places ([#416]).
6. **The next step is always printed.** Each command ends by saying what to run next, as
   `menhir setup` and `menhir up` do now.

## Journey 1: try Menhir locally

| Step | Today | Gap |
|---|---|---|
| Install | `pip install archolith-menhir` | |
| Configure | `menhir setup` creates `.env` and, with `--provider`, a consistent provider block; `menhir up` starts Neo4j, prints a tier report and serves | |
| Connect an agent | Hand-written MCP client JSON from the README (`docs/post-install.md` section 3: setup "does not rewrite MCP client config") | `menhir connect <client>` ([#440]) |
| First tool list | With no keys, a loopback client is bound to the operator tier (`src/menhir/api/auth.py`) and sees every registered tool | Default to the everyday set ([#439]) |
| First memory | `add_memory` queues enrichment; recall finds the memory after enrichment finishes | Agent guidance for the empty, first session ([#441]) |
| First code question | `ingest_project`, then `query_structure` | |

**Target:** install to a useful recall in one sitting, with the only manual edit being secrets.

## Journey 2: adopt Menhir in a repository

| Step | Today | Gap |
|---|---|---|
| Choose identifiers | The user chooses a namespace, a workspace key and a project key | Generate and record them ([#416]) |
| Give the agent instructions | Copy `docs/templates/AGENTS.menhir.md` and replace its placeholders | Render it with the values filled in ([#416]) |
| Index the code | `ingest_project(path=..., name=...)` when code and server share a machine | |
| Install hooks | `menhir setup --install-claude-hooks --workspace <key>`, Claude-compatible hosts only | Hooks as part of install ([#112]) |

**Target:** one command in the repository leaves it with filled-in agent instructions, an
indexed project and, where the client supports them, hooks.

## Journey 3: connect to a hosted deployment

| Step | Today | Gap |
|---|---|---|
| Authenticate | Each client uses its own OAuth identity and the tier its policy grants | |
| Know what is running | `/readyz` reports the version, commit and a digest of the served instructions and tools; `scripts/release/check_served_metadata.py` compares them with a local checkout | Commit in the MCP `initialize` server info; a release step that runs the check ([#429]) |
| Pick up a new release | Clients see new instructions and tools only after reconnecting | Say so in release notes and client docs ([#429]) |
| Index code the server cannot see | `menhir sync` uploads a sanitized snapshot only when the server has receive mode on, and an accepted upload is staged, not yet written to the graph | Finish remote sync; stop reporting reachable projects as stale ([#104]) |

**Target:** a client connects, gets the tools its role needs, and can index and refresh its code
without shell access to the server.

## Journey 4: use Menhir from a chat app

A ChatGPT or Claude web user never reads these docs. The model's tool list and the tool output are
the whole experience.

| Concern | Today | Gap |
|---|---|---|
| Tool choice | Operator clients get almost every tool, including scheduler and lease controls | Profiles ([#435]) |
| Output size | Recall returns full diagnostics and source excerpts unless `compact` is set | Compact by default ([#434]) |
| Reading results | Ranking scores are labelled like similarities | Clear score labels ([#433]) |
| Human review | Staged candidates cannot be approved where production runs | Candidate review over MCP ([#431]) |

**Target:** the model picks the right tool from a short list and can explain the result in plain
words.

## Measuring it

Each target above should become a check: a first-run E2E test that counts manual steps and the
size of the first tool list, and the usage report ([#432]) for which tools new clients actually
call in their first sessions.

[#104]: https://github.com/Archolith/menhir/issues/104
[#112]: https://github.com/Archolith/menhir/issues/112
[#416]: https://github.com/Archolith/menhir/issues/416
[#429]: https://github.com/Archolith/menhir/issues/429
[#431]: https://github.com/Archolith/menhir/issues/431
[#432]: https://github.com/Archolith/menhir/issues/432
[#433]: https://github.com/Archolith/menhir/issues/433
[#434]: https://github.com/Archolith/menhir/issues/434
[#435]: https://github.com/Archolith/menhir/issues/435
[#439]: https://github.com/Archolith/menhir/issues/439
[#440]: https://github.com/Archolith/menhir/issues/440
[#441]: https://github.com/Archolith/menhir/issues/441
