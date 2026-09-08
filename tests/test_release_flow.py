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


def test_next_release_id_increments_sequence_or_resets_for_new_version(
    tmp_path: Path,
) -> None:
    prior = tmp_path / "release.json"
    prior.write_text(
        json.dumps({"release_id": "menhir-prod-0.2.0-13"}), encoding="utf-8"
    )

    assert MODULE.next_release_id(prior.resolve())["release_id"] \
        == "menhir-prod-0.2.0-14"
    assert MODULE.next_release_id(prior.resolve(), "0.3.0")["release_id"] \
        == "menhir-prod-0.3.0-1"


def test_next_release_id_refuses_malformed_version(tmp_path: Path) -> None:
    prior = tmp_path / "release.json"
    prior.write_text(
        json.dumps({"release_id": "menhir-prod-0.2.0-13"}), encoding="utf-8"
    )

    with pytest.raises(MODULE.ReleaseFlowError, match="major"):
        MODULE.next_release_id(prior.resolve(), "0.3")


def test_next_release_id_refuses_version_regression(tmp_path: Path) -> None:
    prior = tmp_path / "release.json"
    prior.write_text(
        json.dumps({"release_id": "menhir-prod-1.2.3-4"}), encoding="utf-8"
    )

    with pytest.raises(MODULE.ReleaseFlowError, match="backwards"):
        MODULE.next_release_id(prior.resolve(), "1.2.2")


def test_prepare_sequence_enforces_generated_label(tmp_path: Path) -> None:
    prior = tmp_path / "prior.json"
    prior.write_text(
        json.dumps({"release_id": "menhir-prod-0.2.0-13"}), encoding="utf-8"
    )

    MODULE._verify_next_release_id({
        "release_id": "menhir-prod-0.2.0-14",
        "initial_release": False,
        "prior_release": str(prior.resolve()),
    })
    with pytest.raises(MODULE.ReleaseFlowError, match="generated next label"):
        MODULE._verify_next_release_id({
            "release_id": "menhir-prod-0.2.0-15",
            "initial_release": False,
            "prior_release": str(prior.resolve()),
        })


def test_initial_release_sequence_must_start_at_one() -> None:
    with pytest.raises(MODULE.ReleaseFlowError, match="sequence must be 1"):
        MODULE._verify_next_release_id({
            "release_id": "menhir-prod-0.2.0-2",
            "initial_release": True,
            "prior_release": None,
        })


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
    prior.write_text(json.dumps({
        "release_id": "menhir-prod-0.2.0-10",
        "repos": prior_repos,
    }), encoding="utf-8")
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
    authority = {
        "release_id": "menhir-prod-0.2.0-11",
        "release_author": "release-operator",
        "deployment_class": "security-config",
        "ingress_mode": "cloudflared",
        "notes_json_sha256": _sha(workspace / MODULE.NOTES_JSON_NAME),
        "notes_markdown_sha256": _sha(workspace / MODULE.NOTES_MARKDOWN_NAME),
    }
    (workspace / MODULE.SPEC_NAME).write_text(
        json.dumps(authority), encoding="utf-8"
    )
    (workspace / MODULE.REVIEW_REQUEST_NAME).write_text(
        json.dumps({"release": authority}), encoding="utf-8"
    )
    if phase in {"bundled", "deployed"}:
        (workspace / MODULE.RELEASE_NAME).write_text(
            json.dumps(authority), encoding="utf-8"
        )
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


def _publication_workspace(
    tmp_path: Path,
    fragments: dict[str, bytes] | None = None,
) -> tuple[Path, dict, Path]:
    workspace, state = _write_staged_workspace(tmp_path)
    fragments_dir = tmp_path / "changes" / "unreleased"
    fragments_dir.mkdir(parents=True)
    payloads = fragments or {
        "first.json": b'{"id":"first"}\n',
        "second.json": b'{"id":"second"}\n',
    }
    for name, payload in payloads.items():
        (fragments_dir / name).write_bytes(payload)
    state.update({
        "fragments_dir": str(fragments_dir.resolve()),
        "fragments": [
            {"name": name, "sha256": _sha(fragments_dir / name)}
            for name in sorted(payloads)
        ],
        "publication_nonce": None,
        "publication_receipt_sha256": None,
    })
    MODULE._atomic_json(workspace / MODULE.STATE_NAME, state)
    return workspace, state, fragments_dir


def test_deploy_requires_exact_release_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _write_staged_workspace(tmp_path)
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# test\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "DEFAULT_WRAPPER", wrapper)

    with pytest.raises(MODULE.ReleaseFlowError, match="exactly match"):
        MODULE.deploy_flow(workspace, "menhir-prod-0.2.0-12", execute=False)


