# Graphiti differential check

Checks whether a Graphiti tree changes what Menhir does, before the Graphiti pin moves. It runs
Menhir's consumer-session acceptance test (`tests/test_consumer_session_e2e.py`: the production
queue and enrichment path, two tenants, a correction, a project scan and a reranked recall) with
production's non-secret settings, then compares:

- the OpenAI requests sent (replay misses: requests the recorded baseline never sent);
- the graph left at teardown (nodes and relationships, with run-varying ids and times masked);
- the session report the test prints.

```bash
export MENHIR_TEST_NEO4J_URI=bolt://127.0.0.1:<port> MENHIR_TEST_NEO4J_PASSWORD=<pw>  # disposable
bash scripts/graphiti_difftest/run_difftest.sh <candidate graphiti tree> <runs dir>
python scripts/graphiti_difftest/compare.py <runs dir> base-replay base-replay2      # control
python scripts/graphiti_difftest/compare.py <runs dir> base-replay candidate-replay
```

The baseline is the Graphiti installed in `.venv`. The candidate goes first on `PYTHONPATH`.
Only the recording run, and any replay miss, calls OpenAI. `DIFFTEST_CAP_USD` (default $0.25)
caps the total across runs. The API key comes from the `.env` of this checkout, or of
`DIFFTEST_MENHIR_DIR` when set (a worktree has no `.env`); nothing else in that file is used.

Read the control first. If the two baseline replays differ, the harness is not deterministic for
that change and the candidate comparison means nothing. Masked tokens are Menhir's
`MenhirCurrentSpeaker_<hex>` markers, wall-clock timestamps, the test's per-run 8-hex suffixes,
temp project paths and nonces. The cache and run outputs hold prompt text, so keep the runs dir
outside the repository.

First used for the Graphiti sync to upstream v0.32.1 (Archolith/graphiti#35): the same 14
chat and 18 embedding requests, zero misses, an identical graph (59 nodes, 49 relationships) and an
identical session report, for about $0.015.
