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
import os
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
_MINILM_ONNX = "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a"
_MINILM_TOKENIZER = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"
PINNED_MODELS = {
    _MINILM_ONNX: "cross-encoder/ms-marco-MiniLM-L6-v2@233902d25c440f23af6f7d6e94d2946bac0bee0a onnx fp32",
}
# model.onnx sha256 -> the tokenizer.json sha256 it must load with (a swapped tokenizer changes
# ranking as much as a swapped model).
PINNED_TOKENIZERS = {_MINILM_ONNX: _MINILM_TOKENIZER}
# Auto download (no MENHIR_RECALL_RERANK_MODEL_DIR): exactly these files at this revision.
# None means the digest is checked elsewhere (load_reranker checks model.onnx on every load).
PINNED_REPO = "cross-encoder/ms-marco-MiniLM-L6-v2"
PINNED_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
PINNED_FILES: dict[str, str | None] = {
    "tokenizer.json": _MINILM_TOKENIZER,
    "onnx/model.onnx": None,
}
# Sent in place of huggingface_hub's default User-Agent, which names the detected agent harness
# and local library versions (SEC-02).
DOWNLOAD_USER_AGENT = "menhir-recall-rerank"


class RerankUnavailable(RuntimeError):
    """The reranker cannot run as configured; the message names the next step."""


class RerankDigestMismatch(RerankUnavailable):
    """A model or tokenizer file is not the pinned one."""


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


