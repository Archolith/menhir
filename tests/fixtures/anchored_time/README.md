# Anchored-time resolver fixtures

Frozen inputs and expected outputs for the anchored-time resolver port (P1 plan:
`.agent/plans/menhir-anchored-time-resolver-p1-plan.md` in the workspace). All files come from
the P0 prototype, never from the Menhir port, so the tests check the port against it.

| File | sha256 (12) | Origin |
|---|---|---|
| `dev_set.json` | ff402a6bbb9c | P0 dev set 1 (hand-written turns), copied byte-for-byte |
| `dev_set2.json` | 82702a2f5b5e | P0 dev set 2, copied byte-for-byte |
| `dev_set3.json` | 16b801b12753 | P0 dev set 3 (clause-guard cases), copied byte-for-byte |
| `ckpt_dev_fix2.jsonl` | 39b066baeba0 | Frozen gpt-6-luna outputs for dev set 1 |
| `ckpt_dev2_it2.jsonl` | eeae84168c09 | Frozen gpt-6-luna outputs for dev set 2 |
| `ckpt_dev3_it1.jsonl` | 1799e4c236c0 | Frozen gpt-6-luna outputs for dev set 3 |
| `prompt_golden.json` | 121090ec637f | Generated: prototype `build_messages` per dev set 1 turn |
| `prototype_expected.json` | 45e4503ae27f | Generated: prototype parse, guard drops, windows, bases per dev turn |

Both generated files record the prototype sources they came from (`reich_lib.py` a7797891eaab,
`clause_guard.py` ec4472e916a8). The generator is the P0 scratch script `gen_fixtures.py`
(`gen_fixtures.py <this dir>`); it imports only the prototype modules.

## Prompt version

`PROMPT_VERSION` is `a7797891`. The three checkpoints were produced with that prompt: counted
with tiktoken o200k, their recorded prompt tokens sit at a constant -1 offset from the current
prompt on every turn. The older `fix1` checkpoint (-36) used an earlier prompt and is not included.

## Held-out sets (not committed)

The held-out turns and gold come from LongMemEval conversations; their licence for
redistribution is still open (plan open question 4), so they stay out of the repo. To run the
held-out parity and pinned O2 tests in `tests/test_anchored_time_replay.py`, set
`MENHIR_ANCHORED_TIME_EVAL_DIR` to a directory holding `heldout*_turns.json`,
`heldout*_gold.json`, the four frozen `ckpt_heldout*.jsonl` runs and
`prototype_expected_heldout.json` (written by the same generator). Without it those tests skip.
