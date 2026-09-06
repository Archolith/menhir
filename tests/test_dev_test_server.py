"""Unit coverage for the isolated shaped development server launcher."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from scripts.dev import test_server
from scripts.dev.test_server import (
    DEAD_JWKS_URI,
    TEST_KEYS,
    RunningServer,
    _shape_env,
    _container_command,
    _throwaway_neo4j,
    launch,
    resolve_container_image,
)


def _oauth_as_env(tmp_path, **overrides):
    return _shape_env(
        "oauth-as",
        port=8123,
        host="127.0.0.1",
        workdir=tmp_path,
        jwks_uri=DEAD_JWKS_URI,
        backend="none",
        instance_id="test-instance",
        **overrides,
    )


def test_oauth_as_remote_profile_uses_exact_public_origin(tmp_path) -> None:
    env = _oauth_as_env(
        tmp_path,
        public_base_url="https://temporary-tunnel.example",
        oauth_refresh=True,
        oauth_access_ttl_s=120,
    )

    assert env["MENHIR_PUBLIC_BASE_URL"] == "https://temporary-tunnel.example"
    assert env["MENHIR_OAUTH_AS_REFRESH_TOKENS_ENABLED"] == "1"
    assert env["MENHIR_OAUTH_AS_REFRESH_WITHOUT_OFFLINE_ACCESS_ENABLED"] == "1"
    assert env["MENHIR_OAUTH_AS_ACCESS_TTL_S"] == "120"
    assert env["MENHIR_OPERATOR_KEY"] == TEST_KEYS["operator"]


def test_oauth_as_remote_profile_canonicalizes_trailing_slash(tmp_path) -> None:
    env = _oauth_as_env(
        tmp_path,
        public_base_url="https://temporary-tunnel.example/",
    )

    assert env["MENHIR_PUBLIC_BASE_URL"] == "https://temporary-tunnel.example"


@pytest.mark.parametrize(
    "value",
    [
        "http://public.example",
        "https://user:secret@public.example",
        "https://public.example/prefix",
        "https://public.example?query=yes",
        "https://public.example#fragment",
        "not-a-url",
    ],
)
def test_oauth_as_remote_profile_rejects_non_origin_urls(tmp_path, value: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        _oauth_as_env(tmp_path, public_base_url=value)


def test_oauth_as_local_defaults_remain_refresh_disabled(tmp_path) -> None:
    env = _oauth_as_env(tmp_path)

    assert env["MENHIR_PUBLIC_BASE_URL"] == "http://127.0.0.1:8123"
    assert "MENHIR_OAUTH_AS_REFRESH_TOKENS_ENABLED" not in env
    assert "MENHIR_OAUTH_AS_REFRESH_WITHOUT_OFFLINE_ACCESS_ENABLED" not in env
    assert "MENHIR_OAUTH_AS_ACCESS_TTL_S" not in env
    assert env["MENHIR_OPERATOR_KEY"] == TEST_KEYS["operator"]


def test_shaped_env_does_not_inherit_real_service_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_OPERATOR_KEY", "real-operator-secret")
    monkeypatch.setenv("NEO4J_URI", "bolt://real.example:7687")
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-secret")

    env = _oauth_as_env(tmp_path)

    assert env["MENHIR_OPERATOR_KEY"] == TEST_KEYS["operator"]
    assert env["NEO4J_URI"] == "bolt://127.0.0.1:7699"
    assert "OPENAI_API_KEY" not in env


def test_public_profile_refuses_external_test_neo4j(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_TEST_NEO4J_URI", "bolt://shared-test.example:7687")

    with pytest.raises(RuntimeError, match="refuse.*MENHIR_TEST_NEO4J_URI"):
        with _throwaway_neo4j(allow_external=False):
            pass


def test_container_command_keeps_secrets_out_of_argv(tmp_path) -> None:
    env = _shape_env(
        "static", port=8123, host="127.0.0.1", workdir=tmp_path,
        jwks_uri=DEAD_JWKS_URI, backend="neo4j", instance_id="test-instance",
        neo4j=("bolt://127.0.0.1:17687", "neo4j", "throwaway-password"),
    )
    env.update({
        "OPENAI_API_KEY": "openai-secret-value",
        "LOCAL_LLM_API_KEY": "openrouter-secret-value",
    })

    command, process_env = _container_command(
        image="menhir:test-image", name="menhir-test-app", port=18123,
        workdir=tmp_path, env=env,
    )

    rendered = " ".join(command)
    assert "openai-secret-value" not in rendered
    assert "openrouter-secret-value" not in rendered
    assert "OPENAI_API_KEY" not in process_env
    assert "LOCAL_LLM_API_KEY" not in process_env
    assert "NEO4J_PASSWORD" not in process_env
    assert "-e OPENAI_API_KEY" not in rendered
    assert "-e LOCAL_LLM_API_KEY" not in rendered
    assert "-e NEO4J_PASSWORD" not in rendered
    assert "bolt://host.docker.internal:17687" == process_env["NEO4J_URI"]
    assert "--read-only" in command
    assert "--pull=never" in command
    assert "--no-healthcheck" in command
    assert command[-1] == "menhir:test-image"


def test_container_command_refuses_non_loopback_neo4j(tmp_path) -> None:
    env = _shape_env(
        "static", port=8123, host="127.0.0.1", workdir=tmp_path,
        jwks_uri=DEAD_JWKS_URI, backend="neo4j", instance_id="test-instance",
        neo4j=("bolt://production.example:7687", "neo4j", "not-used"),
    )

    with pytest.raises(ValueError, match="loopback disposable Neo4j"):
        _container_command(
            image="menhir:test-image", name="menhir-test-app", port=18123,
            workdir=tmp_path, env=env,
        )


def test_resolve_container_image_pins_id_and_revision(monkeypatch) -> None:
    result = test_server.subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps([{
            "Id": "sha256:abc123",
            "Config": {"Labels": {"org.opencontainers.image.revision": "commit123"}},
        }]),
        stderr="",
    )
    monkeypatch.setattr(test_server.subprocess, "run", lambda *args, **kwargs: result)

    assert resolve_container_image(
        "menhir:candidate", expected_revision="commit123"
    ) == ("sha256:abc123", "commit123")


def test_resolve_container_image_refuses_wrong_revision(monkeypatch) -> None:
    result = test_server.subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps([{
            "Id": "sha256:abc123",
            "Config": {"Labels": {"org.opencontainers.image.revision": "wrong"}},
        }]),
        stderr="",
    )
    monkeypatch.setattr(test_server.subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(RuntimeError, match="revision mismatch"):
        resolve_container_image("menhir:candidate", expected_revision="commit123")


def test_invalid_explicit_interpreter_cleans_temporary_workdir(
    tmp_path, monkeypatch
) -> None:
    workdir = tmp_path / "launcher-workdir"

    def _mkdtemp(*, prefix: str) -> str:
        assert prefix.startswith("menhir-test-")
        workdir.mkdir()
        return str(workdir)

    monkeypatch.setattr(test_server.tempfile, "mkdtemp", _mkdtemp)

    with pytest.raises(FileNotFoundError, match="requested Python interpreter"):
        with launch(
            "oauth-as",
            port=8124,
            python_executable=str(tmp_path / "missing-python.exe"),
        ):
            pass

    assert not workdir.exists()


def test_cleanup_failure_is_not_silenced(tmp_path, monkeypatch) -> None:
    workdir = tmp_path / "launcher-workdir"

    def _mkdtemp(*, prefix: str) -> str:
        workdir.mkdir()
        return str(workdir)

    def _failed_cleanup(path: Path) -> None:
        raise PermissionError(f"locked test artifact: {path}")

    monkeypatch.setattr(test_server.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(test_server, "_remove_workdir", _failed_cleanup)

    with pytest.raises(PermissionError, match="locked test artifact"):
        with launch(
            "oauth-as",
            port=8124,
            python_executable=str(tmp_path / "missing-python.exe"),
        ):
            pass


def test_restart_does_not_spawn_when_old_process_cannot_exit(tmp_path, monkeypatch) -> None:
    spawned = []
    log_path = tmp_path / "server.log"

    def _failed_terminate(proc) -> None:
        raise RuntimeError("old process is still running")

    monkeypatch.setattr(test_server, "_terminate", _failed_terminate)
    monkeypatch.setattr(
        test_server.subprocess,
        "Popen",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    with log_path.open("w", encoding="utf-8") as log_file:
        server = RunningServer(
            shape="oauth-as",
            port=8123,
            base_url="http://127.0.0.1:8123",
            proc=object(),  # type: ignore[arg-type]
            workdir=Path(tmp_path),
            instance_id="same-instance",
            command=("python", "-m", "menhir.cli", "serve"),
            env={},
            log_file=log_file,
        )

        with pytest.raises(RuntimeError, match="old process is still running"):
            server.restart()

    assert spawned == []


def test_terminate_raises_when_process_survives_kill() -> None:
    class StubbornProcess:
        pid = 4242

        @staticmethod
        def poll():
            return None

        @staticmethod
        def send_signal(_signal):
            return None

        @staticmethod
        def wait(*, timeout):
            raise test_server.subprocess.TimeoutExpired("stubborn", timeout)

        @staticmethod
        def kill():
            return None

    with pytest.raises(RuntimeError, match="did not exit"):
        test_server._terminate(StubbornProcess())  # type: ignore[arg-type]


def test_restart_preserves_command_environment_and_instance(tmp_path, monkeypatch) -> None:
    old_proc = object()
    new_proc = object()
    terminated = []
    spawned = []
    health_calls = []
    log_path = tmp_path / "server.log"

    monkeypatch.setattr(test_server, "_terminate", terminated.append)

    def _popen(command, **kwargs):
        spawned.append((command, kwargs))
        return new_proc

    monkeypatch.setattr(test_server.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        test_server,
        "_wait_for_health",
        lambda base, timeout, proc, *, expect_instance_id: health_calls.append(
            (base, timeout, proc, expect_instance_id)
        )
        or {"status": "ok"},
    )

    with log_path.open("w", encoding="utf-8") as log_file:
        server = RunningServer(
            shape="oauth-as",
            port=8123,
            base_url="http://127.0.0.1:8123",
            proc=old_proc,  # type: ignore[arg-type]
            workdir=Path(tmp_path),
            instance_id="same-instance",
            command=("python", "-m", "menhir.cli", "serve"),
            env={"MENHIR_OAUTH_AS_DIR": str(tmp_path / "oauth-store")},
            log_file=log_file,
            health_timeout_s=45.0,
        )

        assert server.restart() == {"status": "ok"}

    assert terminated == [old_proc]
    assert server.proc is new_proc
    assert spawned[0][0] == ["python", "-m", "menhir.cli", "serve"]
    assert spawned[0][1]["env"] == server.env
    assert health_calls == [
        ("http://127.0.0.1:8123", 45.0, new_proc, "same-instance")
    ]



def test_container_confirmation_fixtures_are_live_narrow_readonly_mounts(tmp_path) -> None:
    fixtures = tmp_path / "owner fixtures"
    fixtures.mkdir()
    public_key = fixtures / "owner-public.pem"
    public_key.write_text("test public key", encoding="utf-8")
    private_key = fixtures / "owner-private.pem"
    private_key.write_text("private-key-sentinel", encoding="utf-8")
    confirmations = fixtures / "confirmations"
    confirmations.mkdir()
    workdir = tmp_path / "launcher"
    workdir.mkdir()
    env = _shape_env(
        "static", port=8123, host="127.0.0.1", workdir=workdir,
        jwks_uri=DEAD_JWKS_URI, backend="neo4j", instance_id="test-instance",
        neo4j=("bolt://127.0.0.1:17687", "neo4j", "throwaway-password"),
    )
    env.update({
        "MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_PATH": str(public_key),
        "MENHIR_CANONICAL_SELF_CONFIRMATION_DIRECTORY": str(confirmations),
        "MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_SHA256": "a" * 64,
    })
    original_env = dict(env)
    command, process_env = _container_command(
        image="menhir:test-image", name="menhir-test-app", port=18123,
        workdir=workdir, env=env,
    )

    mounts = [command[index + 1] for index, part in enumerate(command) if part == "-v"]
    key_target = "/run/menhir-self-authority/owner-public.pem"
    directory_target = "/run/menhir-self-authority/confirmations"
    assert f"{public_key.resolve()}:{key_target}:ro" in mounts
    assert f"{confirmations.resolve()}:{directory_target}:ro" in mounts
    assert process_env["MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_PATH"] == key_target
    assert process_env["MENHIR_CANONICAL_SELF_CONFIRMATION_DIRECTORY"] == directory_target
    assert process_env["MENHIR_CANONICAL_SELF_CONFIRMATION_PUBLIC_KEY_SHA256"] == "a" * 64
    assert env == original_env
    assert not any(str(private_key) in mount or mount.startswith(f"{fixtures}:") for mount in mounts)
    assert "private-key-sentinel" not in " ".join(command)

    # Docker execution is a separate opt-in test. Pin that the mount uses the live directory,
    # not a startup snapshot, for creation, atomic replacement, and revocation by the host.
    mounted = next(mount for mount in mounts if mount.endswith(f":{directory_target}:ro"))
    mounted_source = Path(mounted.removesuffix(f":{directory_target}:ro"))
    confirmation = confirmations / "episode.json"
    confirmation.write_text("confirmed", encoding="utf-8")
    assert (mounted_source / confirmation.name).read_text(encoding="utf-8") == "confirmed"
    replacement = confirmations / "replacement.tmp"
    replacement.write_text("replaced", encoding="utf-8")
    replacement.replace(confirmation)
    assert (mounted_source / confirmation.name).read_text(encoding="utf-8") == "replaced"
    confirmation.unlink()
    assert not (mounted_source / confirmation.name).exists()


@pytest.mark.parametrize(
    "setting,kind",
    [
        ("PUBLIC_KEY_PATH", "missing"), ("PUBLIC_KEY_PATH", "directory"),
        ("DIRECTORY", "missing"), ("DIRECTORY", "file"),
    ],
)
def test_container_confirmation_mounts_reject_invalid_paths(tmp_path, setting, kind) -> None:
    source = tmp_path / "bad-fixture"
    if kind == "directory":
        source.mkdir()
    elif kind == "file":
        source.write_text("not a directory", encoding="utf-8")
    (tmp_path / "launcher").mkdir()
    env = _shape_env(
        "static", port=8123, host="127.0.0.1", workdir=tmp_path / "launcher",
        jwks_uri=DEAD_JWKS_URI, backend="neo4j", instance_id="test-instance",
        neo4j=("bolt://127.0.0.1:17687", "neo4j", "throwaway-password"),
    )
    env[f"MENHIR_CANONICAL_SELF_CONFIRMATION_{setting}"] = str(source)

    with pytest.raises(ValueError, match=f"MENHIR_CANONICAL_SELF_CONFIRMATION_{setting}"):
        _container_command(
            image="menhir:test-image", name="menhir-test-app", port=18123,
            workdir=tmp_path / "launcher", env=env,
        )
