"""P2A transport probe: how large a chunk body survives the path to a Menhir?

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2A).

`SnapshotLimits.chunk_bytes` (256 KiB) and `max_chunk_bytes` (2 MiB) are the two numbers in the
frozen protocol that were never measured -- they were picked. This script measures them, against
whatever URL it is pointed at, so the same run can be compared across two very different paths:

    # the local stack: an upper bound on what the code can do
    python scripts/probe/p2a_transport_probe.py --label local

    # through a real Cloudflare ingress: what a deployment actually permits
    python scripts/probe/p2a_transport_probe.py --base-url https://sim-ingest.example.dev --label tunnel

**The measurement rests on one distinction.** For each body size the probe asks not "did it work?"
but *who said no*:

* a structured MCP reply -- success OR a refusal carrying a `code` -- proves the bytes traversed
  the ingress and reached the handler. The ingress is not the limit at that size.
* an HTTP status, a reset, or a timeout with no MCP reply proves they did not. That is the ingress
  limit, and it is the number P2A exists to find.

This is why the probe does not need the server to accept oversized chunks, and why it must not
raise the server's own ceiling to run: a server refusal is a *successful* traversal for these
purposes. Conflating the two is the one way to get a confidently wrong answer here, because a
413 from an edge and a refusal from the handler look identical if you only check `ok`.

**Sizes are of the decoded payload; the wire body is larger.** Chunks travel base64-encoded, so a
1 MiB chunk becomes a ~1.33 MiB body inside a JSON envelope. Ingress limits apply to the wire
body. Both numbers are reported, and the ceiling is stated in wire bytes, because that is the units
the limit is actually enforced in.

Nothing here writes to a graph. Every upload is aborted immediately after its chunk, so a run
leaves no staging state behind even when it fails midway.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

KIB = 1024
MIB = 1024 * 1024

#: Doubling past the protocol's 2 MiB ceiling on purpose. The interesting reading is not where the
#: server stops accepting -- that is a constant we chose -- but where the *path* stops delivering,
#: which may be above or below it.
DEFAULT_SIZES = [
    64 * KIB,
    128 * KIB,
    256 * KIB,
    512 * KIB,
    1 * MIB,
    2 * MIB,
    4 * MIB,
    8 * MIB,
    16 * MIB,
]

#: Three passes, not one. P2A needs a size that works *reliably*; a single success at a size that
#: fails one time in three is worse than a lower ceiling, and one pass cannot tell them apart.
DEFAULT_TRIALS = 3

#: An honest product agent. See `ProbeClient.__init__` for why the default urllib one is unusable
#: through a real Cloudflare zone.
DEFAULT_USER_AGENT = "menhir-p2a-probe/0.1 (+snapshot transport measurement)"

# Outcome classes. The first two mean the ingress delivered the body.
DELIVERED_OK = "delivered_ok"
DELIVERED_REFUSED = "delivered_server_refused"
BLOCKED_HTTP = "blocked_http_status"
BLOCKED_TRANSPORT = "blocked_transport"
PROBE_ERROR = "probe_error"

DELIVERED = frozenset({DELIVERED_OK, DELIVERED_REFUSED})


@dataclass
class Trial:
    size_bytes: int
    wire_bytes: int
    outcome: str
    detail: str
    elapsed_s: float
    http_status: int | None = None
    server_code: str | None = None
    #: False when the server refused a chunk plan this large and the body was sent against the
    #: default plan instead. Such a trial measures the path only, never server acceptance.
    plan_negotiated: bool = True


@dataclass
class SizeResult:
    size_bytes: int
    wire_bytes: int
    trials: list[Trial] = field(default_factory=list)

    @property
    def delivered(self) -> int:
        return sum(1 for t in self.trials if t.outcome in DELIVERED)

    @property
    def reliable(self) -> bool:
        """Every trial delivered. Deliberately not a majority: a ceiling is the largest size that
        never failed, because a 'mostly works' chunk size produces intermittent upload failures in
        the field that are near-impossible to attribute."""
        return bool(self.trials) and self.delivered == len(self.trials)

    def median_elapsed(self) -> float:
        times = [t.elapsed_s for t in self.trials if t.outcome in DELIVERED]
        return statistics.median(times) if times else float("nan")

    def throughput_mib_s(self) -> float:
        median = self.median_elapsed()
        if math.isnan(median) or not median:
            return float("nan")
        return (self.wire_bytes / MIB) / median


class ProbeClient:
    """Minimal MCP-over-HTTP caller.

    Hand-rolled for the same reason the remote-sim tests are: a client library that can
    short-circuit to an in-process server would measure nothing. This one only ever speaks over a
    socket, and it surfaces the difference between an HTTP error and a transport failure instead of
    flattening both into an exception.
    """

    def __init__(
        self, base_url: str, operator_key: str, timeout: float, user_agent: str = ""
    ) -> None:
        self.endpoint = f"{base_url.rstrip('/')}/mcp-http"
        self.operator_key = operator_key
        self.timeout = timeout
        # Not cosmetic. Behind a Cloudflare zone with Browser Integrity Check on, urllib's default
        # `Python-urllib/x.y` signature is refused at the edge with a 403 (error 1010) and NOTHING
        # reaches the origin -- which reads exactly like a server outage from the client side. The
        # shipping client must send an honest product agent for the same reason.
        self.user_agent = user_agent or DEFAULT_USER_AGENT

    def _post(self, body: dict[str, Any]) -> tuple[str, str, int | None, str]:
        """Return (outcome, payload_or_detail, http_status, raw_text)."""
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.operator_key}",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return ("http_ok", "", response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # An HTTP status means something in the path answered. If it is an edge or proxy, this
            # is the limit being measured; the body is captured because 413 vs 502 vs 520
            # distinguishes a size rule from an origin failure.
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 -- the body is diagnostic only; never fail the probe
                detail = ""
            return ("http_error", detail, exc.code, "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ("transport_error", f"{type(exc).__name__}: {exc}", None, "")

    def call(self, tool: str, arguments: dict | None) -> dict[str, Any]:
        """Call a tool and classify the result. Never raises for an expected failure mode."""
        if arguments is None:
            body = {"jsonrpc": "2.0", "id": 1, "method": tool, "params": {}}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        wire_bytes = len(json.dumps(body).encode("utf-8"))
        started = time.perf_counter()
        kind, detail, status, raw = self._post(body)
        elapsed = time.perf_counter() - started

        if kind == "http_error":
            return {
                "outcome": BLOCKED_HTTP,
                "detail": f"HTTP {status}: {detail[:200]}",
                "http_status": status,
                "elapsed_s": elapsed,
                "wire_bytes": wire_bytes,
            }
        if kind == "transport_error":
            return {
                "outcome": BLOCKED_TRANSPORT,
                "detail": detail,
                "http_status": None,
                "elapsed_s": elapsed,
                "wire_bytes": wire_bytes,
            }

        try:
            parsed = _parse_mcp_body(raw)
        except ValueError as exc:
            return {
                "outcome": PROBE_ERROR,
                "detail": f"unparseable MCP body: {exc}",
                "http_status": status,
                "elapsed_s": elapsed,
                "wire_bytes": wire_bytes,
            }

        payload = _tool_payload(parsed)
        server_code = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            server_code = str((payload.get("error") or {}).get("code") or "unknown")
        outcome = DELIVERED_REFUSED if server_code else DELIVERED_OK
        return {
            "outcome": outcome,
            "detail": server_code or "accepted",
            "http_status": status,
            "elapsed_s": elapsed,
            "wire_bytes": wire_bytes,
            "payload": payload,
            "server_code": server_code,
        }


def _parse_mcp_body(body: str) -> dict:
    """Accept a plain JSON body or an SSE frame; the transport may use either."""
    text = body.strip()
    if text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[len("data:") :].strip()
                break
    if not text:
        raise ValueError("empty body")
    return json.loads(text)


def _tool_payload(parsed: dict) -> Any:
    """Unwrap the tool's JSON out of the MCP envelope, tolerating either shape."""
    result = parsed.get("result")
    if not isinstance(result, dict):
        return parsed
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                return json.loads(first["text"])
            except ValueError:
                return first["text"]
    return result


