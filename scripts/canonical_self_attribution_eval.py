"""Canonical-self attribution eval: do third-party facts land on the user?

Feeds real LongMemEval user turns that mention another person ("my sister is a nurse")
through the production path -- TurnEvidence, memory, episode-admission, evidence-projection
extraction -- with MENHIR_CANONICAL_SELF_BINDING_MODE set to enforce or off. After each
projection is READY it records every RELATES_TO fact attached to the user: the bound
canonical self entity (enforce) or any first-person/"user" alias node (both modes).

`judge` then asks a model, per fact, whether the fact is about the user per their own words.
Chat (extraction and judge) goes to an OpenAI-compatible endpoint given by --chat-url (a local
model keeps this free); embeddings go to OpenAI (key from --env-file, never printed).

Disposable Neo4j only: every turn gets its own namespace and is written to the graph.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
from menhir.domain.self_identity import (
    SELF_ALIASES,
    SUBJECT_ENDPOINT_MARKER_PREFIX,
    self_uuid_for_namespace,
)
from source_memory_171_latency import _post, _read_key

THIRD_PARTY = re.compile(
    r"\bmy (mom|mother|dad|father|sister|brother|wife|husband|partner|girlfriend|boyfriend|"
    r"friend|son|daughter|boss|manager|coworker|colleague|cousin|aunt|uncle|grandma|"
    r"grandmother|grandpa|grandfather|neighbor|roommate|niece|nephew|kid|kids|parents)\b",
    re.IGNORECASE,
)


def select_turns(oracle: Path, n: int, seed: int) -> list[dict]:
    items = json.loads(oracle.read_text(encoding="utf-8"))
    seen: set[str] = set()
    pool = []
    for it in items:
        for sess in it["haystack_sessions"]:
            for t in sess:
                text = t["content"].strip()
                if t["role"] != "user" or not (40 <= len(text) <= 500) or text in seen:
                    continue
                if THIRD_PARTY.search(text):
                    seen.add(text)
                    pool.append({"qid": it["question_id"], "text": text})
    pool.sort(key=lambda r: (r["qid"], r["text"]))
    return random.Random(seed).sample(pool, min(n, len(pool)))


def backend_env(args, state: Path) -> dict[str, str]:
    import os
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    empty = state / "empty.env"
    empty.write_text("# eval: configuration comes from the environment\n", encoding="utf-8")
    env.update({
        "ENV_FILE": str(empty), "PYTHONPATH": str(REPO / "src"), "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8", "MENHIR_STATE_DIR": str(state / "menhir-state"),
        "MENHIR_LOG_DIR": str(state / "logs"),
        "NEO4J_URI": args.neo4j_uri, "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": args.neo4j_password,
        "NEO4J_DATABASE": "neo4j", "MENHIR_API_HOST": "127.0.0.1", "MENHIR_API_PORT": str(args.port),
        "MENHIR_AUTH_MODE": "none", "MENHIR_BENCHMARK_MODE": "1",
        "MENHIR_SAGA_RECONCILE_STARTUP_MODE": "observe", "MENHIR_MAX_INGEST_WORKERS": "1",
        "GRAPHITI_EMBED_PROVIDER": "openai", "OPENAI_API_KEY": _read_key(Path(args.env_file)),
        "OPENAI_EMBED_MODEL": "text-embedding-3-small",
        "MENHIR_CANONICAL_SELF_BINDING_MODE": args.mode,
    })
    if args.provider == "openai":
        env.update({
            "LLM_CHAT_PROVIDER": "openai", "GRAPHITI_LLM_PROVIDER": "openai",
            "GRAPHITI_RERANKER_PROVIDER": "openai", "OPENAI_CHAT_MODEL": args.chat_model,
            "MENHIR_OPENAI_SERVICE_TIER": "flex",
        })
    else:
        env.update({
            "LLM_CHAT_PROVIDER": "local", "GRAPHITI_LLM_PROVIDER": "local",
            "GRAPHITI_RERANKER_PROVIDER": "local", "LOCAL_LLM_BASE_URL": args.chat_url,
            "LOCAL_LLM_API_KEY": "local", "LOCAL_LLM_CHAT_MODEL": args.chat_model,
        })
    return env


def start_backend(args, state: Path):
    log = (state / f"backend-{args.mode}-{int(time.time())}.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "menhir.main", "serve"], cwd=str(state),
                            env=backend_env(args, state), stdout=log, stderr=subprocess.STDOUT,
                            text=True)
    url = f"http://127.0.0.1:{args.port}"
    deadline, last = time.monotonic() + 300, ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"backend exited {proc.returncode}; see {log.name}")
        try:
            with urllib.request.urlopen(f"{url}/api/ready", timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("status") == "ready":
                    return proc, url
                last = json.dumps(payload)[:300]
        except (OSError, ValueError) as exc:
            last = repr(exc)
        time.sleep(2)
    proc.terminate()
    raise SystemExit(f"backend not ready: {last}")


_SELF_FACTS = """
MATCH (s:Entity)-[r:RELATES_TO]-(o:Entity)
WHERE s.group_id = $ns AND (s.uuid = $self_uuid OR toLower(s.name) IN $aliases
      OR s.name STARTS WITH $marker)
RETURN s.uuid = $self_uuid AS on_canonical_self, s.name AS self_name, o.name AS other,
       r.name AS relation, r.fact AS fact
