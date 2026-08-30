from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "release-author.py"
SPEC = importlib.util.spec_from_file_location("release_author", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_line(name: str, payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(payload).digest()
    ).rstrip(b"=").decode("ascii")
    return f"{name},sha256={digest},{len(payload)}\n"


def _refresh_record(members: dict[str, bytes]) -> None:
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    assert len(record_names) == 1
    record_name = record_names[0]
    members[record_name] = (
        "".join(
            _record_line(name, payload)
            for name, payload in members.items()
            if name != record_name
        )
        + f"{record_name},,\n"
    ).encode("ascii")


def _repo(path: Path, remote: str, files: dict[str, str]) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="ascii")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _wheel(path: Path) -> Path:
    wheel = path / "archolith_oauth-1.0-py3-none-any.whl"
    metadata_dir = "archolith_oauth-1.0.dist-info"
    members = {
        "archolith_oauth/__init__.py": b"VALUE = 1\n",
        f"{metadata_dir}/METADATA": b"Name: archolith-oauth\nVersion: 1.0\n",
        f"{metadata_dir}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{metadata_dir}/entry_points.txt": (
            b"[console_scripts]\narcholith-oauth = archolith_oauth.cli:main\n"
        ),
        f"{metadata_dir}/top_level.txt": b"archolith_oauth\n",
        f"{metadata_dir}/licenses/LICENSE": b"test license\n",
    }
    record_name = f"{metadata_dir}/RECORD"
    members[record_name] = b""
    _refresh_record(members)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return wheel


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    repo_names = ("menhir", "archolith_oauth", "yawn_deploy", "yawn_vps")
    repos: dict[str, str] = {}
    commits: dict[str, str] = {}
    artifact_sources: dict[str, dict[str, str]] = {}
    repo_files = {name: {"tracked.txt": "tracked\n"} for name in repo_names}
    repo_files["archolith_oauth"]["src/archolith_oauth/__init__.py"] = "VALUE = 1\n"
    for index, destination in enumerate(sorted(MODULE.REQUIRED_ARTIFACT_DESTINATIONS)):
        if destination in MODULE.RENDERED_ARTIFACT_DESTINATIONS:
            artifact_sources[destination] = {
                "kind": "rendered",
                "rendered_key": MODULE.RENDERED_ARTIFACT_DESTINATIONS[destination],
            }
            continue
        source_path = f"release-artifacts/artifact-{index}"
        repo_files["menhir"][source_path] = destination + "\n"
        artifact_sources[destination] = {
            "kind": "git",
            "repository": "menhir",
            "path": source_path,
        }
    for name in repo_names:
        repo_path = tmp_path / name
        commits[name] = _repo(
            repo_path, MODULE.EXPECTED_REPO_REMOTES[name], repo_files[name]
        )
        repos[name] = str(repo_path.resolve())

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    oauth_wheel = _wheel(wheelhouse)
    docker_manifest = wheelhouse / "SHA256SUMS"
    docker_manifest.write_text(f"{_sha(oauth_wheel)}  {oauth_wheel.name}\n", encoding="ascii")
    wheel_manifest = tmp_path / "wheel-build.json"
    wheel_manifest.write_text('{"schema":1}\n', encoding="ascii")
    images = {name: "sha256:" + str(index) * 64 for index, name in enumerate(
        ("menhir", "neo4j", "caddy", "base"), start=1
    )}
    provenance = tmp_path / "provenance.json"
    provenance.write_text(json.dumps({
        "schema": 1,
        "repos": commits,
        "repo_remotes": MODULE.EXPECTED_REPO_REMOTES,
        "images": images,
        "oauth_wheel_sha256": _sha(oauth_wheel),
        "wheel_manifest_sha256": _sha(wheel_manifest),
        "dockerfile_wheel_manifest_sha256": _sha(docker_manifest),
    }, sort_keys=True), encoding="utf-8")

    evidence: dict[str, str] = {
        "oauth_wheel": str(oauth_wheel.resolve()),
        "wheelhouse": str(wheelhouse.resolve()),
        "wheel_manifest": str(wheel_manifest.resolve()),
        "dockerfile_wheel_manifest": str(docker_manifest.resolve()),
        "provenance": str(provenance.resolve()),
    }
    for name in ("sbom", "scan"):
        path = tmp_path / f"{name}.json"
        path.write_text('{"ok":true}\n', encoding="ascii")
        evidence[name] = str(path.resolve())

    rendered: dict[str, str] = {}
    policy_payload = {"version": 1, "clients": {}}
    policy_digest = hashlib.sha256(json.dumps(
        policy_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    for name in MODULE.RENDERED:
        path = tmp_path / name
        if name == "policy_sha256":
            path.write_text(json.dumps({
                **policy_payload,
                "canonical_digest": policy_digest,
            }), encoding="ascii")
        elif name == "production_env_sha256":
            path.write_text(
                f"MENHIR_CLIENT_POLICY_DIGEST={policy_digest}\n",
                encoding="ascii",
            )
        else:
            path.write_text(name + "\n", encoding="ascii")
        rendered[name] = str(path.resolve())
    prior_route = tmp_path / "prior-route.json"
    prior_route.write_text('{"route":"legacy"}\n', encoding="ascii")
    initial_host = tmp_path / "initial-host.json"
    initial_host.write_text('{"host":"pre-menhir"}\n', encoding="ascii")
    secrets = {name: f"version-{name}" for name in MODULE.SECRET_VERSIONS}
    spec = {
        "schema": 1,
        "release_id": "menhir-prod-0.2.0-1",
        "release_author": "release-operator@example.com",
        "repositories": repos,
        "images": images,
        "evidence": evidence,
        "rendered": rendered,
        "network": {
            "project": "menhir-prod",
            "external_network": "menhir-proxy",
            "alias": "menhir-prod-app",
            "peers": ["172.30.0.2"],
        },
        "initial_release": True,
        "prior_release": None,
        "prior_route": str(prior_route.resolve()),
        "initial_host_state": str(initial_host.resolve()),
        "initial_prior_images": {
            "menhir": "sha256:" + "a" * 64,
            "neo4j": "sha256:" + "b" * 64,
            "caddy": "sha256:" + "c" * 64,
        },
        "secret_version_ids": secrets,
        "artifact_sources": artifact_sources,
    }
    spec_path = tmp_path / "release-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, tmp_path / "release.json", spec


def _security_review(spec_path: Path, output: Path) -> Path:
    request_path = output.with_name(output.name + ".review-request.json")
    request = MODULE.author_release(
        spec_path, request_path, review_request=True
    )
    review = {
        "schema": 1,
        "kind": "menhir-production-security-review",
        "review_id": "security-review-1",
        "release_author": request["release"]["release_author"],
        "reviewer": "independent-security@example.com",
        "reviewed_utc": datetime.now(timezone.utc).isoformat(),
        "authority_sha256": request["authority_sha256"],
        "verdict": "APPROVED",
        "unresolved_findings": {"critical": 0, "high": 0},
        "scope": sorted(MODULE.menhir_schema.REQUIRED_SECURITY_REVIEW_SCOPE),
        "report_sha256": "a" * 64,
    }
    review_path = output.with_name(output.name + ".security-review.json")
    review_path.write_text(json.dumps(review), encoding="utf-8")
    return review_path


def _author(spec_path: Path, output: Path) -> dict:
    return MODULE.author_release(
        spec_path, output, _security_review(spec_path, output)
    )


def test_authors_canonical_release_from_clean_exact_inputs(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)

    release = _author(spec_path, output)

    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8")) == release
    assert release["security_review"]["authority_sha256"] == (
        MODULE.menhir_schema.release_authority_sha256(release)
    )
    assert release["security_review"]["review_artifact_sha256"] == _sha(
        output.with_name(output.name + ".security-review.json")
    )
    assert release["rollback_anchors"]["prior_release_id"] == ""
    assert release["rollback_anchors"]["initial_release"] is True
    assert release["oauth_wheel_sha256"] == _sha(
        Path(json.loads(spec_path.read_text())["evidence"]["oauth_wheel"])
    )
    assert release["oauth_wheel_source"] == {
        "repository": "archolith_oauth",
        "commit": release["repos"]["archolith_oauth"],
        "source_tree_sha256": MODULE._git_package_tree_digest(
            Path(spec["repositories"]["archolith_oauth"]),
            release["repos"]["archolith_oauth"],
        ),
        "wheel_sha256": release["oauth_wheel_sha256"],
    }
    destination = next(
        path for path, source in spec["artifact_sources"].items()
        if source["kind"] == "git"
    )
    entry = release["artifacts"][destination]
    source = spec["artifact_sources"][destination]
    committed = subprocess.run(
        ["git", "-C", spec["repositories"][source["repository"]], "show",
         f'{entry["commit"]}:{source["path"]}'],
        check=True,
        capture_output=True,
    ).stdout
    assert entry["sha256"] == hashlib.sha256(committed).hexdigest()
    assert entry["blob_oid"] == subprocess.run(
        ["git", "-C", spec["repositories"][source["repository"]], "rev-parse",
         f'{entry["commit"]}:{source["path"]}'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_refuses_production_env_bound_to_raw_policy_file_digest(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    policy = Path(spec["rendered"]["policy_sha256"])
    env = Path(spec["rendered"]["production_env_sha256"])
    env.write_text(
        f"MENHIR_CLIENT_POLICY_DIGEST={_sha(policy)}\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="client policy canonical_digest"):
        _author(spec_path, output)


@pytest.mark.parametrize(
    ("destination", "rendered_key"),
    (
        (
            "/etc/yawn-vps/menhir-oauth-policy.json",
            "operations_policy_sha256",
        ),
        (
            "/etc/yawn-vps/menhir-oauth-public.pem",
            "oauth_public_key_sha256",
        ),
    ),
)
def test_oauth_authority_files_are_required_rendered_artifacts(
    tmp_path: Path, destination: str, rendered_key: str
) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    release = _author(spec_path, output)

    assert MODULE.RENDERED_ARTIFACT_DESTINATIONS[destination] == rendered_key
    assert release["artifacts"][destination] == {
        "kind": "rendered",
        "sha256": release["rendered"][rendered_key],
        "rendered_key": rendered_key,
    }

    del spec["artifact_sources"][destination]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="installed-artifacts.json"):
        _author(spec_path, tmp_path / "missing-artifact.json")


def test_refuses_dirty_repository(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    (Path(spec["repositories"]["menhir"]) / "untracked.txt").write_text("dirty\n")

    with pytest.raises(ValueError, match="not clean"):
        _author(spec_path, output)


def test_refuses_noncanonical_repository_remote(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    subprocess.run(
        ["git", "-C", spec["repositories"]["yawn_vps"], "remote", "set-url",
         "origin", "https://github.com/attacker/yawn.vps.git"],
        check=True,
    )
    with pytest.raises(ValueError, match="origin identity mismatch"):
        _author(spec_path, output)


def test_refuses_provenance_mismatch(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    provenance = Path(spec["evidence"]["provenance"])
    value = json.loads(provenance.read_text())
    value["oauth_wheel_sha256"] = "0" * 64
    provenance.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance"):
        _author(spec_path, output)


def test_refuses_oauth_wheel_payload_not_from_reviewed_commit(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    wheel = Path(spec["evidence"]["oauth_wheel"])
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["archolith_oauth/__init__.py"] = b"VALUE = 999\n"
    _refresh_record(members)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    wheel_sha = _sha(wheel)
    docker_manifest = Path(spec["evidence"]["dockerfile_wheel_manifest"])
    docker_manifest.write_text(f"{wheel_sha}  {wheel.name}\n", encoding="ascii")
    provenance = Path(spec["evidence"]["provenance"])
    value = json.loads(provenance.read_text())
    value["oauth_wheel_sha256"] = wheel_sha
    value["dockerfile_wheel_manifest_sha256"] = _sha(docker_manifest)
    provenance.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="reviewed OAuth source commit"):
        _author(spec_path, output)


def test_refuses_oauth_wheel_executable_payload_outside_reviewed_package(
    tmp_path: Path,
) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    wheel = Path(spec["evidence"]["oauth_wheel"])
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["sitecustomize.py"] = b"raise RuntimeError('unreviewed')\n"
    _refresh_record(members)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    wheel_sha = _sha(wheel)
    docker_manifest = Path(spec["evidence"]["dockerfile_wheel_manifest"])
    docker_manifest.write_text(f"{wheel_sha}  {wheel.name}\n", encoding="ascii")
    provenance = Path(spec["evidence"]["provenance"])
    value = json.loads(provenance.read_text())
    value["oauth_wheel_sha256"] = wheel_sha
    value["dockerfile_wheel_manifest_sha256"] = _sha(docker_manifest)
    provenance.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="unreviewed installable payload"):
        _author(spec_path, output)


def test_non_initial_release_requires_prior_release(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    spec["initial_release"] = False
    spec["prior_release"] = None
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError, match="requires prior_release"):
        _author(spec_path, output)


def test_non_initial_release_pins_complete_prior_release_digest(tmp_path: Path) -> None:
    spec_path, prior_output, spec = _fixture(tmp_path)
    prior = _author(spec_path, prior_output)
    spec["release_id"] = "menhir-prod-0.2.0-2"
    spec["initial_release"] = False
    spec["prior_release"] = str(prior_output.resolve())
    spec["initial_host_state"] = None
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    output = tmp_path / "release-2.json"

    release = _author(spec_path, output)
    assert release["rollback_anchors"]["prior_release_id"] == prior["release_id"]
    assert release["rollback_anchors"]["prior_release_sha256"] == _sha(prior_output)
    assert release["rollback_anchors"]["initial_host_state_sha256"] == ""


def test_refuses_release_without_independent_security_review(tmp_path: Path) -> None:
    spec_path, output, _ = _fixture(tmp_path)

    with pytest.raises(ValueError, match="security review is required"):
        MODULE.author_release(spec_path, output)


def test_refuses_overwriting_security_review_with_release(tmp_path: Path) -> None:
    spec_path, output, _ = _fixture(tmp_path)
    review_path = _security_review(spec_path, output)

    with pytest.raises(ValueError, match="must not overwrite"):
        MODULE.author_release(spec_path, review_path, review_path)


def test_refuses_security_review_for_different_release_authority(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    review_path = _security_review(spec_path, output)
    spec["release_author"] = "different-release-operator@example.com"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError, match="exact release authority"):
        MODULE.author_release(spec_path, output, review_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda review: review.update(verdict="REJECTED"), "verdict"),
        (
            lambda review: review["unresolved_findings"].update(high=1),
            "unresolved high",
        ),
        (
            lambda review: review.update(reviewer=review["release_author"]),
            "independent",
        ),
        (lambda review: review["scope"].pop(), "scope"),
    ),
)
def test_refuses_unapproved_or_non_independent_security_review(
    tmp_path: Path, mutation, message: str
) -> None:
    spec_path, output, _ = _fixture(tmp_path)
    review_path = _security_review(spec_path, output)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    mutation(review)
    review_path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        MODULE.author_release(spec_path, output, review_path)
