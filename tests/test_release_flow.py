from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "release_flow.py"
SPEC = importlib.util.spec_from_file_location("release_flow", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _commit(repo: Path, name: str, content: str) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="ascii")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", name], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path) -> tuple[str, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"], check=True
    )
    base = _commit(path, "base.txt", f"base {path.name}\n")
    head = _commit(path, "change.txt", f"change {path.name}\n")
    return base, head


def _coverage_fixture(tmp_path: Path) -> tuple[dict, dict[str, tuple[str, str]]]:
    repositories: dict[str, str] = {}
    commits: dict[str, tuple[str, str]] = {}
    prior_repos: dict[str, str] = {}
    for name in sorted(MODULE.REPOSITORIES):
        repo = tmp_path / name
        base, head = _repo(repo)
        repositories[name] = str(repo.resolve())
        commits[name] = (base, head)
        prior_repos[name] = base if name == "menhir" else head
    prior = tmp_path / "prior-release.json"
    prior.write_text(json.dumps({"repos": prior_repos}), encoding="utf-8")
    spec = {"repositories": repositories, "prior_release": str(prior.resolve())}
    return spec, commits


def test_fragment_coverage_accepts_claim_inside_changed_range(tmp_path: Path) -> None:
    spec, commits = _coverage_fixture(tmp_path)
    fragment = {
        "repositories": {"menhir": [commits["menhir"][1]]},
        "deployment_class": "security-config",
    }

    MODULE._verify_fragment_coverage([fragment], spec)


def test_fragment_coverage_accepts_validated_immutable_mapping(tmp_path: Path) -> None:
    spec, commits = _coverage_fixture(tmp_path)
    fragment = SimpleNamespace(
        repositories=MappingProxyType({
            "menhir": (commits["menhir"][1],),
        })
    )

    MODULE._verify_fragment_coverage([fragment], spec)


def test_fragment_coverage_requires_every_changed_repository(tmp_path: Path) -> None:
    spec, _ = _coverage_fixture(tmp_path)

    with pytest.raises(MODULE.ReleaseFlowError, match="no release-note fragment"):
        MODULE._verify_fragment_coverage([], spec)


def test_fragment_coverage_rejects_claim_for_unchanged_repository(tmp_path: Path) -> None:
    spec, commits = _coverage_fixture(tmp_path)
    fragment = {
        "repositories": {
            "menhir": [commits["menhir"][1]],
            "yawn_vps": [commits["yawn_vps"][1]],
        }
    }

    with pytest.raises(MODULE.ReleaseFlowError, match="unchanged repository"):
        MODULE._verify_fragment_coverage([fragment], spec)


def test_fragment_coverage_rejects_commit_outside_candidate_range(tmp_path: Path) -> None:
    spec, commits = _coverage_fixture(tmp_path)
    other = tmp_path / "other"
    _, unrelated = _repo(other)
    fragment = {"repositories": {"menhir": [unrelated]}}

    with pytest.raises(MODULE.ReleaseFlowError, match="outside"):
        MODULE._verify_fragment_coverage([fragment], spec)


def test_deployment_class_never_deescalates_non_app_source(tmp_path: Path) -> None:
    spec, _ = _coverage_fixture(tmp_path)
    fragment = {"deployment_class": "app-only"}

    assert MODULE._deployment_class([fragment], spec) == "maintenance"


def test_deployment_class_accepts_only_menhir_application_source(
    tmp_path: Path,
) -> None:
    repositories: dict[str, str] = {}
    prior_repos: dict[str, str] = {}
    for name in sorted(MODULE.REPOSITORIES):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
        )
        base = _commit(repo, "base.txt", "base\n")
        prior_repos[name] = base
        if name == "menhir":
            _commit(repo, "src/menhir/change.py", "VALUE = 1\n")
        repositories[name] = str(repo.resolve())
    prior = tmp_path / "prior-release.json"
    prior.write_text(json.dumps({"repos": prior_repos}), encoding="ascii")
    spec = {
        "repositories": repositories,
        "prior_release": str(prior.resolve()),
    }

    assert MODULE._deployment_class(
        [{"deployment_class": "app-only"}], spec
    ) == "app-only"


