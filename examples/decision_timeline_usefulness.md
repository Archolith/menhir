# Decision timeline usefulness test

Can someone recover an approved rule and its source faster or more accurately from a short timeline than from the issue thread?

## Prompt for both readers

> What did Menhir #725 approve about pending work from an older extension version? State what happens by default, who can move it forward, and where the approval is recorded. Stop when you can cite the source.

## Set up the two views

- **Issue view.** Open the issue thread at the top. Use its body and comments. [Menhir #725](https://github.com/Archolith/menhir/issues/725)
- **Timeline view.** Run the command below from the Menhir worktree with its development Python environment and a fresh test Neo4j instance. Give the reader only the printed section headed **Menhir #725 decision timeline**. The test fixture clears its Neo4j database.

```powershell
python -m pytest --run-online -q -s -rs tests/test_decision_timeline_extension.py::test_decision_timeline_d5_lookup
```

Use `MENHIR_TEST_NEO4J_URI` if the disposable database uses a port other than the default `7688`. The command must report one pass. A skip means it did not produce or check the timeline.

Use two readers, or two fresh assistant sessions with the same model and tools. Give each the same prompt and one starting view. Allow both to follow source links. Start the timer when the reader receives the view and stop when they submit an answer. Run the timeline setup beforehand and record that effort separately. Record the date and the issue revision you used, since issue text can change.

| Starting view | Elapsed seconds | Correct default | Correct actor and actions | Approval citation |
| --- | ---: | --- | --- | --- |
| Issue thread | | | | |
| Decision timeline | | | | |

## Answer key for the evaluator

- Pending work from the older definition version stays pending for review when no explicit action is supplied.
- The extension may explicitly migrate or retire that work.
- The approval is **D5** in the Decisions section of Menhir #725. The open roadmap question and the later local implementation commit are different records. [D5 decision](https://github.com/Archolith/menhir/issues/725#decisions-owner-d1-and-d6-first)

Count the timeline as useful for this trial if it earns all three checks and either takes less time or avoids an error made in the issue view. Treat one comparison as a spot check. The fixture uses manually curated records, so the result measures lookup and citation rather than automatic extraction or the cost of maintaining the timeline.
