"""Session fixtures for the black-box E2E campaign.

OPT-IN IS AN ENVIRONMENT VARIABLE, NOT A ``--run-e2e`` FLAG
-----------------------------------------------------------
The repo's convention for expensive lanes is a CLI flag (``--run-online``,
``--run-remote-sim``). This package cannot follow it as-is: pytest only calls
``pytest_addoption`` in the *initial* conftest, so a ``--run-e2e`` flag has to be
declared in ``tests/conftest.py``. Adding it there is a two-line change and the right
end state -- it is left undone here so this scaffold touches no existing file. Until
then the gate is::

    MENHIR_E2E=1 pytest tests/e2e

Everything is skipped otherwise, including collection-time work, because bringing the
stack up costs a wheel build and a venv install.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e._harness.config import E2EConfig, docker_available
from tests.e2e._harness.evidence import LaneEvidence, new_run_id, repo_commit, tree_is_clean
from tests.e2e._harness.features import (
    FeatureCombo,
    combo_slug,
    combos_from_environment,
    validate_registry,
)
from tests.e2e._harness.artifact_corpus import build_artifact_corpus
from tests.e2e._harness.fixture_repo import build_fixture_repo
from tests.e2e._harness.providers import (
    deterministic_provider,
    failing_provider,
    local_provider_environment,
    real_provider_environment,
)
from tests.e2e._harness.stack import REPO_ROOT, build_wheel, install_into_venv, reset_graph, start_backend

_ENABLE_VAR = "MENHIR_E2E"
_STRICT_VAR = "MENHIR_E2E_STRICT"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: black-box stdio end-to-end lane (Phase C/F). Requires MENHIR_E2E=1, a "
        "disposable Neo4j, Docker, and a wheel build; skipped otherwise.",
    )
    config.addinivalue_line(
        "markers",
        "provider(kind): model provider for the lane -- none (default), deterministic, "
        "failing, or real. Anything but none starts a fake OpenAI-compatible server "
        "and points the child processes at it.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv(_ENABLE_VAR, "").strip() in {"1", "true", "yes"}:
        return
    skip = pytest.mark.skip(
        reason=f"E2E campaign disabled; set {_ENABLE_VAR}=1 to run (builds a wheel and a venv)"
    )
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize every lane that requests ``feature_combo`` over the selected matrix.

    With no matrix configuration this yields exactly one combo (``mvp_default``), so the
    ordinary run is one pass per lane over the shipping defaults. A powerset sweep turns
    the same lane into 2**n runs without the lane knowing anything about it.
    """

    if "feature_combo" not in metafunc.fixturenames:
        return
    combos = combos_from_environment()
    metafunc.parametrize(
        "feature_combo",
        combos,
        ids=[combo_slug(combo) for combo in combos],
        scope="session",
    )


@pytest.fixture(scope="session")
def e2e_run_id() -> str:
    return new_run_id()


@pytest.fixture(scope="session")
def e2e_config(tmp_path_factory: pytest.TempPathFactory) -> E2EConfig:
    """Resolve and validate campaign configuration. Fails closed on an unsafe graph."""

    # Fail at session setup, not mid-lane, if the feature registry has drifted from the
    # settings model: a stale registry silently toggles nothing.
    validate_registry(REPO_ROOT / "src" / "menhir" / "config" / "settings_model.py")

    work_root = Path(os.getenv("MENHIR_E2E_WORK_ROOT") or tmp_path_factory.mktemp("menhir-e2e"))
    config = E2EConfig(work_root=work_root)
    config.validate()
    for directory in (config.state_dir, config.evidence_dir, config.fixtures_dir):
        directory.mkdir(parents=True, exist_ok=True)
    config.write_env_file()
    return config


