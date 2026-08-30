# Governance artifacts

Menhir separates three kinds of governance that are easy to collapse into one claim:

1. **Knowledge governance** controls which evidence is admitted, current, historical,
   promoted, conflicted, or available to a client.
2. **Supply-chain governance** records source licensing, installed dependencies, model
   configuration, and test evidence.
3. **Release provenance** binds reviewed source and build inputs to the exact artifacts
   accepted by the deployment tooling.

An artifact in this repository is evidence about its stated scope. It is not proof of a
particular live deployment unless a release and its external receipts bind it.

## Repository artifacts

| Artifact | Location | Scope |
|---|---|---|
| License and notices | [`LICENSE`](../LICENSE), [`NOTICE`](../NOTICE), [`THIRD-PARTY-LICENSES.txt`](../THIRD-PARTY-LICENSES.txt) | Source and redistributed dependency notices |
| Software bill of materials | [`sbom.json`](../sbom.json) | Reproducible CycloneDX inventory of one clean installed environment |
| Coverage snapshot | `coverage.xml` (generated, not tracked) | Historical offline test execution, with the limitations below |
| Model configuration record | [`model-governance.md`](model-governance.md) | Code defaults, environment overrides, and model-selection policy |
| Runtime activation ledger | [`.agent/default-off-features.md`](../.agent/default-off-features.md) | Which implemented authority and retrieval paths are enabled by default |
| Production release contract | [`deploy/PRODUCTION.md`](../deploy/PRODUCTION.md) | Release manifests, receipts, backup/restore, promotion, rollback, and live acceptance gates |

## Knowledge governance

Menhir preserves source episodes and first-class evidence behind derived knowledge.
Candidates are withheld pending review; promoted memory requires operator authority;
superseded and historical knowledge remains available for audit while current recall omits
it by default. Conflict resolution is explicit. `get_provenance` expands a memory or
derived View into source episodes, evidence, and code anchors.

Namespaces, client policies, OAuth scopes, and reader/agent/operator tiers restrict which
tools and knowledge scopes a client can use. They operate within Menhir's documented
single-operator trust model and are not a general multi-tenant isolation claim.

## SBOM

The checked-in SBOM is CycloneDX 1.6 JSON with `reproducible=true` and 101 components,
including `archolith-menhir` 0.2.0. Representative versions in that generated environment
are:

| Component | Version |
|---|---:|
| `archolith-mcp-framework` | 0.2.0 |
| `archolith-oauth` | 0.2.0 |
| `fastapi` | 0.139.0 |
| `graphiti-core` | 0.29.2 |
| `joserfc` | 1.7.3 |
| `neo4j` | 6.2.0 |
| `openai` | 2.45.0 |
| `pydantic` | 2.13.4 |

The SBOM inventories the environment used to generate it. Source dependencies in
`pyproject.toml` may move to a different reviewed commit before the next SBOM refresh;
release authority must bind the source pins, wheel, and generated SBOM together.
Environment introspection also does not provide distribution-artifact hashes. A
hash-bearing inventory requires hash-pinned build inputs.

Regenerate from a clean environment:

```bash
uvx --from cyclonedx-bom cyclonedx-py environment <clean-venv>/Scripts/python.exe \
    --pyproject pyproject.toml --mc-type application --of JSON --sv 1.6 \
    --gather-license-texts --output-reproducible -o sbom.json
```

## Coverage snapshot

`coverage.xml` is a generated offline-suite snapshot, not a tracked or current quality
score. Online tests that require live services are skipped unless explicitly enabled, and
line or branch execution does not prove the behavior of real Neo4j paths. Regenerate from
the intended clean test environment and report its commit, command, date, pass/fail
totals, skipped tests, and branch setting beside any published coverage claim.

```bash
.venv/Scripts/python.exe -m pytest tests/ --cov=src/menhir \
    --cov-report=xml:coverage.xml --cov-report=term -o addopts=""
```

## Release provenance

The production authoring and validation tooling uses a strict `release.json` authority.
It binds the reviewed repository commits and remotes, container images, OAuth wheel,
manifests, SBOM, policies, installed artifact destinations, rollback anchors, and an
independent security-review attestation. Runtime, backup, restore, candidate, promotion,
and rollback receipts bind their own authority digests.

These controls describe the shipped release contract. A live deployment remains unproven
until its exact release and external acceptance evidence pass the gates in
[`deploy/PRODUCTION.md`](../deploy/PRODUCTION.md).