def _probe_one(client: ProbeClient, size: int, project_key: str) -> Trial:
    """Send one chunk of `size` decoded bytes and classify who answered.

    A fresh upload per trial: the receiver treats a conflicting re-put at the same index as a
    failure of the whole upload, so reusing one would make later trials measure that rule instead
    of the transport. Each upload is aborted before returning, so the per-principal concurrency cap
    is never the thing that fails.
    """
    # Ask for a chunk plan whose chunk IS this size, so a success means the server genuinely
    # accepted a chunk this large. Asking for the default 256 KiB plan instead would make every
    # larger probe fail on `chunk_length_mismatch` -- a plan check, not a size limit -- and the run
    # would report the transport as fine while never once testing acceptance above 256 KiB.
    begin = client.call(
        "begin_project_snapshot",
        {"project_key": project_key, "declared_bytes": size, "chunk_bytes": size},
    )
    negotiated = True
    if begin["outcome"] == DELIVERED_REFUSED:
        # Above the server's own `max_chunk_bytes` the plan is refused before any large body is
        # sent. Fall back to the default plan and send the oversized chunk anyway: the question at
        # these sizes is whether the PATH carries the bytes, and a refusal from the handler still
        # answers it. The trial is tagged so the two cases are never read as the same result.
        negotiated = False
        begin = client.call(
            "begin_project_snapshot",
            {"project_key": project_key, "declared_bytes": size, "chunk_bytes": None},
        )
    if begin["outcome"] not in DELIVERED:
        return Trial(
            size,
            begin["wire_bytes"],
            begin["outcome"],
            f"begin failed: {begin['detail']}",
            begin["elapsed_s"],
            begin.get("http_status"),
            begin.get("server_code"),
        )
    payload = begin.get("payload")
    if not isinstance(payload, dict) or not payload.get("upload_id"):
        return Trial(
            size,
            begin["wire_bytes"],
            PROBE_ERROR,
            f"begin returned no upload_id: {str(payload)[:160]}",
            begin["elapsed_s"],
        )
    upload_id = payload["upload_id"]

    try:
        blob = os.urandom(
            size
        )  # incompressible: a compressing proxy must not flatter the result
        result = client.call(
            "put_project_snapshot_chunk",
            {
                "upload_id": upload_id,
                "index": 0,
                "data_b64": base64.b64encode(blob).decode("ascii"),
                "declared_len": size,
                "digest": hashlib.sha256(blob).hexdigest(),
            },
        )
        return Trial(
            size_bytes=size,
            wire_bytes=result["wire_bytes"],
            outcome=result["outcome"],
            detail=result["detail"],
            elapsed_s=result["elapsed_s"],
            http_status=result.get("http_status"),
            server_code=result.get("server_code"),
            plan_negotiated=negotiated,
        )
    finally:
        # Best effort and deliberately unchecked: if the abort itself cannot get through, the
        # staging TTL reclaims the upload, and failing the probe here would discard the
        # measurement we just paid for.
        client.call("abort_project_snapshot", {"upload_id": upload_id})


