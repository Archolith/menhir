"""P2A soak: what does sustained chunking cost, and does anything grow that shouldn't?

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2A).
Companion to ``p2a_transport_probe.py``, which answers a different question.

The probe asks **how large** a single body can be. That is a ceiling, and one request finds it.
This asks **what a real upload costs repeatedly**: the tail of the latency distribution, whether
staging bytes come back down, and what the server's memory does while a bundle streams through it.
None of those are visible in a single call, which is why the probe could not answer them and why
running only the probe left the P2A gate open.

    python scripts/probe/p2a_soak.py --label local
    python scripts/probe/p2a_soak.py --base-url https://host --label tunnel

Three things are sampled, and two of them cannot be read from the client:

* **Latency per chunk**, client-side. Reported as p50/p95/p99 and max. p95 is the number the gate
  wants: a median hides the stall that makes a 64-chunk upload feel broken.
* **Peak container memory**, via ``docker stats``. A receiver that buffers a whole bundle rather
  than streaming it looks identical from the client until the bundle is big enough to kill it.
* **Staging bytes on disk**, via ``du`` inside the container. The question is not the peak but
  whether it RETURNS: bytes that survive a completed upload are the leak that fills a disk weeks
  later, and no client-side assertion can see them.

Sampling runs on a thread rather than between requests, so a stall shows up in the samples instead
of being stepped over. Both samplers shell out to docker and are best-effort: a failure there
records a gap and never fails the soak, because the latency data is the expensive part.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

MIB = 1024 * 1024
DEFAULT_USER_AGENT = "menhir-p2a-soak/0.1 (+snapshot transport measurement)"

#: Matches the measured default so the soak exercises what ships, not a size nobody sends.
DEFAULT_CHUNK_BYTES = 1 * MIB
DEFAULT_BUNDLE_BYTES = 16 * MIB
DEFAULT_BUNDLES = 4


@dataclass
class Sample:
    at: float
    mem_bytes: int | None
    staging_bytes: int | None
    note: str = ""


@dataclass
class ChunkTiming:
    bundle: int
    index: int
    elapsed_s: float
    ok: bool
    detail: str = ""


@dataclass
class SoakResult:
    label: str
    base_url: str
    chunk_bytes: int
    bundle_bytes: int
    bundles: int
    timings: list[ChunkTiming] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    upload_states: list[str] = field(default_factory=list)
    staging_baseline_bytes: int | None = None
    staging_after_cleanup_bytes: int | None = None


class _ResourceSampler(threading.Thread):
    """Poll container memory and staging size until stopped.

    A thread, not a between-requests hook: the interesting moment is a stall, and a sampler that
    only runs between requests is precisely the one that cannot see one.
    """

    def __init__(
        self, container: str, staging_path: str, interval_s: float = 2.0
    ) -> None:
        super().__init__(daemon=True)
        self.container = container
        self.staging_path = staging_path
        self.interval_s = interval_s
        self.samples: list[Sample] = []
        # NOT `_stop`: threading.Thread has its own internal `_stop()` method, and shadowing it
        # with an Event makes `join()` raise "'Event' object is not callable" after the work is
        # already done -- losing the run rather than failing it early.
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            mem, staging, note = None, None, ""
            try:
                mem = _container_mem_bytes(self.container)
            except Exception as exc:  # noqa: BLE001 -- diagnostic only; never fail the soak
                note = f"mem: {type(exc).__name__}"
            try:
                staging = _staging_bytes(self.container, self.staging_path)
            except Exception as exc:  # noqa: BLE001
                note = (note + f" staging: {type(exc).__name__}").strip()
            self.samples.append(Sample(time.time(), mem, staging, note))
            self._stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self._stop_event.set()


def _run(args: list[str], timeout: float = 20.0) -> str:
    out = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=True
    )
    return out.stdout.strip()


def _container_mem_bytes(container: str) -> int:
    raw = _run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container]
    )
    used = raw.split("/")[0].strip()
    return _parse_size(used)


def _staging_bytes(container: str, path: str) -> int:
    # `du -sb` on a missing directory exits non-zero; the receiver creates it lazily, so a fresh
    # stack legitimately has none and that reads as zero rather than as an error.
    raw = _run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            f"du -sb {path} 2>/dev/null | cut -f1 || echo 0",
        ]
    )
    return int(raw.strip() or 0)


def _parse_size(text: str) -> int:
    units = {
        "B": 1,
        "KIB": 1024,
        "MIB": MIB,
        "GIB": 1024 * MIB,
        "KB": 1000,
        "MB": 10**6,
        "GB": 10**9,
    }
    text = text.strip().upper()
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * mult)
    return int(float(text))


class SoakClient:
    def __init__(self, base_url: str, operator_key: str, timeout: float) -> None:
        self.endpoint = f"{base_url.rstrip('/')}/mcp-http"
        self.operator_key = operator_key
        self.timeout = timeout

    def call(self, tool: str, arguments: dict | None) -> tuple[bool, Any, float]:
        if arguments is None:
            body = {"jsonrpc": "2.0", "id": 1, "method": tool, "params": {}}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.operator_key}",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}", time.perf_counter() - started
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}", time.perf_counter() - started
        elapsed = time.perf_counter() - started

        text = raw.strip()
        if text.startswith(("event:", "data:")):
            for line in text.splitlines():
                if line.startswith("data:"):
                    text = line[len("data:") :].strip()
                    break
        parsed = json.loads(text)
        result = parsed.get("result")
        payload: Any = parsed
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                inner = content[0].get("text")
                if isinstance(inner, str):
                    try:
                        payload = json.loads(inner)
                    except ValueError:
                        payload = inner
            else:
                payload = result
        ok = not (isinstance(payload, dict) and payload.get("ok") is False)
        return ok, payload, elapsed


def run_soak(
    base_url: str,
    label: str,
    operator_key: str,
    bundles: int,
    bundle_bytes: int,
    chunk_bytes: int,
    container: str,
    staging_path: str,
    timeout: float,
) -> SoakResult:
    client = SoakClient(base_url, operator_key, timeout)
    ok, payload, _ = client.call("tools/list", None)
    if not ok:
        raise SystemExit(f"cannot reach MCP at {base_url}: {payload}")

    result = SoakResult(label, base_url, chunk_bytes, bundle_bytes, bundles)
    try:
        result.staging_baseline_bytes = _staging_bytes(container, staging_path)
    except Exception:  # noqa: BLE001
        result.staging_baseline_bytes = None

    sampler = _ResourceSampler(container, staging_path)
    sampler.start()
    chunks_per_bundle = math.ceil(bundle_bytes / chunk_bytes)
    print(
        f"\n  P2A soak -- label={label} url={base_url}\n"
        f"  {bundles} bundles x {bundle_bytes // MIB} MiB at {chunk_bytes // MIB} MiB chunks "
        f"= {bundles * chunks_per_bundle} chunk calls\n"
    )

    try:
        for b in range(bundles):
            ok, payload, _ = client.call(
                "begin_project_snapshot",
                {
                    "project_key": f"p2a-soak-{label}-{b}",
                    "declared_bytes": bundle_bytes,
                    "chunk_bytes": chunk_bytes,
                },
            )
            if not ok or not isinstance(payload, dict) or not payload.get("upload_id"):
                print(f"  bundle {b}: begin refused: {str(payload)[:160]}")
                continue
            upload_id = payload["upload_id"]

            for i in range(chunks_per_bundle):
                remaining = bundle_bytes - (i * chunk_bytes)
                size = min(chunk_bytes, remaining)
                blob = os.urandom(size)
                ok, payload, elapsed = client.call(
                    "put_project_snapshot_chunk",
                    {
                        "upload_id": upload_id,
                        "index": i,
                        "data_b64": base64.b64encode(blob).decode("ascii"),
                        "declared_len": size,
                        "digest": hashlib.sha256(blob).hexdigest(),
                    },
                )
                result.timings.append(
                    ChunkTiming(b, i, elapsed, ok, "" if ok else str(payload)[:120])
                )

            ok, payload, _ = client.call(
                "get_project_snapshot_status", {"upload_id": upload_id}
            )
            state = payload.get("state") if isinstance(payload, dict) else "unknown"
            result.upload_states.append(str(state))
            print(f"  bundle {b}: {chunks_per_bundle} chunks -> state={state}")

            # Abort rather than leave it in terminal retention: the point of the disk reading is
            # whether bytes come back, and an upload deliberately retained for an hour would make
            # a real leak and a working retention window look the same.
            client.call("abort_project_snapshot", {"upload_id": upload_id})
    finally:
        sampler.stop()
        sampler.join(timeout=10)
        result.samples = sampler.samples

    time.sleep(2)  # let the abort's unlink land before the final reading
    try:
        result.staging_after_cleanup_bytes = _staging_bytes(container, staging_path)
    except Exception:  # noqa: BLE001
        result.staging_after_cleanup_bytes = None
    return result


def summarize(result: SoakResult) -> dict[str, Any]:
    good = [t.elapsed_s for t in result.timings if t.ok]
    failed = [t for t in result.timings if not t.ok]
    mems = [s.mem_bytes for s in result.samples if s.mem_bytes is not None]
    stagings = [s.staging_bytes for s in result.samples if s.staging_bytes is not None]

    def pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        k = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
        return round(ordered[k], 4)

    summary = {
        "label": result.label,
        "base_url": result.base_url,
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunk_bytes": result.chunk_bytes,
        "bundle_bytes": result.bundle_bytes,
        "bundles": result.bundles,
        "chunk_calls": len(result.timings),
        "chunk_failures": len(failed),
        "failure_details": [f.detail for f in failed[:5]],
        "latency_s": {
            "p50": pct(good, 50),
            "p95": pct(good, 95),
            "p99": pct(good, 99),
            "max": round(max(good), 4) if good else None,
            "mean": round(statistics.fmean(good), 4) if good else None,
        },
        "upload_states": result.upload_states,
        "peak_container_mem_bytes": max(mems) if mems else None,
        "mem_samples": len(mems),
        "peak_staging_bytes": max(stagings) if stagings else None,
        "staging_baseline_bytes": result.staging_baseline_bytes,
        "staging_after_cleanup_bytes": result.staging_after_cleanup_bytes,
    }

    lat = summary["latency_s"]
    print("\n  Summary")
    print(
        f"    chunk calls              {summary['chunk_calls']} ({summary['chunk_failures']} failed)"
    )
    print(f"    latency p50/p95/p99      {lat['p50']}s / {lat['p95']}s / {lat['p99']}s")
    print(f"    latency max              {lat['max']}s")
    if summary["peak_container_mem_bytes"]:
        print(
            f"    peak container memory    {summary['peak_container_mem_bytes'] / MIB:.1f} MiB"
        )
    print(
        f"    staging bytes            baseline {result.staging_baseline_bytes}"
        f" -> peak {summary['peak_staging_bytes']}"
        f" -> after cleanup {result.staging_after_cleanup_bytes}"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8099")
    parser.add_argument("--label", default="local")
    parser.add_argument(
        "--operator-key", default=os.getenv("MENHIR_OPERATOR_KEY", "sim-operator-key")
    )
    parser.add_argument("--bundles", type=int, default=DEFAULT_BUNDLES)
    parser.add_argument("--bundle-bytes", type=int, default=DEFAULT_BUNDLE_BYTES)
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--container", default="menhir-remote-sim-app")
    parser.add_argument("--staging-path", default="/app/state/snapshot-staging")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    result = run_soak(
        args.base_url,
        args.label,
        args.operator_key,
        args.bundles,
        args.bundle_bytes,
        args.chunk_bytes,
        args.container,
        args.staging_path,
        args.timeout,
    )
    summary = summarize(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
