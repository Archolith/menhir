# Contributing to Menhir

Menhir has one maintainer. Issues and pull requests are welcome; expect replies within about
a week. For a security issue, do not open an issue: follow [SECURITY.md](SECURITY.md).

## Before you start

- **Bugs:** open an issue with the bug template. Include the Menhir version
  (`pip show archolith-menhir`), the OS and Python version, and the smallest steps that
  reproduce it.
- **Changes larger than a fix:** open an issue first, so we can agree on the approach before you
  write it.

## Set up

Python 3.12 or newer, and Docker for the test Neo4j.

```bash
git clone https://github.com/Archolith/menhir.git
cd menhir
python -m pip install -e . --group dev
```

[README.md](README.md), under "Step by step, and working from a checkout", covers `.env` and
running a local Neo4j.

## Tests

CI runs these jobs on every pull request (`.github/workflows/tests.yml`). Run the ones that
cover your change before you push.

| Job | Command |
|---|---|
| lint | `pip install ruff`, then `ruff check --select F811 .` and `ruff check --select F821,ASYNC src` |
| offline | `pytest` (online tests skip themselves without `--run-online`) |
| online | `docker compose -f docker-compose.test.yml up -d` (a throwaway Neo4j on port 7688, the tests' default), then `pytest -m "online and not needs_llm" --run-online` |
| stdio E2E | `MENHIR_E2E=1 pytest tests/e2e`; see [tests/e2e/README.md](tests/e2e/README.md) |

Tests never need a paid model. Lanes that need one use a local deterministic fake.

Add or change tests with the behaviour they cover. During a pure refactor, existing tests stay
as they are; they are the proof that behaviour did not change.

## Pull requests

- One topic per pull request, against `main`.
- Commit messages use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `refactor:`, `docs:`, `chore:`, `ci:`, `test:`.
- Add an entry to the top of `CHANGELOG.md` for a change users would notice.
- Never commit secrets. Configuration for LLM providers, embeddings, Neo4j and auth lives in
  environment variables.
- All required CI checks must pass before a merge.

[AGENTS.md](AGENTS.md) has the full repository rules, written for coding agents but binding on
everyone.

## Releases

Only the maintainer cuts releases, through `scripts/release.py`. See
[docs/runbooks/release.md](docs/runbooks/release.md).

## License

Menhir is Apache-2.0. By contributing, you agree that your contribution is licensed under the
same terms ([LICENSE](LICENSE)).
