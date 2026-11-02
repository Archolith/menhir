from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_pipeline", ROOT / "deploy" / "release_pipeline.py"
)
assert SPEC is not None and SPEC.loader is not None
pipeline = importlib.util.module_from_spec(SPEC)
sys.modules["release_pipeline"] = pipeline
SPEC.loader.exec_module(pipeline)

IMAGE = "ghcr.io/archolith/menhir"
MANIFEST = b'{"schemaVersion":2}'
DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
OTHER_DIGEST = "sha256:" + "0" * 64


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


class FakeRegistry:
    def __init__(self, tags: dict[str, str] | None = None, *, stored_manifest: bytes = MANIFEST,
                 put_status: int = 201, lookup_status: int | None = None) -> None:
        self.tags = dict(tags or {})
        self.stored_manifest = stored_manifest
        self.put_status = put_status
        self.lookup_status = lookup_status
        self.puts: list[tuple[str, bytes, str]] = []

    def __call__(self, method, url, headers, body):
        if "/token?" in url:
            return 200, {}, json.dumps({"token": "t"}).encode()
        reference = url.rsplit("/", 1)[1]
        if method == "PUT":
            if self.put_status in {200, 201}:
                self.puts.append((reference, body, headers["Content-Type"]))
                self.tags[reference] = "sha256:" + hashlib.sha256(body).hexdigest()
            return self.put_status, {}, b""
        if method == "HEAD":
            if self.lookup_status is not None:
                return self.lookup_status, {}, b""
            if reference not in self.tags:
                return 404, {}, b""
            return 200, {"docker-content-digest": self.tags[reference]}, b""
        assert method == "GET" and reference == DIGEST
        return 200, {"content-type": "application/vnd.oci.image.manifest.v1+json"}, self.stored_manifest


class FakePyPI:
    def __init__(self, files: dict[str, bytes] | None, *, reported: dict[str, str] | None = None,
                 host: str = "files.pythonhosted.org", status: int | None = None) -> None:
        self.files = files
        self.reported = reported or {}
        self.host = host
        self.status = status

    def __call__(self, method, url, headers, body):
        if url.endswith("/json"):
            if self.status is not None:
                return self.status, {}, b""
            if self.files is None:
                return 404, {}, b""
            urls = [
                {
                    "filename": name,
                    "url": f"https://{self.host}/x/{name}",
                    "digests": {
                        "sha256": self.reported.get(name, hashlib.sha256(content).hexdigest()),
                    },
                }
                for name, content in self.files.items()
            ]
            return 200, {}, json.dumps({"urls": urls}).encode()
        return 200, {}, self.files[url.rsplit("/", 1)[1]]


def _dist(tmp_path: Path, *names: str) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in names:
        (dist / name).write_bytes(b"rebuilt")
    return dist


# ---- release inputs ---------------------------------------------------------


@pytest.mark.parametrize("tag", ["v0.3", "0.3.2", "v0.3.2rc1", "v0.3.2-1", "v0.3.2\n", "vlatest"])
def test_only_a_plain_release_tag_is_accepted(tag: str) -> None:
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.parse_release_tag(tag)


def test_tag_yields_the_package_version() -> None:
    assert pipeline.parse_release_tag("v0.3.2") == "0.3.2"


def test_a_retry_dispatched_from_a_branch_is_refused() -> None:
    # Counterexample: the attestation would name main's commit while the image is the tag's.
    with pytest.raises(pipeline.ReleasePipelineError, match="--ref v0.3.3"):
        pipeline.require_run_on_tag("refs/heads/main", "v0.3.3")
    pipeline.require_run_on_tag("refs/tags/v0.3.3", "v0.3.3")


def test_committed_python_base_is_a_single_digest_pin() -> None:
    base = pipeline.read_python_base(ROOT / "deploy" / "release-python-base.txt")
    assert base.startswith("python:3.12-slim@sha256:")


@pytest.mark.parametrize("content", [
    "python:3.12-slim\n",
    "",
    "python:3.12-slim@sha256:" + "a" * 64 + "\npython:3.13-slim@sha256:" + "b" * 64 + "\n",
])
def test_unpinned_or_ambiguous_python_base_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "base.txt"
    path.write_text(content, encoding="ascii")
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.read_python_base(path)


