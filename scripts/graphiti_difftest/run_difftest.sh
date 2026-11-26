#!/usr/bin/env bash
# Differential check of a Graphiti tree against the Graphiti installed in Menhir's .venv, on
# Menhir's consumer-session acceptance test (tests/test_consumer_session_e2e.py).
#
#   run_difftest.sh CANDIDATE_GRAPHITI_TREE RUNS_DIR
#
# Run 1 records the baseline's OpenAI traffic (live, capped by DIFFTEST_CAP_USD, default 0.25).
# Runs 2-3 replay it on the baseline (a determinism control); run 4 replays it on the candidate.
# Requests the cache cannot answer go live and count against the cap. Then compare with:
#   python compare.py RUNS_DIR base-replay base-replay2
#   python compare.py RUNS_DIR base-replay candidate-replay
#
# Needs MENHIR_TEST_NEO4J_URI / _PASSWORD pointing at a disposable Neo4j: the test wipes it.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
M="$(cd "${HERE}/../.." && pwd)"
CANDIDATE="${1:?usage: run_difftest.sh CANDIDATE_GRAPHITI_TREE RUNS_DIR}"
D="${2:?usage: run_difftest.sh CANDIDATE_GRAPHITI_TREE RUNS_DIR}"
PY="${M}/.venv/Scripts/python.exe"
[ -x "${PY}" ] || PY="${M}/.venv/bin/python"
[ -x "${PY}" ] || { echo "no Python in ${M}/.venv" >&2; exit 1; }
# Git Bash: hand Windows Python mixed paths, which it can read; use its own PYTHONPATH separator.
if command -v cygpath >/dev/null 2>&1; then HERE="$(cygpath -m "${HERE}")"; M="$(cygpath -m "${M}")"; fi
SEP="$("${PY}" -c "import os; print(os.pathsep)")"
[ -d "${CANDIDATE}/graphiti_core" ] || { echo "${CANDIDATE} has no graphiti_core" >&2; exit 1; }
: "${MENHIR_TEST_NEO4J_URI:?set MENHIR_TEST_NEO4J_URI to a disposable Neo4j}"
: "${MENHIR_TEST_NEO4J_PASSWORD:?set MENHIR_TEST_NEO4J_PASSWORD}"
case "${MENHIR_TEST_NEO4J_URI}" in
  *:7687|*:7687/*|*:7688|*:7688/*) echo "refusing a shared Neo4j port: ${MENHIR_TEST_NEO4J_URI}" >&2; exit 1 ;;
esac

mkdir -p "${D}"
export DIFFTEST_MENHIR_DIR="${DIFFTEST_MENHIR_DIR:-${M}}" DIFFTEST_CACHE="${D}/cache" DIFFTEST_CAP_USD="${DIFFTEST_CAP_USD:-0.25}"
export MENHIR_TEST_NEO4J_USER="${MENHIR_TEST_NEO4J_USER:-neo4j}"
# NEO4J_* stays unset: conftest's pytest_configure redirects it onto MENHIR_TEST_NEO4J_* and guards prod.
unset NEO4J_URI NEO4J_USER NEO4J_PASSWORD

run() {  # name mode graphiti_tree_or_empty
  local name=$1 mode=$2 gpath=$3
  export DIFFTEST_MODE=$mode DIFFTEST_OUT="${D}/out-${name}"
  rm -rf "${DIFFTEST_OUT}"; mkdir -p "${DIFFTEST_OUT}"
  local pp="${HERE}${SEP}${M}/src"
  [ -n "${gpath}" ] && pp="${gpath}${SEP}${pp}"
  (cd "${M}" && PYTHONPATH="${pp}" "${PY}" -c "import graphiti_core; print('graphiti_core from', graphiti_core.__file__)" \
     > "${DIFFTEST_OUT}/version.txt" 2>&1
   PYTHONPATH="${pp}" "${PY}" -m pytest -p difftest_plugin -p no:cacheprovider --basetemp="${D}/pt-${name}" \
     --run-online -m online -s -q tests/test_consumer_session_e2e.py > "${DIFFTEST_OUT}/pytest.log" 2>&1)
  echo "${name} exit=$?" | tee -a "${D}/summary.txt"
  # The cap is cumulative across runs: pass on what is left.
  DIFFTEST_CAP_USD=$("${PY}" -c "import json;s=json.load(open(r'${DIFFTEST_OUT}/stats.json'));print(max(0.0,${DIFFTEST_CAP_USD}-s['spend_usd']))" 2>/dev/null || echo 0)
  export DIFFTEST_CAP_USD
}

rm -f "${D}/summary.txt"; rm -rf "${D}/cache"
run base-record record ""
run base-replay replay ""
run base-replay2 replay ""
run candidate-replay replay "${CANDIDATE}"
echo "remaining cap: \$${DIFFTEST_CAP_USD}"
