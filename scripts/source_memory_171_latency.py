"""#171 check C: recall latency with the source-memory section off vs on, idle or under load.

Runs a Menhir backend from this checkout against a DISPOSABLE Neo4j that holds a copy of the
LongMemEval oracle graph (one namespace per question, ``lme-<question_id>``). Chat, reranking
and Graphiti extraction go to the deterministic local provider from tests/e2e (no spend);
embeddings go to OpenAI (key read from --env-file, never printed).

Modes:
  recall  -- 195 stratified questions x repeats; each question timed with
             source_memory_limit=0 and =10 in random order (the #171/#210 method).
             --load adds a background writer: TurnEvidence + memory ingest into a separate
             namespace, and POST /api/phase3/run looping on it (scalar/event/counter lanes on).
  ingest  -- sequential POST /api/memory?wait=true, timed, with the deployment flag
             MENHIR_FRONTIER_SOURCE_MEMORIES set by --flag (it gates the ingest embedding).

Never point this at a graph holding real memories: the load mode writes to it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "e2e"))
from _harness.providers import deterministic_provider

LIMIT_ON = 10


def _post(url: str, path: str, body: dict, timeout: float = 300.0) -> tuple[float, dict]:
    req = urllib.request.Request(
        f"{url}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"POST {path} -> {exc.code}: {detail}") from exc
    return (time.perf_counter() - t0) * 1000.0, payload


def _read_key(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"no OPENAI_API_KEY in {env_file}")


def _backend_env(args, provider_url: str, state: Path, port: int, flag: bool) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    empty_env = state / "empty.env"
    empty_env.write_text("# check C harness: configuration comes from the environment\n",
                         encoding="utf-8")
    env.update({
        "ENV_FILE": str(empty_env),
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "MENHIR_STATE_DIR": str(state / "menhir-state"),
        "MENHIR_LOG_DIR": str(state / "logs"),
        "NEO4J_URI": args.neo4j_uri,
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "unused-auth-disabled",
        "NEO4J_DATABASE": "neo4j",
        "MENHIR_API_HOST": "127.0.0.1",
        "MENHIR_API_PORT": str(port),
        "MENHIR_AUTH_MODE": "none",
        "MENHIR_BENCHMARK_MODE": "1",  # no scheduler; consolidation is driven explicitly
        "MENHIR_SAGA_RECONCILE_STARTUP_MODE": "observe",
        "LLM_CHAT_PROVIDER": "local",
        "GRAPHITI_LLM_PROVIDER": "local",
        "GRAPHITI_RERANKER_PROVIDER": "local",
        "LOCAL_LLM_BASE_URL": provider_url,
        "LOCAL_LLM_API_KEY": "test-only",
        "LOCAL_LLM_CHAT_MODEL": "deterministic-chat",
        "GRAPHITI_EMBED_PROVIDER": "openai",
        "OPENAI_API_KEY": _read_key(Path(args.env_file)),
        "OPENAI_EMBED_MODEL": "text-embedding-3-small",
        "MENHIR_FRONTIER_SOURCE_MEMORIES": "true" if flag else "false",
        "MENHIR_PERSONAL_MEMORY_CONSOLIDATION_ENABLED": "true",
        "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "true",
        "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "true",
    })
    return env


def _start_backend(args, provider_url: str, state: Path, flag: bool):
    port = args.port
    log = (state / f"backend-{int(time.time())}.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "menhir.main", "serve"], cwd=str(state),
        env=_backend_env(args, provider_url, state, port, flag), stdout=log,
        stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 240
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"backend exited {proc.returncode}; see {log.name}")
        try:
            with urllib.request.urlopen(f"{url}/api/ready", timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("status") == "ready":
                    return proc, url, payload
                last = json.dumps(payload)[:300]
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = repr(exc)
        time.sleep(2)
    proc.terminate()
    raise SystemExit(f"backend not ready: {last}")


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def _questions(oracle: Path, namespaces: set[str], n: int, seed: int) -> list[dict]:
    items = json.loads(oracle.read_text(encoding="utf-8"))
    by_type: dict[str, list[dict]] = {}
    for it in items:
        ns = f"lme-{it['question_id']}"
        if ns in namespaces:
            by_type.setdefault(it["question_type"], []).append(
                {"qid": it["question_id"], "type": it["question_type"],
                 "question": it["question"], "namespace": ns})
    total = sum(len(v) for v in by_type.values())
    rng = random.Random(seed)
    picked: list[dict] = []
    for qtype in sorted(by_type):
        pool = sorted(by_type[qtype], key=lambda q: q["qid"])
        take = max(1, round(n * len(pool) / total))
        picked.extend(rng.sample(pool, min(take, len(pool))))
    return picked[:n]


def _namespaces(neo4j_uri: str) -> set[str]:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(neo4j_uri, auth=("neo4j", "unused"))
    try:
        with driver.session() as s:
            rows = s.run("MATCH (n:Episodic) WHERE n.valid_at IS NULL "
                         "RETURN DISTINCT coalesce(n.namespace, n.group_id) AS ns")
            return {r["ns"] for r in rows if r["ns"]}
    finally:
        driver.close()


class _Load:
    """Background writer: turns + memories into one namespace, phase3 passes looping on it."""

    def __init__(self, url: str, namespace: str, interval_s: float) -> None:
        self.url, self.namespace, self.interval_s = url, namespace, interval_s
        self.stop = threading.Event()
        self.ingested = 0
        self.errors: list[str] = []
        self.passes: list[dict] = []

    def _ingest(self) -> None:
        i = 0
        while not self.stop.is_set():
            i += 1
            text = (f"I now have {random.randint(2, 90)} plants on the balcony and I moved "
                    f"the meeting to {random.choice(['Monday', 'Friday'])} ({i}).")
            try:
                _ms, turn = _post(self.url, "/api/turn-evidence", {
                    "text": text, "role": "user", "declarant": "user",
                    "namespace": self.namespace, "source_kind": "bench",
                    "occurred_at": datetime.now(UTC).isoformat()})
                _post(self.url, "/api/memory", {
                    "episode": text, "namespace": self.namespace,
                    "turn_evidence_uuid": turn["turn_id"]})
                self.ingested += 1
            except Exception as exc:  # noqa: BLE001 -- recorded; the load must keep running
                self.errors.append(f"ingest: {exc}"[:300])
            self.stop.wait(self.interval_s)

    def _consolidate(self) -> None:
        while not self.stop.is_set():
            try:
                ms, body = _post(self.url, "/api/phase3/run",
                                 {"namespace": self.namespace, "k": 3}, timeout=900)
                self.passes.append({"ms": ms, "views_written": body.get("views_written"),
                                    "dirty_after": body.get("dirty_after")})
            except Exception as exc:  # noqa: BLE001 -- recorded; the load must keep running
                self.errors.append(f"phase3: {exc}"[:300])
                self.stop.wait(5)
            self.stop.wait(1)

    def start(self) -> None:
        self.threads = [threading.Thread(target=self._ingest, daemon=True),
                        threading.Thread(target=self._consolidate, daemon=True)]
        for t in self.threads:
            t.start()

    def finish(self) -> dict:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=900)
        return {"ingested": self.ingested, "passes": self.passes, "errors": self.errors[:20],
                "error_count": len(self.errors)}


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


def run_recall(args, url: str, out: dict) -> None:
    qs = _questions(Path(args.oracle), _namespaces(args.neo4j_uri), args.n, args.seed)
    out["questions"] = len(qs)
    rng = random.Random(args.seed + 1)
    # Warm-up, discarded: one call per question, so the query embedding is cached before
    # timing and neither arm pays the provider round trip.
    for q in qs:
        _post(url, "/api/recall", {"query": q["question"], "namespace": q["namespace"],
                                    "source_memory_limit": LIMIT_ON})
    load = None
    if args.load:
        load = _Load(url, f"load-171c-{uuid.uuid4().hex[:8]}", args.load_interval)
        load.start()
        time.sleep(args.load_warmup)
    rows = []
    for rep in range(args.repeats):
        order = qs[:]
        rng.shuffle(order)
        for q in order:
            arms = [0, LIMIT_ON]
            rng.shuffle(arms)
            for limit in arms:
                ms, body = _post(url, "/api/recall", {
                    "query": q["question"], "namespace": q["namespace"],
                    "source_memory_limit": limit})
                rows.append({"qid": q["qid"], "type": q["type"], "rep": rep, "limit": limit,
                             "ms": ms, "source_memories": len(body.get("source_memories") or [])})
    if load is not None:
        out["load"] = load.finish()
    out["rows"] = rows
    off = [r["ms"] for r in rows if r["limit"] == 0]
    on = [r["ms"] for r in rows if r["limit"] == LIMIT_ON]
    paired = {}
    for r in rows:
        paired.setdefault((r["qid"], r["rep"]), {})[r["limit"]] = r["ms"]
    diffs = [v[LIMIT_ON] - v[0] for v in paired.values() if len(v) == 2]
    out["summary"] = {
        "n_per_arm": len(off),
        "off_p50": statistics.median(off), "on_p50": statistics.median(on),
        "off_p95": _pct(off, 95), "on_p95": _pct(on, 95),
        "p50_change_pct": 100 * (statistics.median(on) / statistics.median(off) - 1),
        "p95_change_pct": 100 * (_pct(on, 95) / _pct(off, 95) - 1),
        "paired_diff_median_ms": statistics.median(diffs),
        "paired_diff_p95_ms": _pct(diffs, 95),
        "on_rows_with_section": sum(1 for r in rows if r["limit"] and r["source_memories"]),
    }
    if load is not None and out["load"]["passes"]:
        pass_ms = [p["ms"] for p in out["load"]["passes"]]
        out["summary"]["phase3_pass_ms_max"] = max(pass_ms)
        out["summary"]["phase3_pass_ms_p50"] = statistics.median(pass_ms)


def run_ingest(args, url: str, out: dict) -> None:
    ns = f"ingest-171c-{'on' if args.flag else 'off'}-{uuid.uuid4().hex[:8]}"
    rows = []
    for i in range(args.n):
        text = f"Ingest timing note {i}: the staging database moved to host db{i}."
        ms, body = _post(url, "/api/memory?wait=true", {"episode": text, "namespace": ns})
        rows.append({"i": i, "ms": ms, "status": body.get("status")})
    out["rows"] = rows
    ok = [r["ms"] for r in rows if r["status"] == "ready"]
    out["summary"] = {"flag": args.flag, "n": len(rows), "ready": len(ok),
                      "p50": statistics.median(ok) if ok else None,
                      "p95": _pct(ok, 95) if ok else None}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["recall", "ingest"])
    p.add_argument("--neo4j-uri", required=True)
    p.add_argument("--oracle", default="")
    p.add_argument("--env-file", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--port", type=int, default=18171)
    p.add_argument("--n", type=int, default=195)
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--seed", type=int, default=171)
    p.add_argument("--load", action="store_true")
    p.add_argument("--load-interval", type=float, default=1.0)
    p.add_argument("--load-warmup", type=float, default=60.0)
    p.add_argument("--flag", type=lambda s: s.lower() == "true", default=True)
    args = p.parse_args()
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    out: dict = {"mode": args.mode, "load": args.load, "started": datetime.now(UTC).isoformat(),
                 "args": {k: v for k, v in vars(args).items() if k != "env_file"}}
    with deterministic_provider() as provider_url:
        proc, url, ready = _start_backend(args, provider_url, state, flag=args.flag
                                          if args.mode == "ingest" else True)
        out["ready"] = {k: ready.get(k) for k in ("status", "mode") if k in ready}
        try:
            (run_recall if args.mode == "recall" else run_ingest)(args, url, out)
        finally:
            _stop(proc)
    out["finished"] = datetime.now(UTC).isoformat()
    Path(args.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out.get("summary"), indent=1))
    if out.get("load"):
        ld = out["load"] if isinstance(out["load"], dict) else {}
        print(json.dumps({k: ld.get(k) for k in ("ingested", "error_count")}, default=str))


if __name__ == "__main__":
    main()