"""


def run_ingest(args) -> None:
    from neo4j import GraphDatabase
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    done = {json.loads(line)["i"] for line in out.read_text(encoding="utf-8").splitlines()} \
        if out.exists() else set()
    turns = select_turns(Path(args.oracle), args.n, args.seed)
    driver = GraphDatabase.driver(args.neo4j_uri, auth=("neo4j", args.neo4j_password))
    proc, url = start_backend(args, state)
    try:
        with out.open("a", encoding="utf-8") as fh:
            for i, turn in enumerate(turns):
                if i in done:
                    continue
                ns = f"cs-{args.mode}-{args.seed}-{i}"
                t0 = time.perf_counter()
                _ms, ev = _post(url, "/api/turn-evidence", {
                    "text": turn["text"], "role": "user", "declarant": "user", "namespace": ns,
                    "session_id": ns, "source_kind": "bench", "source_client": "bench"})
                _ms, mem = _post(url, "/api/memory", {
                    "episode": turn["text"], "source": "claude-code", "session_id": ns,
                    "namespace": ns})
                _ms, adm = _post(url, "/api/episode-admission", {
                    "episode_uuid": mem["episode_id"], "turn_evidence_uuid": ev["turn_id"]})
                proj = adm.get("projection_uuid")
                state_ = "NO_PROJECTION"
                deadline = time.monotonic() + args.timeout
                while proj and time.monotonic() < deadline:
                    with driver.session() as s:
                        rec = s.run("MATCH (p:Episodic {uuid:$u}) RETURN p.processing_state AS st",
                                    u=proj).single()
                    state_ = str(rec["st"]) if rec else "MISSING"
                    if state_ in {"READY", "FAILED", "MANUAL_REVIEW"}:
                        break
                    time.sleep(2)
                with driver.session() as s:
                    facts = [dict(r) for r in s.run(
                        _SELF_FACTS, ns=ns, self_uuid=self_uuid_for_namespace(ns),
                        aliases=sorted(SELF_ALIASES), marker=SUBJECT_ENDPOINT_MARKER_PREFIX)]
                    total = s.run("MATCH (:Entity {group_id:$ns})-[r:RELATES_TO]->() "
                                  "RETURN count(r) AS c", ns=ns).single()["c"]
                row = {"i": i, "mode": args.mode, "namespace": ns, "qid": turn["qid"],
                       "text": turn["text"], "state": state_, "linked": adm.get("linked"),
                       "seconds": round(time.perf_counter() - t0, 1), "edges_total": total,
                       "self_facts": facts}
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                print(f"[{args.mode} {i + 1}/{len(turns)}] {state_} {row['seconds']}s "
                      f"self_facts={len(facts)} edges={total}", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        driver.close()


_JUDGE = (
    "A user wrote the message below. A memory system recorded a fact and attached it to the "
    "USER (the person who wrote the message).\n\nMessage: {text}\n\nFact attached to the user: "
    "{fact}\n\nIs this fact about the user themself, as supported by the message? Answer with "
    "exactly one word: USER (it is about the user and supported), OTHER (it is really about "
    "another person), or UNSUPPORTED (the message does not say it)."
)


def _judge_call(args, content: str) -> str:
    body: dict = {"model": args.chat_model,
                  "messages": [{"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json"}
    if args.provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        body.update({"service_tier": "flex", "max_completion_tokens": 400})
        headers["Authorization"] = f"Bearer {_read_key(Path(args.env_file))}"
    else:
        url = args.chat_url.rstrip("/") + "/chat/completions"
        body.update({"temperature": 0, "max_tokens": 8,
                     "chat_template_kwargs": {"enable_thinking": False}})
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=900) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return str(payload["choices"][0]["message"]["content"] or "")


def run_judge(args) -> None:
    rows = [json.loads(x) for x in Path(args.out).read_text(encoding="utf-8").splitlines()]
    judged = []
    for row in rows:
        for f in row["self_facts"]:
            raw = _judge_call(args, _JUDGE.format(
                text=row["text"], fact=f.get("fact") or f.get("relation"))).strip().upper()
            label = next((x for x in ("OTHER", "UNSUPPORTED", "USER") if x in raw), "UNPARSED")
            judged.append({**f, "i": row["i"], "mode": row["mode"], "text": row["text"],
                           "label": label})
    Path(args.judged).write_text("\n".join(json.dumps(j) for j in judged) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for j in judged:
        counts[j["label"]] = counts.get(j["label"], 0) + 1
    print(json.dumps({"facts": len(judged), "labels": counts}))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["ingest", "judge"])
    p.add_argument("--mode", choices=["enforce", "off", "observe"], default="enforce")
    p.add_argument("--neo4j-uri", default="bolt://127.0.0.1:17533")
    p.add_argument("--neo4j-password", default="testpassword")
    p.add_argument("--provider", choices=["openai", "local"], default="openai")
    p.add_argument("--chat-url", default="")
    p.add_argument("--chat-model", default="gpt-6-luna")
    p.add_argument("--oracle", default="")
    p.add_argument("--env-file", default="")
    p.add_argument("--state", default="cs-state")
    p.add_argument("--out", required=True)
    p.add_argument("--judged", default="")
    p.add_argument("--port", type=int, default=18172)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=169)
    p.add_argument("--timeout", type=float, default=900)
    args = p.parse_args()
    (run_ingest if args.action == "ingest" else run_judge)(args)


if __name__ == "__main__":
    main()
