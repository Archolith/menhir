from __future__ import annotations

import json
from pathlib import Path

import pytest

from deploy.lib.validate_durable_inventory import reconcile_live, validate

ROOT = Path(__file__).resolve().parents[1]


def test_canonical_durable_inventory_matches_production_compose():
    validate(
        ROOT / "deploy" / "durable-state-inventory.json",
        ROOT / "deploy" / "docker-compose.production.yml",
    )


def test_new_unclassified_compose_bind_fails_closed(tmp_path: Path):
    compose = tmp_path / "compose.yml"
    compose.write_text((ROOT / "deploy" / "docker-compose.production.yml").read_text()
                       + "\n# drift\nsource: ${MENHIR_STATE_ROOT:-/srv/menhir/production/state}/new-authority\n")
    with pytest.raises(ValueError, match="persistent bind set changed"):
        validate(ROOT / "deploy" / "durable-state-inventory.json", compose)


def test_missing_authority_fails_closed(tmp_path: Path):
    value = json.loads((ROOT / "deploy" / "durable-state-inventory.json").read_text())
    value["authorities"] = value["authorities"][:-1]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="census differs"):
        validate(inventory, ROOT / "deploy" / "docker-compose.production.yml")


def _live_census() -> dict:
    return {
        "services": {
            "menhir": {"container_id": "menhir-container", "pid": 101},
            "neo4j": {"container_id": "neo4j-container", "pid": 202},
        },
        "mounts": [
            {
                "service": "neo4j",
                "source": "/srv/menhir/production/state/neo4j/data",
                "destination": "/data",
                "rw": True,
            },
            {
                "service": "neo4j",
                "source": "/srv/menhir/production/state/neo4j/logs",
                "destination": "/logs",
                "rw": True,
            },
            {
                "service": "neo4j",
                "source": "/srv/menhir/production/secrets/neo4j/neo4j-auth",
                "destination": "/run/secrets/neo4j_auth",
                "rw": False,
            },
            {
                "service": "menhir",
                "source": "/srv/menhir/production/state/oauth",
                "destination": "/srv/menhir/production/state/oauth",
                "rw": True,
            },
            {
                "service": "menhir",
                "source": "/srv/menhir/production/state/telemetry",
                "destination": "/srv/menhir/state/telemetry",
                "rw": True,
            },
            {
                "service": "menhir",
                "source": "/srv/menhir/production/secrets/menhir",
                "destination": "/run/secrets/menhir",
                "rw": False,
            },
            {
                "service": "menhir",
                "source": "/srv/menhir/production/secrets/oauth",
                "destination": "/run/secrets/oauth",
                "rw": False,
            },
            {
                "service": "menhir",
                "source": "/srv/menhir/production/policy",
                "destination": "/srv/menhir/production/policy",
                "rw": False,
            },
        ],
        "open_files": {
            "menhir": [
                "/srv/menhir/production/state/oauth/oauth.db",
                "/srv/menhir/state/telemetry/mcp_telemetry.db",
            ],
            "neo4j": [
                "/data/databases/neo4j/store_lock",
            ],
        },
    }


def test_live_durable_census_reconciles_complete_writer_set():
    live = _live_census()
    assert reconcile_live(live) is live


def test_live_durable_census_rejects_unknown_writable_mount():
    live = _live_census()
    live["mounts"].append({
        "service": "menhir",
        "source": "/srv/menhir/production/state/unclassified",
        "destination": "/srv/menhir/unclassified",
        "rw": True,
    })
    with pytest.raises(ValueError, match="complete production census"):
        reconcile_live(live)


def test_live_durable_census_allows_idle_writer_with_no_open_database():
    live = _live_census()
    live["open_files"] = {"menhir": [], "neo4j": []}
    assert reconcile_live(live) is live


def test_live_durable_census_rejects_open_file_outside_declared_mounts():
    live = _live_census()
    live["open_files"]["menhir"].append("/srv/menhir/unclassified/authority.db")
    with pytest.raises(ValueError, match="outside declared mounts"):
        reconcile_live(live)


def test_live_durable_census_allows_only_the_declared_ephemeral_access_log():
    live = _live_census()
    live["open_files"]["menhir"].append("/tmp/logs/server.access.log")
    assert reconcile_live(live) is live

    live["open_files"]["menhir"].append("/tmp/logs/unclassified.db")
    with pytest.raises(ValueError, match="outside declared mounts"):
        reconcile_live(live)


def test_live_durable_census_accepts_raw_observations_only_after_classification():
    live = _live_census()
    live["open_files"] = {"menhir": ["/tmp/undeclared-writer.db"], "neo4j": []}
    with pytest.raises(ValueError, match="outside declared mounts"):
        reconcile_live(live)