def test_deploy_dry_run_preserves_state_and_selects_security_config(
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
    assert command[command.index("-Mode") + 1] == "SecurityConfig"
    assert (
        command[command.index("-ExpectedBundleSha256") + 1]
        == state["bundle_sha256"]
    )
    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "bundled"


def test_direct_deploy_execution_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _write_staged_workspace(tmp_path)
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# test\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "DEFAULT_WRAPPER", wrapper)
    with pytest.raises(MODULE.ReleaseFlowError, match="personal_deploy.py"):
        MODULE.deploy_flow(
            workspace,
            "menhir-prod-0.2.0-11",
            execute=True,
            runner=lambda _command: pytest.fail("legacy deployment runner was invoked"),
        )

    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "bundled"


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


def test_publish_archives_only_bound_fragments_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)
    later = fragments_dir / "later.json"
    later.write_bytes(b'{"id":"later"}\n')
    monkeypatch.setattr(
        MODULE,
        "deployment_command",
        lambda *_args: pytest.fail("publication attempted to deploy production"),
    )
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda *_args: pytest.fail("publication attempted to run a deployment"),
    )

    published = MODULE.publish_flow(workspace.resolve(), state["release_id"])
    archive = fragments_dir.parent / "releases" / state["release_id"]
    receipt = archive / MODULE.PUBLICATION_RECEIPT_NAME

    assert published["phase"] == "published"
    assert published["publication_receipt_sha256"] == _sha(receipt)
    assert sorted(path.name for path in archive.iterdir()) == [
        "first.json",
        MODULE.PUBLICATION_RECEIPT_NAME,
        "second.json",
    ]
    assert later.exists()
    assert not (archive / MODULE.BUNDLE_NAME).exists()
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_value["artifacts"] == {
        key: state[key] for key in MODULE.FROZEN_ARTIFACT_KEYS
    }
    assert receipt_value["fragments"] == state["fragments"]

    receipt_bytes = receipt.read_bytes()
    resumed = MODULE.publish_flow(workspace.resolve(), state["release_id"])
    assert resumed == published
    assert receipt.read_bytes() == receipt_bytes


def test_publish_requires_exact_release_confirmation_before_mutation(
    tmp_path: Path,
) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)

    with pytest.raises(MODULE.ReleaseFlowError, match="exactly match"):
        MODULE.publish_flow(workspace.resolve(), "menhir-prod-0.2.0-12")

    assert all((fragments_dir / row["name"]).exists() for row in state["fragments"])
    assert not (fragments_dir.parent / "releases").exists()
    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "bundled"


def test_legacy_bundled_workspace_remains_readable_but_cannot_publish(
    tmp_path: Path,
) -> None:
    workspace, state = _write_staged_workspace(tmp_path)

    assert MODULE.status_flow(workspace.resolve()) == state
    with pytest.raises(MODULE.ReleaseFlowError, match="legacy release flow"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])


def test_publish_refuses_preexisting_unowned_release_archive(tmp_path: Path) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)
    archive = fragments_dir.parent / "releases" / state["release_id"]
    archive.mkdir(parents=True)
    for binding in state["fragments"]:
        (archive / binding["name"]).write_bytes(
            (fragments_dir / binding["name"]).read_bytes()
        )
    (archive / MODULE.PUBLICATION_RECEIPT_NAME).write_text(
        '{"kind":"menhir-release-publication"}\n', encoding="ascii"
    )

    with pytest.raises(MODULE.ReleaseFlowError, match="unexpected release archive"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])

    assert all((fragments_dir / row["name"]).exists() for row in state["fragments"])
    persisted = json.loads((workspace / MODULE.STATE_NAME).read_text())
    assert persisted["phase"] == "bundled"
    assert persisted["publication_nonce"] is None


def test_publish_refuses_fragment_drift_before_archiving(tmp_path: Path) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)
    changed = fragments_dir / state["fragments"][0]["name"]
    changed.write_bytes(b"changed after prepare\n")

    with pytest.raises(MODULE.ReleaseFlowError, match="fragment changed"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])

    assert changed.exists()
    assert not (fragments_dir.parent / "releases" / state["release_id"]).exists()
    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "bundled"


def test_publish_refuses_a_fragment_replaced_during_atomic_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state, fragments_dir = _publication_workspace(
        tmp_path, {"only.json": b'{"id":"only"}\n'}
    )
    source = fragments_dir / "only.json"
    real_replace = MODULE.os.replace

    def replace_after_check(old: Path | str, new: Path | str) -> None:
        if Path(old) == source:
            source.write_bytes(b"replacement bytes\n")
        real_replace(old, new)

    monkeypatch.setattr(MODULE.os, "replace", replace_after_check)

    with pytest.raises(MODULE.ReleaseFlowError, match="replaced while publishing"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])

    assert source.read_bytes() == b"replacement bytes\n"
    assert not (fragments_dir.parent / "releases" / state["release_id"]).exists()
    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "publishing"