def run(
    base_url: str,
    label: str,
    sizes: list[int],
    trials: int,
    operator_key: str,
    timeout: float,
    user_agent: str = "",
) -> dict[str, Any]:
    client = ProbeClient(base_url, operator_key, timeout, user_agent)

    probe = client.call("tools/list", None)
    if probe["outcome"] not in DELIVERED:
        raise SystemExit(f"cannot reach MCP at {base_url}: {probe['detail']}")

    results: list[SizeResult] = []
    print(f"\n  P2A transport probe -- label={label} url={base_url} trials={trials}\n")
    header = f"  {'decoded':>9}  {'wire':>9}  {'delivered':>10}  {'median':>8}  {'MiB/s':>7}  outcome"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for size in sizes:
        entry = SizeResult(size_bytes=size, wire_bytes=0)
        for _ in range(trials):
            trial = _probe_one(client, size, project_key=f"p2a-probe-{label}")
            entry.trials.append(trial)
            entry.wire_bytes = trial.wire_bytes or entry.wire_bytes
        results.append(entry)

        outcomes = {t.outcome for t in entry.trials}
        detail = min(t.detail for t in entry.trials)
        median = entry.median_elapsed()
        print(
            f"  {_human(size):>9}  {_human(entry.wire_bytes):>9}  "
            f"{entry.delivered:>4}/{len(entry.trials):<5}  "
            f"{median:>7.3f}s  {entry.throughput_mib_s():>7.1f}  "
            f"{'/'.join(sorted(outcomes))} ({detail[:60]})"
        )
        # Stop climbing once the path itself refuses: everything above is the same answer, and
        # each larger trial costs real seconds against a real edge.
        if not entry.reliable and outcomes & {BLOCKED_HTTP, BLOCKED_TRANSPORT}:
            print(
                f"\n  stopping the sweep: the path rejected {_human(size)} before the handler saw it"
            )
            break

    return _summarize(base_url, label, trials, results)


