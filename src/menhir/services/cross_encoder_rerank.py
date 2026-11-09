"""Cross-encoder rerank for recall (default on).

A frozen local cross-encoder scores ``(query, "name: summary")`` pairs so recall can order its
candidates by relevance instead of cutting them at the RRF rank floor. The model comes from
``MENHIR_RECALL_RERANK_MODEL_DIR`` (a dir holding ``model.onnx``, or ``onnx/model.onnx`` as in a
Hugging Face snapshot, plus ``tokenizer.json``) or, when that is unset, from a pinned download
into the Hugging Face cache (``start_model_fetch``). The ONNX digest must be in ``PINNED_MODELS``.
Evidence and plan:
``.agent/plans/menhir-recall-cross-encoder-rerank-plan-2026-11-08.md`` (workspace root).
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEVICES = ("auto", "cpu", "cuda")
MAX_LENGTH = 512
BATCH_SIZE = 16

# sha256 of model.onnx -> model label. Unknown files are refused, so a swapped model cannot
# silently change ranking.
PINNED_MODELS = {
    "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a":
        "cross-encoder/ms-marco-MiniLM-L6-v2@233902d25c440f23af6f7d6e94d2946bac0bee0a onnx fp32",
}
# Auto download (no MENHIR_RECALL_RERANK_MODEL_DIR): exactly these files at this revision.
# None means the digest is checked elsewhere (load_reranker checks model.onnx on every load).
PINNED_REPO = "cross-encoder/ms-marco-MiniLM-L6-v2"
PINNED_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
PINNED_FILES: dict[str, str | None] = {
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "onnx/model.onnx": None,
}


class RerankUnavailable(RuntimeError):
    """The reranker cannot run as configured; the message names the next step."""


class PairScorer(Protocol):
    label: str
    device: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _import_runtime() -> tuple[Any, Any]:
    try:
        import onnxruntime  # type: ignore[import-not-found]
        from tokenizers import Tokenizer  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RerankUnavailable(
            "recall rerank needs onnxruntime and tokenizers (Menhir dependencies; reinstall "
            "archolith-menhir), or set MENHIR_RECALL_RERANK_ENABLED=false"
        ) from exc
    return onnxruntime, Tokenizer


def select_providers(device: str, available: Sequence[str]) -> list[str]:
    """ONNX Runtime providers for a device setting. ``cuda`` never falls back to CPU."""
    if device not in DEVICES:
        raise RerankUnavailable(f"MENHIR_RECALL_RERANK_DEVICE must be one of {DEVICES}, got {device!r}")
    has_cuda = "CUDAExecutionProvider" in available
    if device == "cuda":
        if not has_cuda:
            raise RerankUnavailable(
                "MENHIR_RECALL_RERANK_DEVICE=cuda but ONNX Runtime has no CUDAExecutionProvider "
                f"(available: {list(available)}). Install onnxruntime-gpu with a matching CUDA, "
                "or set the device to auto or cpu."
            )
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if device == "auto" and has_cuda:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@dataclass
class OnnxCrossEncoder:
    """A pinned ONNX cross-encoder. ``score`` is thread-safe (ONNX Runtime sessions are)."""

    session: Any
    tokenizer: Any
    input_names: frozenset[str]
    label: str
    device: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        import numpy as np

        encodings = [self.tokenizer.encode(query, passage) for passage in passages]
        # Length-sorted batches: one long pair no longer pads a whole batch to 512 tokens.
        order = sorted(range(len(encodings)), key=lambda i: len(encodings[i].ids))
        out = [0.0] * len(passages)
        for start in range(0, len(order), BATCH_SIZE):
            batch = order[start:start + BATCH_SIZE]
            width = max(len(encodings[i].ids) for i in batch)
            ids = np.zeros((len(batch), width), dtype=np.int64)
            mask = np.zeros_like(ids)
            types = np.zeros_like(ids)
            for row, i in enumerate(batch):
                enc = encodings[i]
                n = len(enc.ids)
                ids[row, :n] = enc.ids
                mask[row, :n] = enc.attention_mask
                types[row, :n] = enc.type_ids
            feed = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
            logits = self.session.run(None, {k: v for k, v in feed.items() if k in self.input_names})[0]
            for row, i in enumerate(batch):
                out[i] = float(logits.reshape(-1)[row])
        return out


_CACHE: dict[tuple[str, str], PairScorer] = {}
_LOCK = threading.Lock()


def load_reranker(model_dir: str, device: str = "auto") -> PairScorer:
    """Load (once per model_dir and device) a pinned reranker, or raise RerankUnavailable."""
    key = (str(Path(model_dir).resolve()) if model_dir else "", device)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        if not model_dir:
            raise RerankUnavailable(
                "MENHIR_RECALL_RERANK_MODEL_DIR is not set: point it at a directory with model.onnx "
                "and tokenizer.json from cross-encoder/ms-marco-MiniLM-L6-v2 @233902d2 (onnx/model.onnx)"
            )
        root = Path(model_dir)
        # A flat dir, or a Hugging Face snapshot dir (model under onnx/).
        model = next((p for p in (root / "model.onnx", root / "onnx" / "model.onnx") if p.is_file()), None)
        tok = root / "tokenizer.json"
        if model is None or not tok.is_file():
            raise RerankUnavailable(f"{root} must contain tokenizer.json and model.onnx (or onnx/model.onnx)")
        digest = _sha256(model)
        label = PINNED_MODELS.get(digest)
        if label is None:
            raise RerankUnavailable(
                f"{model} has sha256 {digest}, which is not a pinned rerank model; refusing to load it"
            )
        ort, tokenizer_cls = _import_runtime()
        providers = select_providers(device, ort.get_available_providers())
        options = ort.SessionOptions()
        session = ort.InferenceSession(str(model), options, providers=providers)
        tokenizer = tokenizer_cls.from_file(str(tok))
        tokenizer.enable_truncation(MAX_LENGTH, strategy="only_second")
        tokenizer.no_padding()
        scorer = OnnxCrossEncoder(
            session=session, tokenizer=tokenizer,
            input_names=frozenset(i.name for i in session.get_inputs()),
            label=label, device="cuda" if providers[0] == "CUDAExecutionProvider" else "cpu",
        )
        _CACHE[key] = scorer
        return scorer


def candidate_text(name: str | None, summary: str | None, content: str | None) -> str:
    """The text the reranker reads: ``name: summary`` as in the R1 evaluation."""
    body = (summary or content or "").strip()
    head = (name or "").strip()
    if head and body:
        return f"{head}: {body}"
    return head or body


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    z = math.exp(value)
    return z / (1.0 + z)


_warned: set[str] = set()


async def rerank_scores(
    query: str,
    candidates: Sequence[Any],
    metadata_by_uuid: dict[str, dict[str, Any]],
    *,
    top_n: int,
    model_dir: str,
    device: str,
    loader: Any = None,
) -> tuple[dict[str, float] | None, str | None]:
    """Cross-encoder logits for the first ``top_n`` candidates and no note, or ``(None, note)`` on failure.

    A failure never breaks recall: the caller keeps the RRF floor for that call.
    """
    import asyncio

    head = list(candidates)[:max(0, top_n)]
    if not head:
        return {}, None
    texts = [
        candidate_text(
            getattr(c, "name", None),
            (metadata_by_uuid.get(getattr(c, "uuid", "")) or {}).get("summary"),
            getattr(c, "content", None),
        )
        for c in head
    ]
    load = loader or load_reranker

    def _run() -> list[float]:
        # Load in the worker too: a cold load hashes ~90 MB and builds a session.
        return load(model_dir, device).score(query, texts)

    try:
        logits = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 - rerank must degrade to the floor, never fail recall
        message = f"{type(exc).__name__}: {exc}"
        if message not in _warned:
            _warned.add(message)
            logging.getLogger(__name__).warning("recall rerank unavailable, floor applied: %s", message)
        return None, f"rerank unavailable ({type(exc).__name__}); RRF floor applied"
    return {c.uuid: float(s) for c, s in zip(head, logits)}, None


def order_by_rerank(scored: Sequence[Any], scores: dict[str, float]) -> list[Any]:
    """Scored results in cross-encoder order; unscored ones follow in their prior order.

    ``final_score`` becomes sigmoid(logit) so the shown score falls with rank. The relevance
    breakdown (and so the relevance label) is left as computed.
    """
    from dataclasses import replace

    ranked = sorted(
        (item for item in scored if item.uuid in scores),
        key=lambda item: -scores[item.uuid],
    )
    out = [replace(item, final_score=sigmoid(scores[item.uuid])) for item in ranked]
    floor = out[-1].final_score if out else None
    for item in scored:
        if item.uuid not in scores:
            value = item.final_score if floor is None else min(item.final_score, floor)
            out.append(replace(item, final_score=value))
    return out


def preload_for_settings(settings: Any) -> PairScorer | None:
    """Server startup check: load the reranker when enabled, so a bad setup fails at boot.

    Returns None when rerank is off or no model dir is set (recall then keeps the RRF floor).
    Raises RerankUnavailable when a model dir is set and cannot load.
    """
    if not getattr(settings, "recall_rerank_enabled", False):
        return None
    if not str(getattr(settings, "recall_rerank_model_dir", "") or "").strip():
        if not getattr(settings, "recall_rerank_auto_download", True):
            logging.getLogger(__name__).info(
                "recall rerank is on, but MENHIR_RECALL_RERANK_MODEL_DIR is unset and auto download "
                "is off; recall keeps the RRF floor"
            )
        return None
    return load_reranker(settings.recall_rerank_model_dir, settings.recall_rerank_device)


def fetch_pinned_model(*, local_files_only: bool, cache_dir: str | None = None) -> str:
    """The pinned model's snapshot dir in the Hugging Face cache, downloading it unless
    ``local_files_only``. Raises RerankUnavailable.

    huggingface_hub writes each file to a temp name and renames it under a file lock, so peer
    processes fetching at once never see a partial file. The tokenizer digest is checked here;
    the model digest is checked by load_reranker on every load. No HF token is sent.

    ``local_files_only`` uses ``try_to_load_from_cache``, a pure filesystem lookup: even with
    ``local_files_only=True``, ``hf_hub_download`` builds a User-Agent whose agent-harness
    detection fetches ``/api/agent-harnesses`` (a 3 s stall per hook process when blocked; SEC-01).
    """
    try:
        from huggingface_hub import hf_hub_download, try_to_load_from_cache
    except ModuleNotFoundError as exc:
        raise RerankUnavailable("recall rerank auto download needs huggingface_hub") from exc
    roots: set[Path] = set()
    for name, digest in PINNED_FILES.items():
        try:
            if local_files_only:
                found = try_to_load_from_cache(
                    PINNED_REPO, name, cache_dir=cache_dir, revision=PINNED_REVISION)
                if not isinstance(found, str):  # None or the "known missing" sentinel
                    raise FileNotFoundError(name)
                path = Path(found)
            else:
                path = Path(hf_hub_download(
                    PINNED_REPO, name, revision=PINNED_REVISION, cache_dir=cache_dir, token=False,
                ))
        except Exception as exc:  # offline, not cached, network or hub error: all mean "no model"
            verb = "find a cached" if local_files_only else "download"
            raise RerankUnavailable(
                f"could not {verb} {PINNED_REPO}/{name} ({type(exc).__name__})"
            ) from exc
        if digest is not None and _sha256(path) != digest:
            raise RerankUnavailable(f"{path} does not match its pinned sha256; refusing to use it")
        roots.add(path.parents[len(Path(name).parts) - 1])
    if len(roots) != 1:
        raise RerankUnavailable(f"pinned model files resolved to different snapshots: {sorted(roots)}")
    return str(roots.pop())


def cached_model_dir_for_settings(settings: Any) -> str:
    """Model dir for a short-lived process (hooks): the explicit dir, else an already cached
    pinned model when auto download is on, else "". Never downloads."""
    explicit = str(getattr(settings, "recall_rerank_model_dir", "") or "").strip()
    if explicit or not getattr(settings, "recall_rerank_enabled", False):
        return explicit
    if not getattr(settings, "recall_rerank_auto_download", True):
        return ""
    try:
        return fetch_pinned_model(local_files_only=True)
    except RerankUnavailable:
        return ""


def start_model_fetch(settings: Any, target: Any) -> threading.Thread | None:
    """Long-running servers: fetch and load the pinned model in the background, then set
    ``target.rerank_model_dir`` so recall starts reranking. Boot never waits on the network;
    until then, and on any failure, recall keeps the RRF floor. Returns the thread, or None
    when rerank is off, a model dir is set explicitly, or auto download is off.
    """
    if (
        target is None
        or not getattr(settings, "recall_rerank_enabled", False)
        or str(getattr(settings, "recall_rerank_model_dir", "") or "").strip()
        or not getattr(settings, "recall_rerank_auto_download", True)
    ):
        return None
    device = str(getattr(settings, "recall_rerank_device", "auto"))
    log = logging.getLogger(__name__)

    def run() -> None:
        try:
            model_dir = fetch_pinned_model(local_files_only=False)
            load_reranker(model_dir, device)
        except RerankUnavailable as exc:
            log.warning("recall rerank model unavailable, recall keeps the RRF floor: %s", exc)
            return
        except Exception as exc:  # never let a background fetch take the server down
            log.warning("recall rerank model fetch failed (%s); recall keeps the RRF floor",
                        type(exc).__name__)
            return
        # Set only after a verified load, so recall never sees a dir that cannot load.
        target.rerank_model_dir = model_dir
        log.info("recall rerank ready: %s on %s", PINNED_REPO, device)

    thread = threading.Thread(target=run, name="menhir-rerank-fetch", daemon=True)
    thread.start()
    return thread