def test_plan_outputs_derived_inputs_and_finds_an_existing_tag(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = pipeline.main(
        ["plan", "--tag", "v0.3.3", "--github-ref", "refs/tags/v0.3.3", "--image", IMAGE,
         "--python-base-file", str(ROOT / "deploy" / "release-python-base.txt")],
        FakeRegistry({"0.3.3": DIGEST}),
    )
    assert code == 0
    values = dict(line.split("=", 1) for line in output.read_text(encoding="ascii").splitlines())
    assert values["version"] == "0.3.3"
    assert values["image_version"] == "0.3.3-1"
    assert values["existing_image_digest"] == DIGEST


def test_plan_stops_when_the_registry_answer_is_not_clear(tmp_path: Path, monkeypatch) -> None:
    # Counterexample: treating an outage as "no tag yet" would start a second image build.
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = pipeline.main(
        ["plan", "--tag", "v0.3.3", "--github-ref", "refs/tags/v0.3.3", "--image", IMAGE,
         "--python-base-file", str(ROOT / "deploy" / "release-python-base.txt")],
        FakeRegistry(lookup_status=503),
    )
    assert code == 1
    assert not output.exists()


# ---- image tag --------------------------------------------------------------


def test_tag_stores_the_exact_manifest_bytes() -> None:
    registry = FakeRegistry()
    assert pipeline.tag_image(registry, IMAGE, "0.3.3", DIGEST, "a:b") == "tagged"
    assert registry.puts == [("0.3.3", MANIFEST, "application/vnd.oci.image.manifest.v1+json")]
    assert registry.tags["0.3.3"] == DIGEST


def test_existing_tag_is_never_moved() -> None:
    registry = FakeRegistry({"0.3.3": OTHER_DIGEST})
    with pytest.raises(pipeline.ReleasePipelineError, match="refusing to move"):
        pipeline.tag_image(registry, IMAGE, "0.3.3", DIGEST, "a:b")
    assert registry.puts == []
    assert registry.tags["0.3.3"] == OTHER_DIGEST


def test_rerun_after_the_tag_was_written_is_a_no_op() -> None:
    registry = FakeRegistry({"0.3.3": DIGEST})
    assert pipeline.tag_image(registry, IMAGE, "0.3.3", DIGEST, "a:b") == "already tagged"
    assert registry.puts == []


def test_manifest_that_does_not_hash_to_the_digest_is_not_tagged() -> None:
    registry = FakeRegistry(stored_manifest=b"tampered")
    with pytest.raises(pipeline.ReleasePipelineError, match="do not hash"):
        pipeline.tag_image(registry, IMAGE, "0.3.3", DIGEST, "a:b")
    assert registry.puts == []


def test_failed_write_stops_the_release() -> None:
    with pytest.raises(pipeline.ReleasePipelineError, match="HTTP 403"):
        pipeline.tag_image(FakeRegistry(put_status=403), IMAGE, "0.3.3", DIGEST, "a:b")


@pytest.mark.parametrize("tag, digest", [
    ("0.3.3", ""),
    ("0.3.3", "sha256:short"),
    ("latest", DIGEST),
    ("candidate-x", DIGEST),
])
def test_empty_digest_or_other_tag_name_is_refused(tag: str, digest: str) -> None:
    # An empty digest is what a skipped publish job would pass along.
    registry = FakeRegistry()
    with pytest.raises(pipeline.ReleasePipelineError):
        pipeline.tag_image(registry, IMAGE, tag, digest, "a:b")
    assert registry.puts == []


# ---- PyPI state -------------------------------------------------------------

WHEEL = "archolith_menhir-0.3.3-py3-none-any.whl"
SDIST = "archolith_menhir-0.3.3.tar.gz"


def test_unpublished_version_keeps_the_build(tmp_path: Path) -> None:
    dist = _dist(tmp_path, WHEEL, SDIST)
    assert pipeline.adopt_published_distributions(FakePyPI(None), "0.3.3", dist) is False
    assert (dist / WHEEL).read_bytes() == b"rebuilt"


def test_retry_uses_the_files_pypi_serves_not_the_rebuild(tmp_path: Path) -> None:
    dist = _dist(tmp_path, WHEEL, SDIST)
    pypi = FakePyPI({WHEEL: b"published wheel", SDIST: b"published sdist"})
    assert pipeline.adopt_published_distributions(pypi, "0.3.3", dist) is True
    assert (dist / WHEEL).read_bytes() == b"published wheel"
    assert (dist / SDIST).read_bytes() == b"published sdist"


def test_partial_upload_stops_the_release(tmp_path: Path) -> None:
    # Counterexample: an upload that died after the wheel must not be called finished.
    dist = _dist(tmp_path, WHEEL, SDIST)
    with pytest.raises(pipeline.ReleasePipelineError, match="different file set"):
        pipeline.adopt_published_distributions(FakePyPI({WHEEL: b"published"}), "0.3.3", dist)
    assert (dist / WHEEL).read_bytes() == b"rebuilt"


def test_file_not_matching_the_reported_digest_replaces_nothing(tmp_path: Path) -> None:
    dist = _dist(tmp_path, WHEEL, SDIST)
    pypi = FakePyPI({WHEEL: b"ok", SDIST: b"ok"}, reported={SDIST: "0" * 64})
    with pytest.raises(pipeline.ReleasePipelineError, match="does not match"):
        pipeline.adopt_published_distributions(pypi, "0.3.3", dist)
    assert (dist / WHEEL).read_bytes() == b"rebuilt"
    assert (dist / SDIST).read_bytes() == b"rebuilt"


def test_download_from_an_unexpected_host_is_refused(tmp_path: Path) -> None:
    dist = _dist(tmp_path, WHEEL)
    with pytest.raises(pipeline.ReleasePipelineError, match="unexpected download"):
        pipeline.adopt_published_distributions(
            FakePyPI({WHEEL: b"x"}, host="example.com"), "0.3.3", dist,
        )


def test_pypi_outage_is_not_read_as_unpublished(tmp_path: Path) -> None:
    dist = _dist(tmp_path, WHEEL)
    with pytest.raises(pipeline.ReleasePipelineError, match="HTTP 503"):
        pipeline.adopt_published_distributions(FakePyPI(None, status=503), "0.3.3", dist)


# ---- workflow wiring --------------------------------------------------------


def test_nothing_publishes_before_the_test_gate_and_resolve() -> None:
    jobs = _workflow("publish-pypi.yml")["jobs"]
    assert set(jobs["build"]["needs"]) == {"resolve", "test-gate"}
    assert jobs["publish"]["needs"] == "build"
    assert "needs" not in jobs["test-gate"]


def test_stages_run_in_release_order() -> None:
    jobs = _workflow("publish-pypi.yml")["jobs"]
    assert "publish" in jobs["image"]["needs"]
    assert "image" in jobs["image-tag"]["needs"]
    assert {"publish", "image-tag"} <= set(jobs["github-release"]["needs"])


def test_publish_runs_only_on_an_explicit_not_yet_published_answer() -> None:
    condition = _workflow("publish-pypi.yml")["jobs"]["publish"]["if"]
    assert "already_published == 'false'" in condition


@pytest.mark.parametrize("job", ["image", "github-release"])
def test_later_stages_require_build_and_pypi_to_have_succeeded(job: str) -> None:
    # These jobs use !cancelled(), which would otherwise also run them after a failure.
    condition = _workflow("publish-pypi.yml")["jobs"][job]["if"]
    assert "!cancelled()" in condition
    assert "needs.build.result == 'success'" in condition
    assert "needs.publish.result == 'success'" in condition
    assert "already_published == 'true'" in condition
    assert "needs.publish.result == 'skipped'" not in condition


def test_github_release_waits_for_the_image_tag() -> None:
    condition = _workflow("publish-pypi.yml")["jobs"]["github-release"]["if"]
    assert "needs.image-tag.result == 'success'" in condition
    assert "existing_image_digest != ''" in condition


def test_image_call_grants_every_permission_the_called_job_requests() -> None:
    # A called job asking for more than the caller grants fails the whole run at startup.
    caller = _workflow("publish-pypi.yml")["jobs"]["image"]
    called = _workflow("release-image.yml")
    assert caller["uses"] == "./.github/workflows/release-image.yml"
    for job in called["jobs"].values():
        assert job["permissions"].items() <= caller["permissions"].items()


def test_image_call_passes_only_declared_inputs_and_all_required_ones() -> None:
    caller = _workflow("publish-pypi.yml")["jobs"]["image"]["with"]
    # PyYAML reads the bare `on` key as True.
    declared = _workflow("release-image.yml")[True]["workflow_call"]["inputs"]
    assert set(caller) <= set(declared)
    assert {name for name, spec in declared.items() if spec["required"]} <= set(caller)
    assert caller["push"] is True


def test_image_is_verified_with_the_upgrade_identity_before_it_is_tagged() -> None:
    steps = _workflow("publish-pypi.yml")["jobs"]["image-tag"]["steps"]
    runs = [step.get("run", "") for step in steps]
    verify = next(i for i, run in enumerate(runs) if "gh attestation verify" in run)
    tag = next(i for i, run in enumerate(runs) if "tag-image" in run)
    assert verify < tag
    assert "--signer-workflow Archolith/menhir/.github/workflows/release-image.yml" in runs[verify]
    assert "--repo Archolith/menhir" in runs[verify]


def test_image_publication_stays_in_the_release_environment() -> None:
    jobs = _workflow("publish-pypi.yml")["jobs"]
    assert jobs["image-tag"]["environment"] == "menhir-release-publication"
    assert jobs["publish"]["environment"]["name"] == "pypi"


def test_trusted_publisher_binding_is_kept() -> None:
    # PyPI's publisher names this file and refuses reusable workflows.
    triggers = _workflow("publish-pypi.yml")[True]
    assert "workflow_call" not in triggers
    assert triggers["push"]["tags"] == ["v[0-9]*"]
