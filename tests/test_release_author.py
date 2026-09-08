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


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )


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


def _image_publication_bundle(
    root: Path,
    *,
    commit: str,
    release_id: str,
    menhir_ref: str,
    menhir_digest: str,
    base_ref: str,
    wheel_manifest_sha256: str,
    oauth_wheel_sha256: str,
) -> dict[str, str]:
    root.mkdir()
    archive = root / "release-image.tar"
    archive.write_bytes(b"sealed Menhir image archive")
    archive_sha = _sha(archive)
    image_id = "sha256:" + "5" * 64
    config_sha = "6" * 64
    version = release_id.removeprefix("menhir-prod-")
    repository = menhir_ref.rpartition("@")[0]
    image_tag = f"{repository}:{version}"
    subject = {"image_id": image_id, "image_archive_sha256": archive_sha}
    sbom = root / "sbom.syft.json"
    scan = root / "scan.grype.json"
    _write_json(sbom, {
        "descriptor": {"name": "syft", "version": "1.51.1"},
        "source": {"type": "image", "target": {"id": image_id}},
        "schema": {
            "version": "16.0.0",
            "url": "https://raw.githubusercontent.com/anchore/syft/main/schema/json/schema-16.0.0.json",
        },
        "artifacts": [{"id": "pkg-1", "name": "menhir"}],
        "artifactRelationships": [],
    })
    _write_json(scan, {
        "descriptor": {
            "name": "grype",
            "version": "0.118.0",
            "db": {"built": "2026-09-08T00:00:00Z", "checksum": "sha256:db"},
        },
        "source": {"type": "image", "target": {"imageID": image_id}},
        "matches": [],
        "ignoredMatches": [],
    })
    builder = MODULE.release_spec.build_release_image
    evidence = {
        "required": True,
        "sbom": {
            "artifact_path": "release-image-evidence/sbom.syft.json",
            "sha256": _sha(sbom),
            "scanner_image": "docker.io/anchore/syft@sha256:" + "a" * 64,
            "subject": subject,
            "validation": builder.validate_sbom(sbom.read_bytes(), image_id),
        },
        "vulnerability_scan": {
            "artifact_path": "release-image-evidence/scan.grype.json",
            "sha256": _sha(scan),
            "scanner_image": "docker.io/anchore/grype@sha256:" + "b" * 64,
            "subject": subject,
            "validation": builder.validate_vulnerability_scan(
                scan.read_bytes(), image_id
            ),
        },
    }
    metadata = root / "release-image-metadata.json"
    _write_json(metadata, {
        "schema": 3,
        "source_commit": commit,
        "image_tag": image_tag,
        "image_id": image_id,
        "config_sha256": config_sha,
        "image_archive_sha256": archive_sha,
        "python_base": base_ref,
        "labels": {
            "commit": commit,
            "version": version,
            "wheel_manifest_sha256": wheel_manifest_sha256,
            "oauth_wheel_sha256": oauth_wheel_sha256,
        },
        "evidence": evidence,
    })
    identity_evidence = {
        "sbom": evidence["sbom"]["sha256"],
        "vulnerability_scan": evidence["vulnerability_scan"]["sha256"],
    }
    identity = root / "release-image-identity.json"
    _write_json(identity, {
        "schema": 2,
        "source_commit": commit,
        "image_tag": image_tag,
        "image_id": image_id,
        "config_sha256": config_sha,
        "image_archive": {
            "artifact_path": "release-image.tar",
            "sha256": archive_sha,
        },
        "metadata": {
            "artifact_path": "release-image-metadata.json",
            "sha256": _sha(metadata),
        },
        "evidence": identity_evidence,
    })
    candidate_tag = f"{repository}:candidate-{archive_sha}"
    publication = root / "release-image-publication.json"
    _write_json(publication, {
        "schema": 2,
        "validation_identity_sha256": _sha(identity),
        "source_commit": commit,
        "image_tag": image_tag,
        "image_id": image_id,
        "config_sha256": config_sha,
        "image_archive_sha256": archive_sha,
        "registry_digest": menhir_digest,
        "image_ref": menhir_ref,
        "candidate_tag": candidate_tag,
        "candidate_ref": f"{candidate_tag}@{menhir_digest}",
        "idempotent_existing_candidate": False,
        "evidence": identity_evidence,
    })
    return {
        "sbom": str(sbom.resolve()),
        "scan": str(scan.resolve()),
        "image_publication": str(publication.resolve()),
        "image_metadata": str(metadata.resolve()),
        "image_identity": str(identity.resolve()),
        "image_archive": str(archive.resolve()),
    }


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
    image_refs = {
        name: f"ghcr.io/archolith/{name}@{digest}"
        for name, digest in images.items()
    }
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
    evidence.update(_image_publication_bundle(
        tmp_path / "image-publication",
        commit=commits["menhir"],
        release_id="menhir-prod-0.2.0-1",
        menhir_ref=image_refs["menhir"],
        menhir_digest=images["menhir"],
        base_ref=image_refs["base"],
        wheel_manifest_sha256=_sha(docker_manifest),
        oauth_wheel_sha256=_sha(oauth_wheel),
    ))

    rendered: dict[str, str] = {}
    policy_payload = json.loads(
        (MODULE_PATH.parent / "client-policy.production.json").read_text(
            encoding="utf-8"
        )
    )
    policy_digest = policy_payload["canonical_digest"]
    for name in MODULE.RENDERED:
        path = tmp_path / name
        if name == "policy_sha256":
            path.write_text(json.dumps(policy_payload), encoding="ascii")
        elif name == "production_env_sha256":
            path.write_text(
                f"MENHIR_CLIENT_POLICY_DIGEST={policy_digest}\n"
                "MENHIR_CANONICAL_SELF_BINDING_MODE=enforce\n",
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
        "deployment_class": "maintenance",
        "ingress_mode": "cloudflared",
        "notes_json_sha256": "d" * 64,
        "notes_markdown_sha256": "e" * 64,
        "repositories": repos,
        "images": images,
        "image_refs": image_refs,
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


def _rebind_metadata_identity(spec: dict) -> None:
    metadata = Path(spec["evidence"]["image_metadata"])
    identity = Path(spec["evidence"]["image_identity"])
    publication = Path(spec["evidence"]["image_publication"])
    identity_value = json.loads(identity.read_text(encoding="ascii"))
    identity_value["metadata"]["sha256"] = _sha(metadata)
    _write_json(identity, identity_value)
    publication_value = json.loads(publication.read_text(encoding="ascii"))
    publication_value["validation_identity_sha256"] = _sha(identity)
    _write_json(publication, publication_value)


def _retarget_publication(spec: dict) -> None:
    version = spec["release_id"].removeprefix("menhir-prod-")
    repository = spec["image_refs"]["menhir"].rpartition("@")[0]
    image_tag = f"{repository}:{version}"
    metadata = Path(spec["evidence"]["image_metadata"])
    metadata_value = json.loads(metadata.read_text(encoding="ascii"))
    metadata_value["image_tag"] = image_tag
    metadata_value["labels"]["version"] = version
    _write_json(metadata, metadata_value)
    identity = Path(spec["evidence"]["image_identity"])
    identity_value = json.loads(identity.read_text(encoding="ascii"))
    identity_value["image_tag"] = image_tag
    _write_json(identity, identity_value)
    publication = Path(spec["evidence"]["image_publication"])
    publication_value = json.loads(publication.read_text(encoding="ascii"))
    publication_value["image_tag"] = image_tag
    _write_json(publication, publication_value)
    _rebind_metadata_identity(spec)


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
    assert release["deployment_class"] == spec["deployment_class"]
    assert release["notes_json_sha256"] == spec["notes_json_sha256"]
    assert release["notes_markdown_sha256"] == spec["notes_markdown_sha256"]
    metadata = json.loads(
        Path(spec["evidence"]["image_metadata"]).read_text(encoding="ascii")
    )
    assert release["image_publication"] == {
        "publication_sha256": _sha(Path(spec["evidence"]["image_publication"])),
        "validation_identity_sha256": _sha(
            Path(spec["evidence"]["image_identity"])
        ),
        "image_id": metadata["image_id"],
        "config_sha256": metadata["config_sha256"],
        "image_archive_sha256": _sha(Path(spec["evidence"]["image_archive"])),
        "registry_digest": spec["images"]["menhir"],
    }
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


def test_author_revalidates_publication_registry_digest(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    publication = Path(spec["evidence"]["image_publication"])
    document = json.loads(publication.read_text(encoding="ascii"))
    document["registry_digest"] = "sha256:" + "f" * 64
    _write_json(publication, document)

    with pytest.raises(ValueError, match="registry digest"):
        MODULE.author_release(spec_path, output, review_request=True)


def test_author_refuses_arbitrary_unbound_sbom(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    Path(spec["evidence"]["sbom"]).write_text(
        '{"arbitrary":"sbom"}\n', encoding="ascii"
    )

    with pytest.raises(ValueError, match="sbom evidence digest"):
        MODULE.author_release(spec_path, output, review_request=True)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("publication_sha256", "bad", "publication_sha256"),
        ("registry_digest", "sha256:" + "f" * 64, "images.menhir"),
    ),
)
def test_release_schema_rejects_invalid_persisted_image_publication(
    tmp_path: Path, field: str, value: str, message: str,
) -> None:
    spec_path, output, _ = _fixture(tmp_path)
    release = _author(spec_path, output)
    release["image_publication"][field] = value
    tampered = tmp_path / f"tampered-{field}.json"
    _write_json(tampered, release)

    with pytest.raises(ValueError, match=message):
        MODULE.menhir_schema.validate_release(str(tampered))


@pytest.mark.parametrize("kind", ["sbom", "vulnerability_scan"])
def test_author_refuses_ci_evidence_for_wrong_subject(
    tmp_path: Path, kind: str,
) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    metadata = Path(spec["evidence"]["image_metadata"])
    document = json.loads(metadata.read_text(encoding="ascii"))
    document["evidence"][kind]["subject"]["image_archive_sha256"] = "f" * 64
    _write_json(metadata, document)
    _rebind_metadata_identity(spec)

    with pytest.raises(ValueError, match="wrong release subject"):
        MODULE.author_release(spec_path, output, review_request=True)


def test_accepts_yawn_env_as_digest_without_copying_secret_file(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    digest = "d" * 64
    spec["rendered"]["yawn_env_sha256"] = "sha256:" + digest
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    release = _author(spec_path, output)

    assert release["rendered"]["yawn_env_sha256"] == digest


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        ("deployment_class", "direct", "deployment_class"),
        ("notes_json_sha256", "not-a-digest", "notes_json_sha256"),
        ("notes_markdown_sha256", "A" * 64, "notes_markdown_sha256"),
    ),
)
def test_refuses_invalid_release_authority_binding(
    tmp_path: Path, key: str, value: str, message: str,
) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    spec[key] = value
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _author(spec_path, output)


