"""pytest plugin: record/replay OpenAI HTTP traffic and dump the graph before teardown.

Loaded by run_difftest.sh with ``-p difftest_plugin``; see README.md.

Env:
  DIFFTEST_MODE        record | replay
  DIFFTEST_CACHE       directory for cached responses (shared between runs)
  DIFFTEST_OUT         directory for this run's dump, misses and usage
  DIFFTEST_CAP_USD     abort live spend above this (default 0.20)
  DIFFTEST_MENHIR_DIR  Menhir checkout whose .env holds OPENAI_API_KEY (default: this repo)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import dotenv
import httpx
import pytest

# Production settings read from menhir-prod-app on 2026-11-25 (non-secret keys only); update them
# when production changes. The checkout's .env is NOT production config; only the API key is taken
# from it.
PROD_ENV = {
    "LLM_CHAT_PROVIDER": "openai",
    "GRAPHITI_LLM_PROVIDER": "openai",
    "GRAPHITI_EMBED_PROVIDER": "openai",
    "OPENAI_CHAT_MODEL": "gpt-6-luna",
    "OPENAI_EMBED_MODEL": "text-embedding-3-small",
    "LOCAL_LLM_BASE_URL": "",
    "LOCAL_LLM_CHAT_MODEL": "",
    "MENHIR_CANONICAL_SELF_BINDING_MODE": "enforce",
    "MENHIR_PRIVACY_REDACT": "true",
    "MENHIR_RECALL_RERANK_ENABLED": "true",
    "MENHIR_RECALL_RERANK_TOP_N": "15",
}
_ENV_FILE = (
    Path(os.environ.get("DIFFTEST_MENHIR_DIR") or Path(__file__).resolve().parents[2])
    / ".env"
)
os.environ.update(PROD_ENV)
_api_key = dotenv.dotenv_values(_ENV_FILE).get("OPENAI_API_KEY")
if _api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = _api_key


def _key_only_load_dotenv(*_args, **_kwargs) -> bool:
    # live_dotenv would otherwise pull the checkout's non-production settings in.
    return True


dotenv.load_dotenv = _key_only_load_dotenv

MODE = os.environ["DIFFTEST_MODE"]
CACHE = Path(os.environ["DIFFTEST_CACHE"])
OUT = Path(os.environ["DIFFTEST_OUT"])
CAP = float(os.environ.get("DIFFTEST_CAP_USD", "0.20"))
CACHE.mkdir(parents=True, exist_ok=True)
(OUT / "misses").mkdir(parents=True, exist_ok=True)

# USD per 1M tokens (input, output)
PRICES = {"gpt-6-luna": (0.10, 0.50), "text-embedding-3-small": (0.02, 0.0)}
_lock = threading.Lock()
STATS = {"hits": 0, "misses": 0, "live_calls": 0, "spend_usd": 0.0, "by_path": {}}


# Run-varying tokens: Menhir's per-run subject markers and this run's wall-clock timestamps.
# They are replaced by placeholders in the cache key; on a hit, the recorded tokens in the
# response are mapped back onto this run's tokens by order of first appearance.
_RUN_DATE = (
    __import__("datetime")
    .datetime.now(__import__("datetime").timezone.utc)
    .strftime("%Y-%m-%d")
)
_VARYING = re.compile(
    r"Menhir[A-Za-z]+_[0-9a-f]{12,}"
    # test_consumer_session_e2e: one uuid4().hex[:8] suffix per run, plus per-turn prompt ids
    r"|\b(?:acme|other|sess-[ab]|p)-[0-9a-f]{8}\b"
    r"|20\d\d-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?"
)


def _tokens(text: str) -> list[str]:
    seen: list[str] = []
    for m in _VARYING.finditer(text):
        tok = m.group(0)
        if tok[:4].isdigit() and not tok.startswith(_RUN_DATE[:8]):
            continue  # a fixed date in the test story, not this run's clock
        if tok not in seen:
            seen.append(tok)
    return seen


def _normalize(text: str, toks: list[str]) -> str:
    for i, tok in enumerate(sorted(toks, key=len, reverse=True)):
        text = text.replace(tok, f"<V{toks.index(tok)}>")
    return text


def _canonical_body(request: httpx.Request) -> str:
    body = (request.content or b"").decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(body), sort_keys=True)
    except ValueError:
        return body


def _key(request: httpx.Request) -> tuple[str, list[str]]:
    body = _canonical_body(request)
    toks = _tokens(body)
    digest = hashlib.sha256(
        request.method.encode()
        + str(request.url).encode()
        + _normalize(body, toks).encode()
    ).hexdigest()
    return digest, toks


def _remap(text: str, recorded: list[str], current: list[str]) -> str:
    if len(recorded) != len(current):
        return text
    pairs = sorted(zip(recorded, current), key=lambda p: len(p[0]), reverse=True)
    for i, (old, _new) in enumerate(pairs):
        text = text.replace(old, f"\x00{i}\x00")
    for i, (_old, new) in enumerate(pairs):
        text = text.replace(f"\x00{i}\x00", new)
    return text


def _is_embeddings(request: httpx.Request) -> bool:
    return request.url.path.endswith("/embeddings")


def _embed_key(model: str, fmt: str, text: str) -> str:
    return hashlib.sha256(
        f"{model}\x00{fmt}\x00{_normalize(text, _tokens(text))}".encode()
    ).hexdigest()


def _is_openai(request: httpx.Request) -> bool:
    return request.url.host == "api.openai.com"


def _account(request: httpx.Request, payload: bytes) -> None:
    try:
        data = json.loads(payload)
    except ValueError:
        return
    usage = data.get("usage") or {}
    model = str(data.get("model", ""))
    # Unknown model: price high so the cap stays conservative.
    price = next(
        (p for name, p in PRICES.items() if model.startswith(name)), (2.0, 8.0)
    )
    tin = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    tout = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    with _lock:
        STATS["live_calls"] += 1
        STATS["spend_usd"] += tin / 1e6 * price[0] + tout / 1e6 * price[1]
        if STATS["spend_usd"] > CAP:
            raise RuntimeError(
                f"DIFFTEST cost cap exceeded: {STATS['spend_usd']:.4f} > {CAP}"
            )


def _miss(request: httpx.Request, key: str) -> None:
    if MODE == "replay":
        with _lock:
            STATS["misses"] += 1
            n = STATS["misses"]
        (OUT / "misses" / f"{n:04d}-{key[:12]}.json").write_bytes(
            request.content or b""
        )


def _embed_lookup(request: httpx.Request) -> httpx.Response | None:
    req = json.loads(request.content or b"{}")
    inputs = req.get("input")
    inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
    model, fmt = str(req.get("model")), str(req.get("encoding_format", "float"))
    vectors = []
    for text in inputs:
        path = CACHE / "emb" / f"{_embed_key(model, fmt, str(text))}.json"
        if not path.exists():
            _miss(request, _embed_key(model, fmt, str(text)))
            return None
        vectors.append(json.loads(path.read_text(encoding="utf-8")))
    with _lock:
        STATS["hits"] += 1
    data = [
        {"object": "embedding", "index": i, "embedding": v}
        for i, v in enumerate(vectors)
    ]
    body = {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
        request=request,
    )


def _lookup(request: httpx.Request) -> httpx.Response | None:
    with _lock:
        STATS["by_path"][request.url.path] = (
            STATS["by_path"].get(request.url.path, 0) + 1
        )
    if _is_embeddings(request):
        return _embed_lookup(request)
    key, toks = _key(request)
    path = CACHE / f"{key}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        with _lock:
            STATS["hits"] += 1
        return httpx.Response(
            cached["status"],
            headers={"content-type": "application/json"},
            content=_remap(cached["body"], cached.get("tokens", []), toks).encode(
                "utf-8"
            ),
            request=request,
        )
    _miss(request, key)
    return None


def _store(request: httpx.Request, response: httpx.Response, body: bytes) -> None:
    _account(request, body)
    if response.status_code != 200:
        return
    if _is_embeddings(request):
        req = json.loads(request.content or b"{}")
        inputs = req.get("input")
        inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
        model, fmt = str(req.get("model")), str(req.get("encoding_format", "float"))
        (CACHE / "emb").mkdir(exist_ok=True)
        for item in json.loads(body).get("data", []):
            text = str(inputs[item["index"]])
            (CACHE / "emb" / f"{_embed_key(model, fmt, text)}.json").write_text(
                json.dumps(item["embedding"]), encoding="utf-8"
            )
        return
    key, toks = _key(request)
    (CACHE / f"{key}.json").write_text(
        json.dumps({"status": 200, "body": body.decode("utf-8"), "tokens": toks}),
        encoding="utf-8",
    )


_orig_send = httpx.Client.send
_orig_async_send = httpx.AsyncClient.send


def _send(self, request, *args, **kwargs):
    if not _is_openai(request):
        return _orig_send(self, request, *args, **kwargs)
    hit = _lookup(request)
    if hit is not None:
        return hit
    response = _orig_send(self, request, *args, **kwargs)
    body = response.read()
    _store(request, response, body)
    return response


async def _async_send(self, request, *args, **kwargs):
    if not _is_openai(request):
        return await _orig_async_send(self, request, *args, **kwargs)
    hit = _lookup(request)
    if hit is not None:
        return hit
    response = await _orig_async_send(self, request, *args, **kwargs)
    body = await response.aread()
    _store(request, response, body)
    return response


httpx.Client.send = _send
httpx.AsyncClient.send = _async_send

_VOLATILE = {
    "uuid",
    "created_at",
    "updated_at",
    "last_accessed",
    "enriched_at",
    "queued_at",
    "name_embedding",
    "fact_embedding",
    "embedding",
    "summary_embedding",
    "content_embedding",
    "claimed_at",
    "lease_expires_at",
    "lease_token",
    "started_at",
    "completed_at",
    "ingested_at",
}


def _norm(value):
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dump_graph() -> None:
    from neo4j import GraphDatabase

    uri = os.environ["MENHIR_TEST_NEO4J_URI"]
    auth = (
        os.environ.get("MENHIR_TEST_NEO4J_USER", "neo4j"),
        os.environ["MENHIR_TEST_NEO4J_PASSWORD"],
    )
    with GraphDatabase.driver(uri, auth=auth) as driver, driver.session() as session:
        nodes = [
            dict(r)
            for r in session.run(
                "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"
            )
        ]
        rels = [
            dict(r)
            for r in session.run(
                "MATCH (a)-[r]->(b) RETURN elementId(a) AS a, type(r) AS t, elementId(b) AS b, "
                "properties(r) AS props"
            )
        ]
    uuid_label: dict[str, str] = {}
    id_label: dict[str, str] = {}
    for n in nodes:
        p = n["props"]
        label = (
            "|".join(sorted(n["labels"]))
            + ":"
            + str(
                p.get("name") or p.get("content", "")[:60] or p.get("node_type") or ""
            )
        )
        id_label[n["id"]] = label
        if p.get("uuid"):
            uuid_label[str(p["uuid"])] = label

    def clean(props: dict) -> dict:
        out = {}
        for k, v in sorted(props.items()):
            if (
                k in _VOLATILE
                or k.endswith("_at")
                and k not in {"valid_at", "invalid_at", "expired_at"}
            ):
                continue
            v = _norm(v)
            if isinstance(v, str) and v in uuid_label:
                v = "<" + uuid_label[v] + ">"
            if isinstance(v, list):
                v = (
                    sorted(
                        ("<" + uuid_label[x] + ">")
                        if isinstance(x, str) and x in uuid_label
                        else x
                        for x in v
                        if x is not None
                    )
                    if all(not isinstance(x, (list, dict)) for x in v)
                    else v
                )
            if k in {"valid_at", "invalid_at", "expired_at"} and v is not None:
                v = "SET" if k == "expired_at" else v
            out[k] = v
        return out

    dump = {
        "nodes": sorted(
            json.dumps(
                {"labels": sorted(n["labels"]), **clean(n["props"])},
                sort_keys=True,
                default=str,
            )
            for n in nodes
        ),
        "rels": sorted(
            json.dumps(
                {
                    "a": id_label[r["a"]],
                    "t": r["t"],
                    "b": id_label[r["b"]],
                    **clean(r["props"]),
                },
                sort_keys=True,
                default=str,
            )
            for r in rels
        ),
    }
    (OUT / "graph.json").write_text(json.dumps(dump, indent=1), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        _dump_graph()
    except Exception as exc:  # noqa: BLE001
        (OUT / "dump_error.txt").write_text(repr(exc), encoding="utf-8")
    yield


def pytest_sessionfinish(session, exitstatus):
    STATS["exitstatus"] = int(exitstatus)
    (OUT / "stats.json").write_text(json.dumps(STATS, indent=1), encoding="utf-8")