def _read(path: Path) -> bytes:
    """A pinned file's bytes. Callers hash and use these same bytes, never the path again (SEC-03)."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RerankUnavailable(f"could not read {path} ({type(exc).__name__})") from exc


def _import_runtime() -> tuple[Any, Any]:
    try:
        import onnxruntime  # type: ignore[import-not-found]
        from tokenizers import Tokenizer  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RerankUnavailable(
            "recall rerank needs onnxruntime and tokenizers (Menhir dependencies; reinstall "
            "archolith-menhir), or set MENHIR_RECALL_RERANK_ENABLED=false"
        ) from exc
    disable = getattr(onnxruntime, "disable_telemetry_events", None)
    if disable is not None:
        try:
            disable()
        except Exception:  # noqa: BLE001 - telemetry opt-out must never block loading
            pass
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
        # One scoring at a time per process: each run already uses ONNX Runtime's full intra-op
        # pool, so concurrent recalls (or a many-arm Recall Lab run) queue instead of
        # oversubscribing the CPU (SEC-06).
        with _SCORE_SLOT:
            return self._score(query, passages)

    def _score(self, query: str, passages: Sequence[str]) -> list[float]:
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


CGROUP_ROOT = Path("/sys/fs/cgroup")


def cgroup_cpu_limit(root: Path = CGROUP_ROOT) -> float | None:
    """CPUs allowed by this container's cgroup quota (v2 ``cpu.max``, else v1 CFS), or None."""
    try:
        quota, period = (root / "cpu.max").read_text(encoding="ascii").split()[:2]
        if quota == "max":
            return None
        if int(period) > 0 and int(quota) > 0:
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        quota_us = int((root / "cpu" / "cpu.cfs_quota_us").read_text(encoding="ascii"))
        period_us = int((root / "cpu" / "cpu.cfs_period_us").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    return quota_us / period_us if quota_us > 0 and period_us > 0 else None


def visible_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def resolve_threads(setting: int, *, limit: float | None, cpus: int) -> int | None:
    """ONNX Runtime intra-op threads for MENHIR_RECALL_RERANK_THREADS (#567).

    A positive setting is used as given. 0 (auto) caps the pool at the cgroup CPU quota when
    that quota is below the visible CPUs: ONNX Runtime sizes its pool from the visible cores,
    and on a 1-CPU container with 6 visible cores the extra threads were throttled by the
    quota, stalling the whole process. None keeps ONNX Runtime's own default (no quota).
    """
    if setting > 0:
        return setting
    if limit is None:
        return None
    capped = max(1, int(limit))  # floor: 1.5 CPUs gets 1 thread, not 2 that the quota throttles
    return capped if capped < cpus else None


_thread_setting = 0
_auto_threads: list[int | None] = []  # memo for setting 0: the cgroup is read once per process


def configure_threads(setting: int) -> None:
    """Process-wide MENHIR_RECALL_RERANK_THREADS, set once at startup from settings."""
    global _thread_setting
    _thread_setting = max(0, int(setting))


def current_threads() -> int | None:
    if _thread_setting > 0:
        return _thread_setting
    if not _auto_threads:
        _auto_threads.append(resolve_threads(0, limit=cgroup_cpu_limit(), cpus=visible_cpus()))
    return _auto_threads[0]


_CACHE: dict[tuple[str, str, int | None], PairScorer] = {}
_LOCK = threading.Lock()
_SCORE_SLOT = threading.BoundedSemaphore(1)


def load_reranker(model_dir: str, device: str = "auto") -> PairScorer:
    """Load (once per model_dir, device and thread count) a pinned reranker, or raise RerankUnavailable."""
    threads = current_threads()
    key = (str(Path(model_dir).resolve()) if model_dir else "", device, threads)
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
        # Read each file once, check its digest, and build from those bytes (SEC-03/04).
        model_bytes = _read(model)
        digest = hashlib.sha256(model_bytes).hexdigest()
        label = PINNED_MODELS.get(digest)
        if label is None:
            raise RerankDigestMismatch(
                f"{model} has sha256 {digest}, which is not a pinned rerank model; refusing to load it"
            )
        tok_bytes = _read(tok)
        if hashlib.sha256(tok_bytes).hexdigest() != PINNED_TOKENIZERS.get(digest):
            raise RerankDigestMismatch(
                f"{tok} is not the tokenizer pinned for {label}; refusing to load it"
            )
        ort, tokenizer_cls = _import_runtime()
        providers = select_providers(device, ort.get_available_providers())
        options = ort.SessionOptions()
        if threads is not None:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        session = ort.InferenceSession(model_bytes, options, providers=providers)
        tokenizer = tokenizer_cls.from_str(tok_bytes.decode("utf-8"))
        tokenizer.enable_truncation(MAX_LENGTH, strategy="only_second")
        tokenizer.no_padding()
        scorer = OnnxCrossEncoder(
            session=session, tokenizer=tokenizer,
            input_names=frozenset(i.name for i in session.get_inputs()),
            label=label, device="cuda" if providers[0] == "CUDAExecutionProvider" else "cpu",
        )
        _CACHE[key] = scorer
        logging.getLogger(__name__).info(
            "recall rerank loaded: %s on %s, %s", label, scorer.device, threads_label(threads))
        return scorer


def threads_label(threads: int | None) -> str:
    return f"{threads} intra-op thread(s)" if threads is not None else "onnxruntime default threads"


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
    configure_threads(getattr(settings, "recall_rerank_threads", 0))
    if not str(getattr(settings, "recall_rerank_model_dir", "") or "").strip():
        if not getattr(settings, "recall_rerank_auto_download", True):
            logging.getLogger(__name__).info(
                "recall rerank is on, but MENHIR_RECALL_RERANK_MODEL_DIR is unset and auto download "
                "is off; recall keeps the RRF floor"
            )
        return None
    return load_reranker(settings.recall_rerank_model_dir, settings.recall_rerank_device)


def fetch_pinned_model(
    *, local_files_only: bool, cache_dir: str | None = None, force_download: bool = False,
) -> str:
    """The pinned model's snapshot dir in the Hugging Face cache, downloading it unless
    ``local_files_only`` (``force_download`` replaces cached copies). Raises RerankUnavailable,
    or RerankDigestMismatch when a file is not the pinned one.

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
    if force_download and not local_files_only:
        # huggingface_hub refreshes the blob but keeps an existing snapshot entry, which is a
        # plain copy where symlinks are unavailable (Windows): drop ours so it is rebuilt.
        for name in PINNED_FILES:
            found = try_to_load_from_cache(PINNED_REPO, name, cache_dir=cache_dir, revision=PINNED_REVISION)
            if isinstance(found, str):
                try:
                    Path(found).unlink(missing_ok=True)
                except OSError:
                    pass  # the download below then fails its pin again and recall keeps the floor
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
                    force_download=force_download, headers={"user-agent": DOWNLOAD_USER_AGENT},
                ))
        except Exception as exc:  # offline, not cached, network or hub error: all mean "no model"
            verb = "find a cached" if local_files_only else "download"
            raise RerankUnavailable(
                f"could not {verb} {PINNED_REPO}/{name} ({type(exc).__name__})"
            ) from exc
        if digest is not None and hashlib.sha256(_read(path)).hexdigest() != digest:
            raise RerankDigestMismatch(f"{path} does not match its pinned sha256; refusing to use it")
        roots.add(path.parents[len(Path(name).parts) - 1])
    if len(roots) != 1:
        raise RerankUnavailable(f"pinned model files resolved to different snapshots: {sorted(roots)}")
    return str(roots.pop())


def cache_dir_for_settings(settings: Any) -> str | None:
    """MENHIR_RECALL_RERANK_CACHE_DIR, or None for huggingface_hub's default (HF_HOME)."""
    return str(getattr(settings, "recall_rerank_cache_dir", "") or "").strip() or None


def tmpfs_mount_for(path: str, mounts_file: str = "/proc/mounts", *, resolve: bool = True) -> str | None:
    """The tmpfs mount point holding ``path``, or None (not tmpfs, or no mount table).

    The longest mount point that contains the path wins, as the kernel resolves it.
    ``resolve`` follows symlinks first (POSIX only; mount tables are Linux-only anyway).
    """
    try:
        lines = Path(mounts_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = os.path.realpath(path) if resolve and os.name == "posix" else path
    best: tuple[str, str] | None = None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        # /proc/mounts escapes spaces and other specials as octal (\040).
        point = fields[1].encode("ascii", "replace").decode("unicode_escape")
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) > len(best[0])):
            best = (point, fields[2])
    return best[0] if best is not None and best[1] == "tmpfs" else None