def _write_staged_workspace(tmp_path: Path, phase: str = "bundled") -> tuple[Path, dict]:
    workspace = tmp_path / "release-workspace"
    workspace.mkdir()
    files = {
        MODULE.SPEC_NAME: b"spec\n",
        MODULE.NOTES_JSON_NAME: b"{}\n",
        MODULE.NOTES_MARKDOWN_NAME: b"# notes\n",
        MODULE.REVIEW_REQUEST_NAME: b"{}\n",
    }
    if phase in {"bundled", "deployed"}:
        files.update({
            "security-review.json": b"{}\n",
            MODULE.RELEASE_NAME: b"{}\n",
            f"{MODULE.BUNDLE_NAME}/bundle-manifest.json": b"{}\n",
        })
    for name, payload in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    state = {
        "schema": MODULE.SCHEMA,
        "kind": MODULE.KIND,
        "phase": phase,
        "release_id": "menhir-prod-0.2.0-11",
        "release_author": "release-operator",
        "workspace": str(workspace.resolve()),
        "deployment_class": "security-config",
        "inputs_sha256": "1" * 64,
        "spec_sha256": _sha(workspace / MODULE.SPEC_NAME),
        "notes_json_sha256": _sha(workspace / MODULE.NOTES_JSON_NAME),
        "notes_markdown_sha256": _sha(workspace / MODULE.NOTES_MARKDOWN_NAME),
        "review_request_sha256": _sha(workspace / MODULE.REVIEW_REQUEST_NAME),
        "security_review_sha256": (
            _sha(workspace / "security-review.json") if phase in {"bundled", "deployed"} else None
        ),
        "release_sha256": (
            _sha(workspace / MODULE.RELEASE_NAME) if phase in {"bundled", "deployed"} else None
        ),
        "bundle_manifest_sha256": (
            _sha(workspace / MODULE.BUNDLE_NAME / "bundle-manifest.json")
            if phase in {"bundled", "deployed"} else None
        ),
        "bundle_sha256": (
            MODULE._tree_sha256(workspace / MODULE.BUNDLE_NAME)
            if phase in {"bundled", "deployed"} else None
        ),
    }
    MODULE._atomic_json(workspace / MODULE.STATE_NAME, state)
    return workspace, state


def test_deploy_requires_exact_release_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _write_staged_workspace(tmp_path)
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# test\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "DEFAULT_WRAPPER", wrapper)

    with pytest.raises(MODULE.ReleaseFlowError, match="exactly match"):
        MODULE.deploy_flow(workspace, "menhir-prod-0.2.0-12", execute=False)


def test_deploy_dry_run_preserves_state_and_selects_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state = _write_staged_workspace(tmp_path)
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# test\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "DEFAULT_WRAPPER", wrapper)

    command = MODULE.deploy_flow(
        workspace, "menhir-prod-0.2.0-11", execute=False
    )

    assert isinstance(command, list)
    assert command[command.index("-Mode") + 1] == "Maintenance"
    assert (
        command[command.index("-ExpectedBundleSha256") + 1]
        == state["bundle_sha256"]
    )
    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "bundled"


def test_deploy_records_success_only_after_runner_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _write_staged_workspace(tmp_path)
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# test\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "DEFAULT_WRAPPER", wrapper)
    seen: list[list[str]] = []

    result = MODULE.deploy_flow(
        workspace,
        "menhir-prod-0.2.0-11",
        execute=True,
        runner=seen.append,
    )

    assert seen
    assert isinstance(result, dict) and result["phase"] == "deployed"
    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "deployed"