@pytest.fixture(scope="session")
def e2e_installed(e2e_config: E2EConfig):
    """Build a wheel and install it into a clean venv, once per session.

    In strict mode (``MENHIR_E2E_STRICT=1``, which the RC run must use) a dirty working
    tree aborts the campaign: Gate D requires the tree to be clean on the candidate, and
    a wheel built from uncommitted changes makes the recorded commit a false label for
    what was actually tested.
    """

    clean, dirty_paths = tree_is_clean(REPO_ROOT)
    if not clean and os.getenv(_STRICT_VAR, "").strip() in {"1", "true", "yes"}:
        pytest.fail(
            "MENHIR_E2E_STRICT is set and the working tree is dirty. The RC campaign "
            "must build from a clean tree so the recorded commit describes the wheel.\n"
            + "\n".join(f"  {path}" for path in dirty_paths[:40])
        )

    try:
        wheel = build_wheel(e2e_config.work_root / "dist")
    except Exception as exc:  # noqa: BLE001 - surfaced as a skip with the real reason
        pytest.skip(f"wheel build failed (is `build` installed?): {exc}")

    installed = install_into_venv(e2e_config, wheel)
    yield installed


@pytest.fixture(scope="session")
def e2e_fixture_repo(e2e_config: E2EConfig):
    """The generated fixture project used by E2E-3, E2E-6 and E2E-8."""

    return build_fixture_repo(e2e_config.fixtures_dir / "shop")


@pytest.fixture(scope="session")
def e2e_artifact_corpus(e2e_config: E2EConfig):
    """The generated WorkArtifact corpus used by E2E-4.

    Session-scoped, and E2E-4 mutates it: the stable-UUID criterion moves a document with
    ``git mv``. That is safe only because E2E-4 is the single consumer. A second lane
    taking this fixture must not assume the corpus is still in its built state -- rebuild
    into a distinct directory instead.
    """

    return build_artifact_corpus(e2e_config.fixtures_dir / "artifact-corpus")


@pytest.fixture
def lane_evidence(request: pytest.FixtureRequest, e2e_config: E2EConfig, e2e_run_id: str) -> LaneEvidence:
    """One evidence directory per test, named for the lane and its feature combination.

    ``request.node.name`` already carries the parametrized combo id, so a powerset sweep
    writes one directory per combination instead of overwriting a single one -- which is
    the difference between "the lane passed" and "the lane passed under these flags".
    """

    lane = request.node.name
    evidence = LaneEvidence(lane=lane, directory=e2e_config.evidence_dir / e2e_run_id / lane)
    evidence.record_stack(
        repo_commit=repo_commit(REPO_ROOT),
        tree_clean=tree_is_clean(REPO_ROOT)[0],
        neo4j_uri=e2e_config.neo4j_uri,
        backend_url=e2e_config.backend_url,
    )
    combo = request.getfixturevalue("feature_combo") if "feature_combo" in request.fixturenames else None
    if combo is not None:
        evidence.manifest["features"] = combo.as_evidence()
    yield evidence
    if not (evidence.directory / "result.json").exists():
        # A lane that raised before closing still leaves evidence; an empty directory
        # would be indistinguishable from a lane that never ran.
        evidence.close(status="INCOMPLETE")
        return

    # A PASS must have recorded every criterion the lane declares. E2E-7 once went green
    # in CI with 3 of its 5 criteria recorded, because a patch had cut the two headline
    # assertions out of the lane body -- and nothing noticed until a human compared the
    # evidence count to the declaration. Lanes with several tests per module keep their
    # criteria in per-test lists rather than a module CRITERIA, and are exempt here.
    declared = getattr(request.module, "CRITERIA", None)
    if declared and evidence.manifest.get("status") == "PASS":
        missing = sorted(set(declared) - set(evidence.criteria))
        if missing:
            pytest.fail(
                f"{lane} closed PASS but never recorded {len(missing)} declared "
                f"criteria: {missing}. A pass that skips a criterion is not a pass."
            )