def test_legacy_release_remains_readable(tmp_path: Path) -> None:
    spec_path, output, _ = _fixture(tmp_path)
    release = _author(spec_path, output)
    output.chmod(0o600)
    for key in (
        "deployment_class", "ingress_mode", "notes_json_sha256", "notes_markdown_sha256",
        "image_publication",
    ):
        release.pop(key)
    release["security_review"]["authority_sha256"] = (
        MODULE.menhir_schema.release_authority_sha256(release)
    )
    output.write_text(json.dumps(release), encoding="utf-8")

    assert MODULE.menhir_schema.validate_release(str(output))["release_id"] \
        == release["release_id"]


def test_refuses_literal_digest_for_nonsecret_rendered_artifact(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    spec["rendered"]["caddy_sha256"] = "sha256:" + "d" * 64
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError, match="does not permit a literal digest"):
        _author(spec_path, output)


def test_refuses_malformed_yawn_env_literal_digest(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    spec["rendered"]["yawn_env_sha256"] = "sha256:not-a-digest"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(ValueError, match="literal digest is invalid"):
        _author(spec_path, output)


def test_refuses_production_env_bound_to_raw_policy_file_digest(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    policy = Path(spec["rendered"]["policy_sha256"])
    env = Path(spec["rendered"]["production_env_sha256"])
    env.write_text(
        f"MENHIR_CLIENT_POLICY_DIGEST={_sha(policy)}\n"
        "MENHIR_CANONICAL_SELF_BINDING_MODE=enforce\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="client policy canonical_digest"):
        _author(spec_path, output)


def test_refuses_digest_valid_policy_with_product_role_drift(tmp_path: Path) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    policy_path = Path(spec["rendered"]["policy_sha256"])
    env_path = Path(spec["rendered"]["production_env_sha256"])
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    codex_id = (
        "https://memory.ctharvey.me/oauth/client-metadata/agent-smith.json?client=codex"
    )
    policy["clients"][codex_id]["maximum_tier"] = "agent"
    canonical = dict(policy)
    canonical.pop("canonical_digest")
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    policy["canonical_digest"] = digest
    policy_path.write_text(json.dumps(policy), encoding="ascii")
    env_path.write_text(
        f"MENHIR_CLIENT_POLICY_DIGEST={digest}\n"
        "MENHIR_CANONICAL_SELF_BINDING_MODE=enforce\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="codex client tier"):
        _author(spec_path, output)


@pytest.mark.parametrize("mode", [None, "", "typo", "enforce\nenforce"])
def test_refuses_missing_or_invalid_canonical_self_mode(
    tmp_path: Path, mode: str | None,
) -> None:
    spec_path, output, spec = _fixture(tmp_path)
    env_path = Path(spec["rendered"]["production_env_sha256"])
    policy_path = Path(spec["rendered"]["policy_sha256"])
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    lines = [f"MENHIR_CLIENT_POLICY_DIGEST={policy['canonical_digest']}"]
    if mode is not None:
        lines.extend(
            f"MENHIR_CANONICAL_SELF_BINDING_MODE={value}"
            for value in mode.splitlines()
        )
    env_path.write_text("\n".join(lines) + "\n", encoding="ascii")

    with pytest.raises(ValueError, match="MENHIR_CANONICAL_SELF_BINDING_MODE"):
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
        (
            "/etc/yawn-vps/menhir-python-runtime.sha256",
            "python_runtime_digest_sha256",
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
    _retarget_publication(spec)
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