def test_deploy_resume_does_not_run_transaction_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _write_staged_workspace(tmp_path, phase="deployed")
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# test\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "DEFAULT_WRAPPER", wrapper)

    result = MODULE.deploy_flow(
        workspace,
        "menhir-prod-0.2.0-11",
        execute=True,
        runner=lambda _command: pytest.fail("completed deployment ran again"),
    )

    assert isinstance(result, dict) and result["phase"] == "deployed"


def test_status_rejects_artifact_drift(tmp_path: Path) -> None:
    workspace, _ = _write_staged_workspace(tmp_path)
    (workspace / MODULE.NOTES_MARKDOWN_NAME).write_text("changed\n", encoding="ascii")

    with pytest.raises(MODULE.ReleaseFlowError, match="artifact changed"):
        MODULE.status_flow(workspace)


def test_status_rejects_bundle_payload_drift(tmp_path: Path) -> None:
    workspace, _ = _write_staged_workspace(tmp_path)
    (workspace / MODULE.BUNDLE_NAME / "extra").write_text(
        "changed\n", encoding="ascii"
    )

    with pytest.raises(MODULE.ReleaseFlowError, match="install bundle changed"):
        MODULE.status_flow(workspace)


def test_tree_sha256_is_portable_sorted_file_manifest(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "nested").mkdir(parents=True)
    (bundle / "z.txt").write_bytes(b"last\n")
    (bundle / "nested" / "a.txt").write_bytes(b"first\n")

    expected = hashlib.sha256()
    for relative in ("nested/a.txt", "z.txt"):
        payload = hashlib.sha256((bundle / Path(relative)).read_bytes()).hexdigest()
        expected.update(f"{relative}\0{payload}\n".encode("utf-8"))

    assert MODULE._tree_sha256(bundle) == expected.hexdigest()


def test_state_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    workspace = tmp_path / "release-workspace"
    workspace.mkdir()
    (workspace / MODULE.STATE_NAME).write_text(
        '{"schema":1,"schema":1}\n', encoding="ascii"
    )

    with pytest.raises(MODULE.ReleaseFlowError, match="duplicate JSON key"):
        MODULE._load_state(workspace.resolve())


def test_prepare_authors_review_request_and_binds_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, commits = _coverage_fixture(tmp_path)
    inputs = tmp_path / "inputs.json"
    inputs.write_text("{}\n", encoding="ascii")
    workspace = tmp_path / "flow"
    workspace.mkdir()
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    fragment = {
        "repositories": {"menhir": [commits["menhir"][1]]},
        "deployment_class": "security-config",
    }

    def prepare_release_spec(_inputs: Path, output: Path) -> None:
        output.write_text(
            json.dumps({
                **spec,
                "release_id": "menhir-prod-0.2.0-11",
                "release_author": "release-operator",
            }),
            encoding="utf-8",
        )

    modules = {
        "release_spec.py": SimpleNamespace(prepare_release_spec=prepare_release_spec),
        "release_notes.py": SimpleNamespace(
            collect_fragments=lambda _path: [fragment],
            render_markdown=lambda _rows, release_id: f"# {release_id}\n",
            render_json=lambda _rows, release_id: json.dumps({"release_id": release_id}) + "\n",
        ),
    }
    monkeypatch.setattr(MODULE, "_load_local_module", lambda _name, filename: modules[filename])

    def author(_spec: Path, destination: Path, security_review: Path | None = None) -> None:
        assert security_review is None
        destination.write_text(json.dumps({
            "release": {
                "release_id": "menhir-prod-0.2.0-11",
                "release_author": "release-operator",
            }
        }), encoding="utf-8")

    monkeypatch.setattr(MODULE, "_run_release_author", author)

    state = MODULE.prepare_flow(inputs.resolve(), workspace.resolve(), fragments.resolve())

    assert state["phase"] == "review_requested"
    assert state["deployment_class"] == "maintenance"
    assert (workspace / MODULE.REVIEW_REQUEST_NAME).exists()
    assert MODULE.status_flow(workspace.resolve()) == state

    resumed = MODULE.prepare_flow(inputs.resolve(), workspace.resolve(), fragments.resolve())
    assert resumed == state


