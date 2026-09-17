# P3 parity over real repositories

Gate: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3) — "structural parity is
explained for every fixture".
Probe: `scripts/probe/p3_parity_report.py`
Run: 2026-09-17, four real repositories on the maintainer's machine. No production data, no tunnel,
no graph.

## What was run

Per repository: the real bundler builds a plan and a deterministic archive, `plan_archive`
validates it, `materialize` extracts it into a temporary root, and **the same `ProjectScanner`**
scans the repository and the extraction. Differences are then classified rather than counted.

The distinction is the point. The bundler is *supposed* to drop things — untracked files, excluded
directories, oversized files, refused paths — so a run where both fingerprints matched would mean
the selection policy had stopped working. Only two classes are findings:

- a path **in the manifest** that is missing after extraction (bundle, transport or extraction bug)
- a path present remotely and not locally (should be impossible)

## Results

| repository | stack | files local / remote | symbols local / remote | explained omissions | unexplained |
| --- | --- | --- | --- | --- | --- |
| archolith-bench | Python | 1575 / 1573 | 3422 / 3422 | 2 | **0** |
| menhir | Python | 1408 / 1380 | 18249 / 18204 | 28 | **0** |
| reasonix | Python | 1197 / 1197 | 4 / 4 | 0 | **0** |
| headroom | TypeScript | 1475 / 1475 | 14867 / 14867 | 0 | **0** |
| bulletproof-react | TypeScript | — | — | — | skipped, see below |

**Zero unexplained differences across every repository.** Two of the four reproduce their symbol
count exactly — 3,422 and 14,867 — which is the strongest available signal that the structural
content survives the round trip. Menhir's 45-symbol difference tracks its 28 intentionally omitted
files.

## bulletproof-react: skipped, and that is the system working

The selection policy refused three paths as secret-risk, so `plan.blocked` is set and nothing is
bundled. A forked repository carrying `.env`-shaped files cannot be synced without an explicit
per-run `--allow-path`, which is the designed behaviour and is worth seeing on a real repository
rather than only in a unit test.

## One probe bug, recorded because of how it looked

The first run reported **3,147 unexplained differences** against archolith-bench — every path
simultaneously "missing after extraction" and "present remotely but not locally". That reads as
total data loss.

It was the probe. The bundle nests files under `content/` and keeps the manifest beside them, so
the project root inside an extraction is one level down; the two sides were comparing `x` against
`content/x`. Nothing was wrong with the bundler, transport or extractor.

Worth recording for the reason it was confusing: a path-prefix mistake and catastrophic data loss
produce the same shape of report, and the number was large enough to be alarming rather than
suspicious.

## What this does and does not close

**Closes:** the gate's parity requirement over representative real projects, across two stacks and
four repositories, using the shipping bundler and the production scanner.

**Does not close:** these repositories are all healthy. Nothing here exercises a repository with
submodules in an unusual state, a detached HEAD, exotic encodings, or files near the per-file size
limit. The hostile corpus covers deliberate attacks; this covers ordinary code. Neither covers
strange-but-honest repositories.