def cached_model_dir_for_settings(settings: Any) -> str:
    """Model dir for a short-lived process (hooks): the explicit dir, else an already cached
    pinned model when auto download is on, else "". Never downloads."""
    if not getattr(settings, "recall_rerank_enabled", False):
        return ""  # off means no model dir anywhere, so no tuning default can turn it back on (SEC-07)
    explicit = str(getattr(settings, "recall_rerank_model_dir", "") or "").strip()
    if explicit:
        return explicit
    if not getattr(settings, "recall_rerank_auto_download", True):
        return ""
    try:
        return fetch_pinned_model(local_files_only=True, cache_dir=cache_dir_for_settings(settings))
    except RerankUnavailable:
        return ""


_FETCH_LOCK = threading.Lock()
_fetch_thread: threading.Thread | None = None
_fetch_targets: list[Any] = []


def start_model_fetch(settings: Any, target: Any) -> threading.Thread | None:
    """Long-running servers: fetch and load the pinned model in the background, then set
    ``target.rerank_model_dir`` so recall starts reranking. Boot never waits on the network;
    until then, and on any failure, recall keeps the RRF floor. Returns the thread, or None
    when rerank is off, a model dir is set explicitly, or auto download is off.

    One fetch per process: a call while one runs (a runtime-init retry) joins it, and the
    finished fetch sets every waiting target (SEC-15).
    """
    global _fetch_thread
    if (
        target is None
        or not getattr(settings, "recall_rerank_enabled", False)
        or str(getattr(settings, "recall_rerank_model_dir", "") or "").strip()
        or not getattr(settings, "recall_rerank_auto_download", True)
    ):
        return None
    device = str(getattr(settings, "recall_rerank_device", "auto"))
    cache_dir = cache_dir_for_settings(settings)
    log = logging.getLogger(__name__)

    def fetch_and_load(force: bool) -> str:
        model_dir = fetch_pinned_model(
            local_files_only=False, force_download=force, cache_dir=cache_dir)
        load_reranker(model_dir, device)
        return model_dir

    def run() -> None:
        global _fetch_thread
        model_dir = ""
        try:
            try:
                model_dir = fetch_and_load(False)
            except RerankDigestMismatch as exc:
                # A corrupt or tampered cached copy is never repaired by retrying from the
                # cache: download once more over it, then give up (SEC-11).
                log.warning("recall rerank cached model failed its pin, downloading again: %s", exc)
                model_dir = fetch_and_load(True)
        except RerankUnavailable as exc:
            log.warning("recall rerank model unavailable, recall keeps the RRF floor: %s", exc)
        except Exception as exc:  # never let a background fetch take the server down
            log.warning("recall rerank model fetch failed (%s); recall keeps the RRF floor",
                        type(exc).__name__)
        with _FETCH_LOCK:
            # Set only after a verified load, so recall never sees a dir that cannot load.
            for waiting in _fetch_targets:
                if model_dir:
                    waiting.rerank_model_dir = model_dir
            _fetch_targets.clear()
            _fetch_thread = None
        if model_dir:
            log.info("recall rerank ready: %s on %s, %s", PINNED_REPO, device,
                     threads_label(current_threads()))
            mount = tmpfs_mount_for(model_dir)
            if mount is not None:
                log.warning(
                    "recall rerank model cache is on tmpfs (%s): every restart downloads it again; "
                    "set MENHIR_RECALL_RERANK_CACHE_DIR or HF_HOME to persistent storage", mount)

    with _FETCH_LOCK:
        _fetch_targets.append(target)
        if _fetch_thread is not None:
            return _fetch_thread
        thread = threading.Thread(target=run, name="menhir-rerank-fetch", daemon=True)
        _fetch_thread = thread
        try:
            thread.start()
        except BaseException:
            # A thread that never ran must not make every later start join it.
            _fetch_thread = None
            _fetch_targets.clear()
            raise
        return thread