def test_prepare_failure_restores_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tmp_path / "inputs.json"
    inputs.write_text("{}\n", encoding="ascii")
    workspace = tmp_path / "flow"
    workspace.mkdir()
    fragments = tmp_path / "fragments"
    fragments.mkdir()

    def prepare_release_spec(_inputs: Path, output: Path) -> None:
        output.write_text("{}\n", encoding="ascii")
        (output.parent / "release-spec-inputs").mkdir()

    modules = {
        "release_spec.py": SimpleNamespace(prepare_release_spec=prepare_release_spec),
        "release_notes.py": SimpleNamespace(
            collect_fragments=lambda _path: [],
        ),
    }
    monkeypatch.setattr(
        MODULE, "_load_local_module", lambda _name, filename: modules[filename]
    )

    with pytest.raises(MODULE.ReleaseFlowError, match="repositories are invalid"):
        MODULE.prepare_flow(inputs.resolve(), workspace.resolve(), fragments.resolve())
    assert list(workspace.iterdir()) == []


def test_finalize_builds_bundle_only_after_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _write_staged_workspace(tmp_path, phase="review_requested")
    review = tmp_path / "approved-review.json"
    review.write_text('{"verdict":"APPROVED"}\n', encoding="ascii")

    def author(_spec: Path, destination: Path, security_review: Path | None = None) -> None:
        assert security_review is not None
        assert security_review.name == "security-review.json"
        assert security_review.parent.parent == workspace
        destination.write_text(
            '{"release_id":"menhir-prod-0.2.0-11"}\n', encoding="ascii"
        )

    def build(
        _release: Path,
        _spec: Path,
        output: Path,
    ) -> None:
        output.mkdir()
        (output / "bundle-manifest.json").write_text(
            '{"release_id":"menhir-prod-0.2.0-11"}\n', encoding="ascii"
        )

    monkeypatch.setattr(MODULE, "_run_release_author", author)
    monkeypatch.setattr(
        MODULE,
        "_load_local_module",
        lambda _name, _filename: SimpleNamespace(build_install_bundle=build),
    )

    state = MODULE.finalize_flow(workspace.resolve(), review.resolve())

    assert state["phase"] == "bundled"
    assert MODULE.status_flow(workspace.resolve()) == state

    resumed = MODULE.finalize_flow(workspace.resolve(), review.resolve())
    assert resumed == state


def test_finalize_failure_leaves_review_requested_state_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, original_state = _write_staged_workspace(
        tmp_path, phase="review_requested"
    )
    review = tmp_path / "approved-review.json"
    review.write_text('{"verdict":"APPROVED"}\n', encoding="ascii")

    def author(_spec: Path, destination: Path, security_review: Path | None = None) -> None:
        assert security_review is not None
        destination.write_text(
            '{"release_id":"menhir-prod-0.2.0-11"}\n', encoding="ascii"
        )

    def fail_build(_release: Path, _spec: Path, output: Path) -> None:
        output.mkdir()
        (output / "partial").write_text("partial\n", encoding="ascii")
        raise ValueError("injected bundle failure")

    monkeypatch.setattr(MODULE, "_run_release_author", author)
    monkeypatch.setattr(
        MODULE,
        "_load_local_module",
        lambda _name, _filename: SimpleNamespace(build_install_bundle=fail_build),
    )

    with pytest.raises(ValueError, match="injected bundle failure"):
        MODULE.finalize_flow(workspace.resolve(), review.resolve())
    assert MODULE.status_flow(workspace.resolve()) == original_state
    assert not (workspace / "security-review.json").exists()
    assert not (workspace / MODULE.RELEASE_NAME).exists()
    assert not (workspace / MODULE.BUNDLE_NAME).exists()
    assert list(workspace.glob(".release-finalize.*")) == []