@pytest.fixture
def fresh_graph(e2e_config: E2EConfig) -> None:
    """Reset the disposable graph before a lane runs."""

    if not docker_available():
        pytest.skip("docker not available; cannot guarantee a disposable graph")
    try:
        reset_graph(e2e_config)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"disposable Neo4j at {e2e_config.neo4j_uri} unreachable: {exc}")


@pytest.fixture
def provider_kind(request: pytest.FixtureRequest) -> str:
    """The provider a lane declared, as a string rather than a boolean.

    ``running_stack`` needs the KIND so a lane with no provider is not held to
    enrichment_ready (it never can be), while every lane WITH one is -- including
    `failing`, whose provider passes preflight and fails at the call.
    """

    marker = request.node.get_closest_marker("provider")
    return (marker.args[0] if marker and marker.args else "none").strip().lower()


@pytest.fixture
def provider_env(request: pytest.FixtureRequest) -> Iterator[dict[str, str]]:
    """Provider environment for the lane, selected by ``@pytest.mark.provider(...)``.

    ``"deterministic"`` is what makes enrichment lanes runnable at all. Reaching
    READY needs a model; a real one costs money, needs a key, and makes the lane
    irreproducible. The fake answers every Graphiti extraction schema, so E2E-2 can
    assert on real enrichment output rather than only the states reachable without a
    provider.

    ``"failing"`` refuses every completion, which is the only way to prove E2E-8's
    "provider failure does not silently pass" -- without it, an outage and a pass are
    indistinguishable.

    The default is no provider at all, and deliberately so: a lane that does not
    declare one gets no credentials and no endpoint, so an unintended live call fails
    loudly instead of quietly spending budget.
    """

    marker = request.node.get_closest_marker("provider")
    kind = (marker.args[0] if marker and marker.args else "none").strip().lower()

    if kind == "none":
        yield {}
    elif kind == "deterministic":
        with deterministic_provider() as base_url:
            yield local_provider_environment(base_url)
    elif kind == "failing":
        with failing_provider() as base_url:
            yield local_provider_environment(base_url)
    elif kind == "real":
        yield real_provider_environment(REPO_ROOT)
    else:
        raise ValueError(
            f"unknown provider kind {kind!r}; use none, deterministic, failing or real"
        )


@pytest.fixture
def feature_env(feature_combo: FeatureCombo) -> dict[str, str]:
    """The child-process environment for this lane's feature combination.

    Both the backend and the stdio bridge take this same dict, from this one fixture, so
    the two processes cannot end up running different configurations.
    """

    return feature_combo.env()


@pytest.fixture
def running_stack(
    e2e_config: E2EConfig,
    e2e_installed,
    fresh_graph,
    lane_evidence: LaneEvidence,
    feature_env: dict[str, str],
    provider_env: dict[str, str],
    provider_kind: str,
):
    """A started backend, torn down after the lane, with its log captured as evidence.

    The backend is started per lane rather than per session because feature flags are
    read at startup: a session-scoped backend could not honour a per-combo matrix, and
    would report results for whichever combination happened to start first.
    """

    # A lane that asked for a provider must get a backend that can actually enrich.
    # Without this the backend comes up `degraded / degraded_queue_only` whenever the
    # provider failed to wire up, the lane runs anyway, and "the episode never reached
    # READY" lands in the evidence as a product defect instead of a harness fault.
    backend = start_backend(
        e2e_config,
        e2e_installed,
        log_path=lane_evidence.backend_log_path,
        feature_env={**feature_env, **provider_env},
        # Every provider, `failing` included: it passes preflight and refuses at the
        # call, so the backend comes up READY and the worker genuinely attempts the
        # episode. A degraded backend under `failing` would mean the fake broke
        # preflight again -- and the lane would pass without the pipeline ever trying.
        require_enrichment=provider_kind != "none",
    )
    lane_evidence.record_stack(provider=provider_kind)
    lane_evidence.record_stack(backend_ready=backend.ready_payload)
    lane_evidence.record_stack(**e2e_installed.as_evidence())
    try:
        yield backend
    finally:
        backend.terminate()