def test_publish_resumes_after_an_interrupted_fragment_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)
    real_replace = MODULE.os.replace
    moves = 0

    def interrupt_second_move(old: Path | str, new: Path | str) -> None:
        nonlocal moves
        if Path(old).parent == fragments_dir:
            moves += 1
            if moves == 2:
                raise OSError("injected interruption")
        real_replace(old, new)

    monkeypatch.setattr(MODULE.os, "replace", interrupt_second_move)
    with pytest.raises(OSError, match="injected interruption"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])
    monkeypatch.setattr(MODULE.os, "replace", real_replace)

    assert json.loads((workspace / MODULE.STATE_NAME).read_text())["phase"] == "publishing"
    interrupted = json.loads((workspace / MODULE.STATE_NAME).read_text())
    staging = fragments_dir.parent / "releases" / (
        f".{state['release_id']}.{interrupted['publication_nonce']}.publishing"
    )
    assert staging.is_dir()
    assert len(list(staging.iterdir())) == 1

    published = MODULE.publish_flow(workspace.resolve(), state["release_id"])
    assert published["phase"] == "published"
    assert not staging.exists()
    assert MODULE.status_flow(workspace.resolve()) == published


def test_publish_recovers_only_its_nonce_bound_committed_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)
    real_replace = MODULE.os.replace

    def crash_after_archive_commit(old: Path | str, new: Path | str) -> None:
        real_replace(old, new)
        if Path(new).name == state["release_id"]:
            raise OSError("crash after archive commit")

    monkeypatch.setattr(MODULE.os, "replace", crash_after_archive_commit)
    with pytest.raises(OSError, match="crash after archive commit"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])
    monkeypatch.setattr(MODULE.os, "replace", real_replace)

    interrupted = json.loads((workspace / MODULE.STATE_NAME).read_text())
    assert interrupted["phase"] == "publishing"
    assert MODULE.PUBLICATION_NONCE_RE.fullmatch(interrupted["publication_nonce"])

    published = MODULE.publish_flow(workspace.resolve(), state["release_id"])
    assert published["phase"] == "published"
    receipt = (
        fragments_dir.parent
        / "releases"
        / state["release_id"]
        / MODULE.PUBLICATION_RECEIPT_NAME
    )
    assert json.loads(receipt.read_text())["publication_nonce"] == interrupted[
        "publication_nonce"
    ]


def test_publish_validates_all_frozen_artifacts_before_moving_fragments(
    tmp_path: Path,
) -> None:
    workspace, state, fragments_dir = _publication_workspace(tmp_path)
    (workspace / MODULE.BUNDLE_NAME / "extra").write_bytes(b"drift\n")

    with pytest.raises(MODULE.ReleaseFlowError, match="install bundle changed"):
        MODULE.publish_flow(workspace.resolve(), state["release_id"])

    assert all((fragments_dir / row["name"]).exists() for row in state["fragments"])
    assert not (fragments_dir.parent / "releases").exists()


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


def test_current_state_rejects_release_authority_binding_mismatch(
    tmp_path: Path,
) -> None:
    workspace, state, _ = _publication_workspace(tmp_path)
    state["ingress_mode"] = "cloudflared"
    state["deployment_class"] = "maintenance"
    MODULE._atomic_json(workspace / MODULE.STATE_NAME, state)

    with pytest.raises(MODULE.ReleaseFlowError, match="release spec deployment_class"):
        MODULE.status_flow(workspace.resolve())


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
    (fragments / "prepared.json").write_text("{}\n", encoding="ascii")
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
        authored_spec = json.loads(_spec.read_text(encoding="utf-8"))
        destination.write_text(json.dumps({
            "release": {
                "release_id": "menhir-prod-0.2.0-11",
                "release_author": "release-operator",
                "deployment_class": authored_spec["deployment_class"],
                "ingress_mode": authored_spec["ingress_mode"],
                "notes_json_sha256": authored_spec["notes_json_sha256"],
                "notes_markdown_sha256": authored_spec["notes_markdown_sha256"],
            }
        }), encoding="utf-8")

    monkeypatch.setattr(MODULE, "_run_release_author", author)

    state = MODULE.prepare_flow(inputs.resolve(), workspace.resolve(), fragments.resolve())

    assert state["phase"] == "review_requested"
    assert state["deployment_class"] == "maintenance"
    prepared_spec = json.loads((workspace / MODULE.SPEC_NAME).read_text(encoding="utf-8"))
    assert prepared_spec["deployment_class"] == state["deployment_class"]
    assert prepared_spec["notes_json_sha256"] == state["notes_json_sha256"]
    assert prepared_spec["notes_markdown_sha256"] == state["notes_markdown_sha256"]
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
        output.write_text(json.dumps({
            "release_id": "menhir-prod-0.2.0-1",
            "initial_release": True,
            "repositories": {},
        }), encoding="ascii")
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
