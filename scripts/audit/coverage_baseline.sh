#!/usr/bin/env bash
# Regenerate the coverage baseline used by A6 (test-coverage) audits.
#
# WHY THIS EXISTS
#
# The A6 audit type was blocked for weeks because coverage.xml was 34 days older
# than HEAD. Auditing against a stale snapshot attributes coverage to code that
# has since changed, so freshly-written untested logic reports as covered -- a
# false clean, which is worse than no data at all.
#
# It stayed blocked because regenerating it was a sequence somebody had to
# remember: bring up a throwaway Neo4j, prove it is actually reachable, install
# coverage tooling that is not in the project dependencies, then run the suite
# with the right markers and environment. This script is that sequence.
#
# WHAT IT PRODUCES
#
# coverage.xml and .coverage at the repo root, covering BOTH the offline suite
# and the graph-backed online suite in ONE run. CI cannot do this -- it splits
# them across two jobs, so each produces only partial data and neither sees the
# combined picture. Both artifacts are gitignored; they are audit inputs, not
# source.
#
# SAFETY
#
# Online tests run unscoped destructive queries -- apply_decay rewrites sharpness
# and edge counts across every node. They MUST NOT touch a real graph. This runs
# them against docker-compose.test.yml: separate ports (7688, not 7687) and tmpfs
# storage, so it is ephemeral by construction. The suite's own
# force_all_tests_onto_test_neo4j fixture is the second line of defence; see the
# 2026-07-12 incident referenced in tests/conftest.py.
#
# LLM-dependent tests are deselected (-m "not needs_llm"). They need llama.cpp on
# :8081 and :8083; without it they either skip cleanly or hang in the
# chat-completion retry path until pytest-timeout kills the run.
#
# USAGE
#
#   bash scripts/audit/coverage_baseline.sh          # up, verify, run, report
#   bash scripts/audit/coverage_baseline.sh up       # just start the test Neo4j
#   bash scripts/audit/coverage_baseline.sh status   # is it reachable?
#   bash scripts/audit/coverage_baseline.sh down     # stop and delete it
#
# Then analyse the result with the auditprobe a6 plugin:
#   auditprobe src/menhir/<module> --type a6 --coverage coverage.xml

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="docker-compose.test.yml"
CONTAINER="menhir-neo4j-test"
TEST_URI="bolt://localhost:7688"
TEST_USER="neo4j"
TEST_PASSWORD="testpassword"

if [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"          # Windows
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"                  # POSIX
else
    echo "ERROR: no project venv found at .venv/. Create it before running." >&2
    exit 1
fi

say() { printf '\n=== %s\n' "$*"; }

# --------------------------------------------------------------------------
# Connectivity proof.
#
# A missing test Neo4j makes the online tests SKIP rather than FAIL. That means
# an unreachable database produces a green run that tested nothing -- the exact
# false-green CI guards against by probing before pytest. Do the same here.
# --------------------------------------------------------------------------
neo4j_reachable() {
    "$PY" - "$TEST_URI" "$TEST_USER" "$TEST_PASSWORD" <<'PY' 2>/dev/null
import sys
from neo4j import GraphDatabase
uri, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
driver = GraphDatabase.driver(uri, auth=(user, password))
try:
    driver.verify_connectivity()
    with driver.session() as session:
        assert session.run("RETURN 1 AS ok").single()["ok"] == 1
finally:
    driver.close()
PY
}

cmd_up() {
    say "starting test Neo4j ($CONTAINER)"
    # A container left in Exited state blocks `compose up` with a name conflict.
    # Its storage is tmpfs, so removing it discards nothing that could matter.
    if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
            echo "removing stale container (tmpfs storage, nothing to preserve)"
            docker rm -f "$CONTAINER" >/dev/null
        fi
    fi
    docker compose -f "$COMPOSE_FILE" up -d

    say "waiting for Neo4j to accept connections"
    for attempt in $(seq 1 30); do
        if neo4j_reachable; then
            echo "reachable after ${attempt} attempt(s)"
            return 0
        fi
        sleep 5
    done
    echo "ERROR: $TEST_URI did not become reachable. Refusing to run the suite," >&2
    echo "       because online tests would SKIP and report a false green." >&2
    exit 1
}

cmd_down() {
    say "stopping and deleting test Neo4j"
    docker compose -f "$COMPOSE_FILE" down -v
}

cmd_status() {
    docker ps --filter "name=$CONTAINER" --format '{{.Names}} | {{.Status}}' || true
    if neo4j_reachable; then
        echo "$TEST_URI: reachable"
    else
        echo "$TEST_URI: NOT reachable"
        return 1
    fi
}

cmd_run() {
    cmd_up

    # pytest-cov is deliberately not a project dependency -- it is only needed to
    # produce this baseline. Install into the venv without touching pyproject.toml
    # or uv.lock, so a coverage run never perturbs the dependency manifest.
    if ! "$PY" -c "import pytest_cov" >/dev/null 2>&1; then
        say "installing coverage tooling into the venv (not into pyproject/uv.lock)"
        "$PY" -m pip install -q pytest-cov coverage
    fi

    say "running full suite (offline + online) with coverage"
    echo "this takes roughly 7 minutes; CI's two jobs total about 6 without coverage"
    MENHIR_TEST_NEO4J_URI="$TEST_URI" \
    MENHIR_TEST_NEO4J_USER="$TEST_USER" \
    MENHIR_TEST_NEO4J_PASSWORD="$TEST_PASSWORD" \
    MENHIR_TEST_NEO4J_DATABASE="neo4j" \
    "$PY" -m pytest --run-online -m "not needs_llm" \
        --cov=menhir --cov-report=xml --cov-report=term -q

    say "baseline written"
    ls -la coverage.xml .coverage
    echo
    echo "Analyse per module with:"
    echo "  auditprobe src/menhir/api --type a6 --coverage coverage.xml"
    echo
    echo "The test Neo4j is still running. Stop it with:"
    echo "  bash scripts/audit/coverage_baseline.sh down"
}

case "${1:-run}" in
    run)    cmd_run ;;
    up)     cmd_up ;;
    down)   cmd_down ;;
    status) cmd_status ;;
    *)
        echo "usage: $0 [run|up|down|status]" >&2
        exit 2
        ;;
esac
