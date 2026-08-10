#!/usr/bin/env bash
# Stand up (or recreate) a dedicated TEST Neo4j on a REMOTE host over SSH.
#
# Tests run destructive, unscoped queries (cleanup_orphan_subgraphs DETACH DELETEs every
# isolated :Entity in the database; apply_decay rewrites the whole graph). They MUST hit
# their own Neo4j instance, never a real database. Neo4j Community allows one user database
# per instance, so the test database has to be a SEPARATE container.
#
# Most people do not need this script: docker-compose.test.yml runs the same throwaway
# instance locally on port 7688. Use this only when the test Neo4j must live on another
# machine, alongside a real instance on 7687. Storage is tmpfs -> ephemeral by
# construction, so a run can never accumulate state or be mistaken for production.
#
# Requires key-based SSH as root to the target host. Both variables are mandatory; there is
# deliberately no default host, so this can never point somewhere unintended:
#
#   OVM_HOST=neo4j-test.example  OVM_KEY=~/.ssh/id_ed25519 \
#     bash scripts/setup_remote_test_neo4j.sh [up|down|status]
#
# Then point the suite at it:
#   MENHIR_TEST_NEO4J_URI=bolt://$OVM_HOST:7688 pytest --run-online

set -euo pipefail

: "${OVM_HOST:?set OVM_HOST to the remote host that will run the test Neo4j}"
: "${OVM_KEY:?set OVM_KEY to the SSH private key used to reach OVM_HOST}"
NAME="menhir-neo4j-test"
BOLT_PORT="${BOLT_PORT:-7688}"
HTTP_PORT="${HTTP_PORT:-7475}"
TEST_PASSWORD="${MENHIR_TEST_NEO4J_PASSWORD:-testpassword}"

ssh_ovm() {
    ssh -i "$OVM_KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile=/dev/null \
        -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@${OVM_HOST}" "$@"
}

action="${1:-up}"
case "$action" in
    up)
        ssh_ovm "docker rm -f ${NAME} >/dev/null 2>&1 || true; \
            docker run -d --name ${NAME} \
              -e NEO4J_AUTH=neo4j/${TEST_PASSWORD} \
              -e NEO4J_server_memory_heap_max__size=512m \
              -p ${BOLT_PORT}:7687 -p ${HTTP_PORT}:7474 \
              --tmpfs /data --tmpfs /logs \
              --restart unless-stopped \
              neo4j:5-community >/dev/null && echo started"
        echo "Test Neo4j on ${OVM_HOST}:${BOLT_PORT}. Point tests with:"
        echo "  MENHIR_TEST_NEO4J_URI=bolt://${OVM_HOST}:${BOLT_PORT} pytest --run-online"
        ;;
    down)
        ssh_ovm "docker rm -f ${NAME} >/dev/null 2>&1 && echo removed || echo 'not running'"
        ;;
    status)
        ssh_ovm "docker ps -a --filter name=${NAME} --format '{{.Names}} {{.Status}} {{.Ports}}'"
        ;;
    *)
        echo "usage: $0 [up|down|status]" >&2
        exit 1
        ;;
esac