def _summarize(
    base_url: str, label: str, trials: int, results: list[SizeResult]
) -> dict[str, Any]:
    delivered = [r for r in results if r.reliable]
    ceiling = delivered[-1] if delivered else None
    blocked = [
        r
        for r in results
        if any(t.outcome in {BLOCKED_HTTP, BLOCKED_TRANSPORT} for t in r.trials)
    ]
    # Genuine acceptance requires the plan to have been negotiated at this size; a body sent
    # against a smaller default plan can only ever be refused, so counting it would understate the
    # server's real ceiling.
    accepted = [
        r
        for r in results
        if r.trials
        and all(t.outcome == DELIVERED_OK and t.plan_negotiated for t in r.trials)
    ]

    summary = {
        "label": label,
        "base_url": base_url,
        "trials_per_size": trials,
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # The headline: the largest body every trial of which reached the handler.
        "max_delivered_decoded_bytes": ceiling.size_bytes if ceiling else None,
        "max_delivered_wire_bytes": ceiling.wire_bytes if ceiling else None,
        # Separate, and not the same question: the largest the server itself said yes to.
        "max_server_accepted_decoded_bytes": accepted[-1].size_bytes
        if accepted
        else None,
        "first_blocked_decoded_bytes": blocked[0].size_bytes if blocked else None,
        "path_limited": bool(blocked),
        "sizes": [
            {
                **{k: v for k, v in asdict(r).items() if k != "trials"},
                "delivered": r.delivered,
                "reliable": r.reliable,
                "median_elapsed_s": _round_or_none(r.median_elapsed(), 4),
                "throughput_mib_s": _round_or_none(r.throughput_mib_s(), 2),
                "trials": [asdict(t) for t in r.trials],
            }
            for r in results
        ],
    }

    print("\n  Summary")
    if ceiling:
        print(
            f"    largest body that always reached the handler: {_human(ceiling.size_bytes)} "
            f"decoded / {_human(ceiling.wire_bytes)} on the wire"
        )
    else:
        print("    nothing was delivered reliably at any probed size")
    if accepted:
        print(
            f"    largest the server itself accepted:           {_human(accepted[-1].size_bytes)}"
        )
    if blocked:
        print(
            f"    first size the path itself rejected:          {_human(blocked[0].size_bytes)}"
        )
    else:
        print(
            "    the path rejected nothing probed -- the ceiling here is the server's, not the "
            "ingress's"
        )
    return summary


def _round_or_none(value: float, digits: int) -> float | None:
    """NaN means "no delivered trial at this size". JSON has no NaN, so it becomes null."""
    return None if math.isnan(value) else round(value, digits)


def _human(n: int) -> str:
    if n >= MIB:
        value = n / MIB
        return f"{value:.0f}MiB" if value == int(value) else f"{value:.2f}MiB"
    if n >= KIB:
        value = n / KIB
        return f"{value:.0f}KiB" if value == int(value) else f"{value:.2f}KiB"
    return f"{n}B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8099",
        help="Menhir base URL. The same script measures the local stack and a "
        "real ingress; only this changes.",
    )
    parser.add_argument("--label", default="local", help="Tag recorded with the run.")
    parser.add_argument(
        "--operator-key", default=os.getenv("MENHIR_OPERATOR_KEY", "sim-operator-key")
    )
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--sizes", default="", help="Comma-separated decoded byte sizes."
    )
    parser.add_argument("--out", default="", help="Write the JSON result here.")
    parser.add_argument(
        "--user-agent",
        default="",
        help="Override the client agent. Sending urllib's default through a "
        "Cloudflare zone with Browser Integrity Check on returns 403.",
    )
    args = parser.parse_args(argv)

    sizes = (
        [int(s) for s in args.sizes.split(",") if s.strip()]
        if args.sizes
        else DEFAULT_SIZES
    )
    summary = run(
        args.base_url,
        args.label,
        sizes,
        args.trials,
        args.operator_key,
        args.timeout,
        args.user_agent,
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
