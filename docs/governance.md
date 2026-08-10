# Menhir Governance Artifacts

The launch-required governance artifacts for Menhir and how to regenerate them. These close the
MVP governance gaps (LICENSE, SBOM, coverage artifact, model/version record).

| Artifact | Location | Purpose |
|---|---|---|
| **License** | `LICENSE` + `NOTICE` | Apache License 2.0. `NOTICE` carries the copyright attribution. |
| **SBOM** | `sbom.json` | CycloneDX 1.6 software bill of materials — supply-chain dependency inventory. |
| **Coverage artifact** | `coverage.xml` | Cobertura XML from the offline test suite (see caveat below). |
| **Model/version record** | `docs/model-governance.md` | Every LLM/embedding model, provider, and where it is configured (AI-G01). |

## License (Apache-2.0)

`LICENSE` is the Apache License 2.0. `NOTICE` holds the copyright line
(`Copyright 2026 Archolith contributors`). `pyproject.toml` declares
`license = "Apache-2.0"` (SPDX) so package metadata matches.

## SBOM (`sbom.json`)

CycloneDX 1.6 JSON, **93 dependency components with per-dependency license data** (92/93; only
`archolith-mcp-framework` lacks license metadata). The declared root component is
`archolith-menhir` 0.2.0 under Apache-2.0. Generated from a clean wheel install. Regenerate:

```bash
uvx --from cyclonedx-bom cyclonedx-py environment <clean-venv>/Scripts/python.exe \
    --pyproject pyproject.toml --mc-type application --of JSON --sv 1.6 \
    --gather-license-texts --output-reproducible -o sbom.json
```

Key deps at generation: fastapi 0.141.1, joserfc 1.7.4, graphiti-core 0.29.3,
cryptography 50.0.0, httpx 0.28.1, neo4j 6.2.0, pydantic 2.13.4,
openai 2.53.0, and numpy 2.5.2.

**Prerequisite fix (2026-07-10):** the environment scan originally failed on a **corrupted
`numpy-2.4.4.dist-info`** (only a `licenses/` subdir; no `METADATA`/`RECORD`, so
`importlib.metadata` returned `Name = None`). Repaired by removing the broken dist-info and
`pip install --force-reinstall --no-deps numpy==2.4.4` (the production server had to be stopped first
— it held a Windows lock on numpy's `.pyd` binaries). If the scan ever fails again on package
metadata, the fallback is a cleaned `pip freeze` → `cyclonedx_py requirements` (produces purls +
versions but **no license data**).

**Known gaps:** (1) **no artifact hashes** — an environment-introspection SBOM inventories *installed*
packages and does not carry distribution-artifact hashes; a hash-bearing SBOM would require a
hash-pinned lockfile (`pip-compile --generate-hashes`), tracked as a post-MVP hardening. (2)
`archolith-mcp-framework` 0.2.0 does not declare its MIT license in package metadata; the repository
license is recorded separately in `THIRD-PARTY-LICENSES.txt`.

## Coverage artifact (`coverage.xml`)

Cobertura XML over the **offline** test suite (`online`-marked tests that hit live services are
skipped by default; pass `--run-online` to include them). Regenerate:

```bash
.venv/Scripts/python.exe -m pytest tests/ --cov=src/menhir --cov-report=xml:coverage.xml --cov-report=term -o addopts=""
```

**Result at generation (2026-07-10, `main`):** **78.2% line coverage** (14,687 / 18,782 lines).
Suite at generation: 2,699 passed, 2 failed, 32 skipped (11m18s). **The 2 failures were fixed
same day** (see below) — the suite is now green.

**Root cause of the 2 fixed failures (test hermeticity, not a code defect):**
`test_oauth_authorize.py::test_post_empty_operator_key_403` and
`test_oauth_consent_session.py::test_no_operator_key_disables_one_click` set `operator_key=""` on a
settings double to mean "no admin key configured." But `api/oauth._get_setting` treats an empty
settings value as "not configured" and falls through to `os.getenv("MENHIR_OPERATOR_KEY")` — which
leaks in from the repo `.env` loaded into the process. So a real key resolved, the empty-key **403**
branch (`oauth_authorize.py:580`) was skipped, and the wrong-secret **401** branch was hit instead.
Production behavior is correct (empty config → env fallback is intentional); the tests just weren't
isolating the env var. Fixed by clearing `MENHIR_OPERATOR_KEY` in each file's `_isolate` autouse
fixture, so the settings value is authoritative and the intended 403 branch is exercised. These
tests are environment-dependent: they passed in a clean CI env and failed only on a dev machine with
`.env` present.

**Honest caveat (TQ-03 + dark-code audit):** the headline line-coverage number **overstates real
coverage**. `tests/conftest.py`'s `StubMemoryGraphAdapter` reimplements production contracts
(conflict validation, episode state machine), and `test_perception.py` / `test_recall_service.py`
are happy-path only. So executed-line coverage is high while behavioral coverage of the real Neo4j
paths is thinner. Treat `coverage.xml` as a floor/inventory artifact, not proof of behavioral
completeness. Strengthening this (real-adapter integration coverage) is a post-MVP quality item.

## Model / version record

See `docs/model-governance.md` (AI-G01) — models by role, provider selection, production `.env`
selection, and the governance stance (models are explicit config, never auto-upgraded). Dependency
versions live in `sbom.json`.
