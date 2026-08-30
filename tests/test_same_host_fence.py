from __future__ import annotations

import importlib.util
import json
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FENCE_PATH = ROOT / "deploy" / "lib" / "same_host_fence.py"
STAGE_PATH = ROOT / "deploy" / "lib" / "stage_generation.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fence = _load(FENCE_PATH, "same_host_fence")
stage_generation = _load(STAGE_PATH, "stage_generation")


def _container(*, cid="a" * 64, name="menhir-prod-app", image="b" * 64,
               project="menhir-prod", service="menhir", mode="production",
               running=True, mounts=None):
    return {
        "Id": cid,
        "Name": "/" + name,
        "Image": "sha256:" + image,
        "Config": {
            "Image": "ghcr.io/archolith/menhir:test@sha256:" + image,
            "Env": [f"MENHIR_RUNTIME_MODE={mode}", "MENHIR_STARTUP_SCOPE=production"],
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
        },
        "State": {"Running": running},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {"Networks": {"menhir-proxy": {}}},
        "Mounts": mounts or [],
    }


def _intent():
    return {
        "legacy": {
            "app": {
                "container_id": "a" * 64,
                "container_name": "menhir-prod-app",
                "image_id": "sha256:" + "b" * 64,
                "mount_sources": [],
            },
            "database": {
                "container_id": "d" * 64,
                "container_name": "menhir-prod-neo4j",
                "image_id": "sha256:" + "e" * 64,
                "mount_sources": ["/srv/menhir/production/state/neo4j/data"],
            },
        }
    }


def _release():
    return {"images": {"menhir": "sha256:" + "f" * 64,
                       "neo4j": "sha256:" + "9" * 64}}


def _app_mounts(*, candidate=False):
    sources = [
        "/srv/menhir/production/secrets/menhir",
        "/srv/menhir/production/secrets/oauth",
        "/srv/menhir/production/policy",
        "/srv/menhir/production/state/oauth",
        ("/srv/menhir/backups/candidate/generation.Abc123/probe-output/telemetry"
         if candidate else "/srv/menhir/production/state/telemetry"),
    ]
    return [{"Source": value} for value in sources]


def _database_mounts():
    return [{"Source": "/srv/menhir/production/state/neo4j/data"},
            {"Source": "/srv/menhir/production/state/neo4j/logs"}]


def test_writer_census_allows_only_exact_readonly_candidate():
    candidate = _container(
        cid="c" * 64,
        name="menhir-candidate-app",
        image="f" * 64,
        project="menhir-candidate",
        mode="candidate-readonly",
        mounts=_app_mounts(candidate=True),
    )
    candidate_database = _container(
        cid="f" * 64,
        name="menhir-candidate-neo4j",
        image="9" * 64,
        project="menhir-candidate",
        service="neo4j",
        mode="",
        mounts=_database_mounts(),
    )
    fence._validate_census([candidate, candidate_database], _intent(), _release())


def test_writer_census_rejects_candidate_claim_with_unreviewed_image():
    candidate = _container(
        cid="c" * 64, name="menhir-candidate-app", image="7" * 64,
        project="menhir-candidate", mode="candidate-readonly",
        mounts=_app_mounts(candidate=True),
    )
    with pytest.raises(ValueError, match="candidate identity"):
        fence._validate_census([candidate], _intent(), _release())


def test_writer_census_allows_only_exact_reviewed_production_pair():
    app = _container(cid="1" * 64, image="f" * 64, mounts=_app_mounts())
    database = _container(
        cid="2" * 64,
        name="menhir-prod-neo4j",
        image="9" * 64,
        service="neo4j",
        mode="",
        mounts=_database_mounts(),
    )
    fence._validate_census(
        [app, database], _intent(), _release(), allow_production=True
    )


def test_writer_census_rejects_wrong_production_image():
    app = _container(cid="1" * 64, image="8" * 64, mounts=_app_mounts())
    database = _container(
        cid="2" * 64, name="menhir-prod-neo4j", image="9" * 64,
        service="neo4j", mode="", mounts=_database_mounts(),
    )
    with pytest.raises(ValueError, match="unreviewed replacement"):
        fence._validate_census(
            [app, database], _intent(), _release(), allow_production=True
        )


@pytest.mark.parametrize(
    "container",
    [
        _container(running=False),
        _container(cid="e" * 64, name="renamed", project="other"),
        _container(cid="f" * 64, name="renamed", image="f" * 64, project="other"),
    ],
)
def test_writer_census_rejects_legacy_or_competing_writer(container):
    with pytest.raises(ValueError, match="legacy|competing"):
        fence._validate_census([container], _intent(), _release())


def test_writer_census_rejects_malformed_input():
    with pytest.raises(ValueError, match="JSON list"):
        fence._validate_census({}, _intent(), _release())


def test_generation_stager_rejects_links_and_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("generation.Abc/../../escape")
        info.size = 1
        import io
        bundle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe"):
        stage_generation.stage(archive, "generation.Abc", tmp_path / "out")


def test_generation_stager_is_atomic_and_idempotent(tmp_path):
    source = tmp_path / "source"
    generation = source / "generation.Abc123"
    generation.mkdir(parents=True)
    (generation / "MANIFEST.json").write_text(json.dumps({"schema": 1}))
    archive = tmp_path / "good.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(generation, arcname=generation.name)
    root = tmp_path / "decrypted"
    first = stage_generation.stage(archive, generation.name, root)
    second = stage_generation.stage(archive, generation.name, root)
    assert first == second
    assert json.loads((first / "MANIFEST.json").read_text()) == {"schema": 1}
