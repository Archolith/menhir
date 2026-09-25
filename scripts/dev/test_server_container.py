"""Production-image container launch helpers for the throwaway test-server launcher.

Builds an isolated ``docker run`` invocation for a Menhir release image without
secrets in argv or the container environment, plus image-identity resolution and
container teardown.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .test_server_util import REPO_ROOT, TEST_KEYS, _terminate


def _container_secret_mounts(workdir: Path, env: dict[str, str]) -> list[tuple[Path, str]]:
    """Materialize throwaway files required by the production image entrypoint."""
    menhir_dir = workdir / "container-secrets" / "menhir"
    oauth_dir = workdir / "container-secrets" / "oauth"
    policy_dir = workdir / "container-policy"
    menhir_dir.mkdir(parents=True)
    oauth_dir.mkdir(parents=True)
    policy_dir.mkdir(parents=True)

    secret_values = {
        "neo4j-password": env["NEO4J_PASSWORD"],
        "operator-key": env.get("MENHIR_OPERATOR_KEY", TEST_KEYS["operator"]),
        "agent-key": env.get("MENHIR_AGENT_KEY", TEST_KEYS["agent"]),
        "readonly-key": env.get("MENHIR_READONLY_KEY", TEST_KEYS["readonly"]),
        "openai-api-key": env.get("OPENAI_API_KEY", ""),
        "local-llm-api-key": env.get("LOCAL_LLM_API_KEY", ""),
    }
    for name, value in secret_values.items():
        if value:
            path = menhir_dir / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)

    consent = oauth_dir / "oauth-consent-secret"
    consent.write_text("throwaway-container-consent", encoding="utf-8")
    consent.chmod(0o600)
    signing_key = oauth_dir / "oauth_signing_key.json"
    signing_key.write_text("{}", encoding="utf-8")
    signing_key.chmod(0o600)
    (policy_dir / "client-policy.json").write_bytes(
        (REPO_ROOT / "deploy" / "client-policy.production.json").read_bytes()
    )
    return [
        (menhir_dir, "/run/secrets/menhir"),
        (oauth_dir, "/run/secrets/oauth"),
        (policy_dir, "/srv/menhir/production/policy"),
    ]


def _container_command(
    *, image: str, name: str, port: int, workdir: Path, env: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """Build an isolated production-image invocation without secrets in argv."""
    if not image.strip():
        raise ValueError("container image must be non-empty")
    neo4j = urlsplit(env["NEO4J_URI"])
    if neo4j.hostname not in {"127.0.0.1", "localhost"} or neo4j.port is None:
        raise ValueError("container-image tests require a loopback disposable Neo4j")

    mounts = _container_secret_mounts(workdir, env)
    prefixes = (
        "ENV_FILE", "GRAPHITI_", "LLM_", "LOCAL_LLM_", "MENHIR_",
        "NEO4J_", "OPENAI_", "SCHEDULER_", "GEMINI_",
    )
    secret_env_keys = {
        "GEMINI_API_KEY", "LOCAL_LLM_API_KEY", "MENHIR_AGENT_KEY",
        "MENHIR_API_KEY", "MENHIR_OPERATOR_KEY", "MENHIR_READONLY_KEY",
        "NEO4J_PASSWORD", "OPENAI_API_KEY",
    }
    container_env = {
        key: value for key, value in env.items()
        if key.startswith(prefixes) and key not in secret_env_keys
    }
    container_env.update({
        "ENV_FILE": "/tmp/empty.env",
        "MENHIR_API_HOST": "0.0.0.0",
        "MENHIR_API_PORT": "8099",
        "MENHIR_OAUTH_AS_DIR": "/tmp/menhir-oauth",
        "MENHIR_MCP_TELEMETRY_DB": "/tmp/menhir-telemetry.db",
        "NEO4J_URI": f"bolt://host.docker.internal:{neo4j.port}",
        "WORKSPACE_ROOT": "/tmp/menhir-workspace",
    })
    docker_env = os.environ.copy()
    for key in secret_env_keys:
        docker_env.pop(key, None)
    docker_env.update(container_env)
    command = [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
        "--no-healthcheck",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--add-host", "host.docker.internal:host-gateway",
        "-p", f"127.0.0.1:{port}:8099",
    ]
    for source, target in mounts:
        command.extend(["-v", f"{source}:{target}:ro"])
    for key in sorted(container_env):
        command.extend(["-e", key])
    command.append(image)
    return command, docker_env


def resolve_container_image(image: str, *, expected_revision: str) -> tuple[str, str]:
    """Resolve a tag once to an immutable image ID and verify its revision label."""
    inspected = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True,
    )
    if inspected.returncode != 0:
        raise RuntimeError(f"cannot inspect release image {image!r}: {inspected.stderr.strip()}")
    try:
        records = json.loads(inspected.stdout)
        record = records[0]
        image_id = str(record["Id"])
        revision = str(record["Config"]["Labels"]["org.opencontainers.image.revision"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"release image {image!r} has invalid identity metadata") from exc
    if not image_id.startswith("sha256:"):
        raise RuntimeError(f"release image {image!r} did not resolve to an immutable ID")
    if revision != expected_revision:
        raise RuntimeError(
            f"release image revision mismatch: expected {expected_revision}, got {revision}"
        )
    return image_id, revision


def _remove_container(name: str, proc: subprocess.Popen) -> None:
    """Remove one exact throwaway container and reap its docker client."""
    removed = None
    for _attempt in range(2):
        removed = subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30,
        )
        if removed.returncode == 0:
            break
        exists = subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True, text=True, timeout=30,
        )
        if exists.returncode != 0:
            break
    _terminate(proc)
    if removed is not None and removed.returncode != 0:
        exists = subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True, text=True, timeout=30,
        )
        if exists.returncode == 0:
            raise RuntimeError(
                f"throwaway container {name!r} survived removal: {removed.stderr.strip()}"
            )
