## 2026-11-04 - `/readyz` shows build details only to callers with a valid token

- The public `/readyz` body keeps its status fields but no longer reports the version, commit or
  metadata digest. A caller sending a bearer token the MCP endpoint would accept gets the `build`
  block. Both bodies are `Cache-Control: no-store`. A bad token gets the public body, never an
  error, and does not change the readiness answer.
- `scripts/release/check_served_metadata.py` reads the token from `MENHIR_READYZ_TOKEN`, sends it
  only over HTTPS or directly to loopback, and does not follow redirects while holding it. It now
  treats any status other than 200 or 503 as an error instead of reading the body.

## 2026-11-04 - Warning lines stop suggesting a silence; candidate text is marked untrusted

- Operator warning lines no longer tell the reader how to silence them. `acknowledge_warning` is
  unchanged and still listed for operator clients.
- `review_inbox` marks the candidates section as unreviewed, untrusted text.

## 2026-11-04 - One read-only inbox of what is waiting on a decision

New MCP tool `review_inbox` (readonly tier, #437). It lists unresolved conflicts, failed
enrichments, candidates, stale TODOs and open artifact questions in one call, with the tool that
acts on each. Plans stuck in a lifecycle state and a start-session inbox line are not included yet.

- `src/menhir/mcp/tools/ops/review_inbox.py`: the tool. One failed or hung read shows as
  `unavailable (<ExceptionType>)` and the other sections still render.
- `list_candidates` gains an optional `namespace` at every backend layer. The internal backend
  dispatch now forces a pinned client's silo onto it, as it already did for the other four reads.
  Each candidate row now also reports its `namespace`. An unscoped call is unchanged.
- Agent-tier clients do not get the tool. A production client policy must decide `review_inbox`
  for every client, or startup refuses the policy.

## 2026-11-02 - Usability charter and guardrails

Documentation only. Sets who Menhir must work for and the rules that keep new work usable.

- `docs/usability/README.md`: audiences, nine principles, and which guardrails are enforced in code.
- `docs/usability/tool-design.md`: rules for MCP tool names, descriptions, arguments, output, errors,
  safety and lifecycle.
- `docs/usability/agent-facing-text.md`: rules for server instructions, tool descriptions, the AGENTS
  template and hook output, including when a change needs an agent evaluation.
- `docs/usability/first-run.md`: standards for the first-run journeys and the gaps against them
  (#429-#441).
- `docs/usability/documentation.md`: rules for docs, and which document owns each fact.
- `AGENTS.md`, `CONTRIBUTING.md`, `.github/pull_request_template.md`: point contributors and agents at
  the guides; the PR template gains a usability checklist.

## 2026-11-02 - One command prepares a version or fork bump

`scripts/bump.py` replaces the by-hand edits before a release. No runtime code changes.

- `scripts/bump.py`: `version` and `fork` subcommands update every file that states the version or
  pin, or write nothing if an expected line has moved. `uv lock --check` must pass, and `--pr` opens
  the pull request. Tagging stays in `scripts/release.py`.
- `tests/test_bump.py`: covers partial writes, restores, and CRLF files. It also fails if a new file
  names the current version or fork pin and the bump would miss it.
- `docs/runbooks/release.md`: step 2 and the fork pin procedure.

## 2026-11-02 - One tag publishes the whole release

A `v*` tag now publishes PyPI, the release image, the image's version tag and the GitHub release
in one run. Nothing is typed in by hand, and a stopped release can be finished by a re-run.

- `.github/workflows/publish-pypi.yml`: calls `release-image.yml`, verifies the attestation with
  the identity `menhirctl.ps1 upgrade` uses, tags the image `X.Y.Z`, then creates the GitHub
  release. The test gate is unchanged.
- `deploy/release_pipeline.py`, `deploy/release-python-base.txt`: derive the release inputs, skip
  finished stages, and refuse to move an image tag or upload over a different PyPI file set.
- `scripts/release.py`: wait up to three hours for the longer run.
- `docs/runbooks/release.md`: the new flow and how to finish a stopped release.

## 2026-11-02 - v0.3.2: retry replies cut off at the output limit

- `pyproject.toml`, `uv.lock`, `sbom.json`, `README.md`, `docs/governance.md`,
  `docs/model-governance.md`, `docs/post-install.md`: version 0.3.2. No dependency changes in this
  commit.
- Includes the truncation retry and fork 0.31.2.post2 (#424), the parallel `MENTIONS` retention fix
  (#422) and the release command error report (#420).

## 2026-11-02 - Retry LLM replies cut off at the output limit; Graphiti fork 0.31.2.post2

A reply cut off at the output token limit could be parsed into a nested fragment, so the retry with
a higher limit never ran, or the fragment was accepted. The fork now raises `OutputTruncatedError`
when the provider reports `finish_reason=length`, and Menhir treats that as truncation.

- `infrastructure/graphiti_llm_adapter.py`: `_looks_like_truncation` returns true for
  `OutputTruncatedError` at any reply length. The retry limits are unchanged: 4096, 8192, 16384,
  then 16384 with a request for a smaller extraction.
- `tests/test_graphiti_truncation_retry.py`: real-client tests for the escalation.
- `pyproject.toml`, `uv.lock`: `archolith-graphiti-core==0.31.2.post2`; pins, docs and release
  checks updated.

A prose-wrapped reply that is cut off, when the provider does not report `length`, can still parse
to a fragment.

## 2026-11-01 - v0.3.1: scalar values checked against their source (#411)

Hotfix on 0.3.0. A wrong value in five scalar kinds could become a current View if 2 of 3 model
samples agreed on it, because nothing compared it with what the user said.

- `services/typed_scalar_rules.py`: `parse_scalar_row` now drops a duration, frequency, weekday,
  status or boolean value the source does not state, or that the source negates, hedges or asks
  about. The clause is read from the source, so a short quote cannot leave a negation out. Status
  and true booleans are also dropped in the past tense or in a "who" clause. `expire` rows are
  exempt.
- `uv.lock`: `joserfc` 1.7.3 -> 1.7.7 (GHSA-jpxq-7h8p-2q76, GHSA-9pg5-8c7x-5hfx).
- `tests/test_scalar_source_grounding_411.py` (new). `tests/test_boolean_polarity_consistency.py`:
  three cases asserted `has_passport` from unrelated sentences and now name their own attribute.
- `docs/personal-memory-readiness.md`, `docs/post-install.md`, `README.md`: what the checks do and
  do not cover.
- `pyproject.toml`, `uv.lock`, `sbom.json`, `docs/governance.md`: version 0.3.1.

Values stored before 0.3.1 are not rechecked. The benchmark was not rerun for this hotfix, so the
Gate E results in #123 describe 0.3.0.

## 2026-11-01 - v0.3.0: release version for candidate 44933985 (#123)

The frozen MVP release candidate `44933985` (#123) gets its own version: 0.2.3 is already on PyPI.

- `pyproject.toml`, `uv.lock`: version 0.2.3 -> 0.3.0. No dependency changes.
- No source changes since `44933985`. Outside `src/`: offline tests also run on Windows/py3.12 and Ubuntu/py3.13, test fixes for those platforms, and community files (CONTRIBUTING, Code of Conduct, issue and PR templates). `docs/personal-memory-readiness.md` gained the scalar grounding known limitation (#412; post-MVP follow-up #411).
- `CHANGELOG-archive.md`: the oldest former current entry moved there (10-entry limit).
- Docs accuracy pass for the release: known limitations (#169, #417) and the Beacon deferral (#120) are stated; defaults, ports, retired settings, `menhir sync` upload behavior and runbook commands are corrected; `docs/roadmap/menhir-mvp-roadmap.md` is now a pointer to the release plan.
