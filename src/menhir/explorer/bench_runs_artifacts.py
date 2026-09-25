"""Checkpoint parsing, run discovery, and provenance readers for the bench-run explorer.

Extracted from ``menhir.explorer.bench_runs``, which re-exports every symbol.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .bench_runs_io import _is_safe_component, _read_json, _safe_int

logger = logging.getLogger(__name__)

def _parse_checkpoint_record(rec: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(rec, dict):
        return None
    result = rec.get("result")
    if isinstance(result, dict):
        task_id = str(result.get("task_id") or rec.get("task_id") or "")
        correct = bool(result.get("correct"))
        raw_score = result.get("score")
        score = float(raw_score) if raw_score is not None else (1.0 if correct else 0.0)
        return {
            "arm": str(rec.get("arm", "")),
            "task_id": task_id,
            "correct": correct,
            "score": score,
            "input_tokens": _safe_int(result.get("input_tokens")),
            "output_tokens": _safe_int(result.get("output_tokens")),
            "response_text": str(result.get("response_text", "")),
            "recalled": str(result.get("recalled", "")),
            "gold": str(result.get("gold", "")),
            "question": str(result.get("question", "")),
        }
    correct = bool(rec.get("correct"))
    raw_score = rec.get("score")
    score = float(raw_score) if raw_score is not None else (1.0 if correct else 0.0)
    return {
        "arm": str(rec.get("arm", "")),
        "task_id": str(rec.get("task_id", "")),
        "correct": correct,
        "score": score,
        "input_tokens": _safe_int(rec.get("input_tokens")),
        "output_tokens": _safe_int(rec.get("output_tokens")),
        "response_text": str(rec.get("response_text", "")),
        "recalled": str(rec.get("recalled", "")),
        "gold": str(rec.get("gold", "")),
        "question": str(rec.get("question", "")),
    }


def _read_checkpoint_scores(path: Path) -> dict[str, list[dict[str, Any]]]:
    scores: dict[str, list[dict[str, Any]]] = {}
    if path.is_symlink():
        return scores
    if not path.exists():
        return scores
    try:
        raw = path.read_text(encoding="utf-8")
        raw = raw.strip()
        if not raw:
            return scores
        if raw.startswith("["):
            try:
                records = json.loads(raw)
                if isinstance(records, list):
                    for rec in records:
                        parsed = _parse_checkpoint_record(rec)
                        if parsed and parsed["task_id"]:
                            ns = parsed["task_id"]
                            if not ns.startswith("lme-"):
                                ns = f"lme-{ns}"
                            scores.setdefault(ns, []).append(parsed)
                    return scores
            except json.JSONDecodeError:
                pass
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            parsed = _parse_checkpoint_record(rec)
            if parsed and parsed["task_id"]:
                ns = parsed["task_id"]
                if not ns.startswith("lme-"):
                    ns = f"lme-{ns}"
                scores.setdefault(ns, []).append(parsed)
    except OSError as exc:
        logger.debug("Could not read checkpoint %s: %s", path, exc)
    return scores


def _discover_run_dirs(results_root: Path) -> dict[str, Path]:
    """Discover run directories mapping run_id -> relative path from root.

    Rejects symlinked entries. Duplicate run IDs are logged as ambiguous
    and both are excluded.
    """
    if not results_root.is_dir() or results_root.is_symlink():
        return {}
    root_resolved = results_root.resolve()
    found: dict[str, list[Path]] = {}

    for entry in sorted(root_resolved.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if not _is_safe_component(entry.name):
            continue
        # Pattern 1: direct subdirectory
        manifest = _read_json(entry / "manifest.json")
        if isinstance(manifest, list):
            rid = entry.name
            rel = Path(entry.name)
            found.setdefault(rid, []).append(rel)
            continue
        # Pattern 2: one level deeper
        for sub in sorted(entry.iterdir()):
            if sub.is_symlink():
                continue
            if not sub.is_dir():
                continue
            if not _is_safe_component(sub.name):
                continue
            sub_manifest = _read_json(sub / "manifest.json")
            if isinstance(sub_manifest, list):
                rid = sub.name
                rel = Path(entry.name) / sub.name
                found.setdefault(rid, []).append(rel)

    # Exclude ambiguous duplicates
    result: dict[str, Path] = {}
    for rid, paths in found.items():
        if len(paths) > 1:
            logger.warning("Ambiguous run_id %r found at %s; excluding both", rid, paths)
            continue
        result[rid] = paths[0]

    return result


def _read_provenance(provenance_path: Path) -> dict[str, Any]:
    data = _read_json(provenance_path)
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        return data[0]
    return {}


_IDENTITY_FIELDS = frozenset({
    "variant", "arm", "extract_model", "menhir_commit", "bench_commit",
    "started_at", "LONGMEMEVAL_VARIANT", "dataset", "namespace_prefix",
    "segmentation", "fixture_sha256", "fixture_count", "container", "volume",
})


def _get_identity(provenance: dict[str, Any]) -> dict[str, Any]:
    """Resolve identity, merging latest_attempt fields over top-level identity.

    In the current provenance schema, latest_attempt carries identity fields
    directly (menhir_commit, bench_commit, variant, etc.) rather than under
    a nested .identity key. Fields from latest_attempt win over top-level.
    """
    base: dict[str, Any] = {}
    identity = provenance.get("identity")
    if isinstance(identity, dict):
        for k in _IDENTITY_FIELDS:
            if k in identity:
                base[k] = identity[k]
    latest = provenance.get("latest_attempt")
    if isinstance(latest, dict):
        for k in _IDENTITY_FIELDS:
            if k in latest:
                base[k] = latest[k]
    return base


def _get_provenance_value(provenance: dict[str, Any], key: str) -> Any:
    """Resolve a provenance field with latest-attempt precedence.

    Run writers have used three placements over time: top-level fields, the nested
    ``identity`` object, and direct fields on ``latest_attempt``. Read all three,
    while retaining the same latest-attempt precedence as ``_get_identity``.
    """
    value = provenance.get(key)
    identity = provenance.get("identity")
    if isinstance(identity, dict) and key in identity:
        value = identity[key]
    latest = provenance.get("latest_attempt")
    if isinstance(latest, dict) and key in latest:
        value = latest[key]
    return value


def _find_checkpoint_files(run_dir: Path) -> list[Path]:
    """Find checkpoint score files, rejecting symlinks."""
    results: list[Path] = []
    for pat in (".checkpoint_*.jsonl",):
        for cp in run_dir.glob(pat):
            if not cp.is_symlink():
                results.append(cp)
    for child in run_dir.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        for pat in (".checkpoint_*.jsonl",):
            for cp in child.glob(pat):
                if not cp.is_symlink():
                    results.append(cp)
    rm = run_dir / "run_manifest.json"
    if rm.exists() and not rm.is_symlink():
        results.append(rm)
    return results
