"""Live operator console for menhir -- a `rich` dashboard.

Polls the running server's ``/api/ready`` and ``/api/stats`` (loopback, no auth in
AuthMode.NONE) and live-tails ``logs/server.log``, rendering a compact status
dashboard. Memory content in the log tail is masked when privacy mode is on.

Keys: ``p`` toggle privacy redaction, ``q`` / Ctrl+C quit (leaves the server
running -- this is a monitor, not the server's lifetime).

Windows keypress uses ``msvcrt``; POSIX is a best-effort ``select`` on stdin.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from menhir.config.settings_helpers import is_loopback_host
from menhir.privacy import redact_log_line

logger = logging.getLogger(__name__)

_REFRESH_HZ = 4


def _read_key_nonblocking() -> str | None:
    """Return a single pressed key without blocking, or None. Platform-specific."""
    if os.name == "nt":
        import msvcrt

        if msvcrt.kbhit():
            try:
                return msvcrt.getwch()
            except Exception:  # noqa: BLE001
                return None
        return None
    # POSIX best-effort: non-canonical stdin read via select.
    import select
    import sys

    try:
        dr, _, _ = select.select([sys.stdin], [], [], 0)
        if dr:
            return sys.stdin.read(1)
    except Exception:  # noqa: BLE001
        return None
    return None


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return up to the last ``n`` lines of ``path`` without reading the whole file."""
    if not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 64 * 1024)
            fh.seek(size - block)
            data = fh.read(block)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n:]


def _status_dot(ready: bool, reachable: bool) -> Text:
    if ready:
        return Text("READY", style="bold green")
    if reachable:
        return Text("STARTING", style="bold yellow")
    return Text("DOWN", style="bold red")


def _render(
    *,
    host: str,
    port: int,
    ready: dict | None,
    stats: dict | None,
    reachable: bool,
    log_lines: list[str],
    redact: bool,
) -> Group:
    is_ready = bool(ready and ready.get("status") == "ready")
    header = Table.grid(expand=True)
    header.add_column(justify="left")
    header.add_column(justify="right")

    startup = (ready or {}).get("startup_mode") or (stats or {}).get("startup_mode") or "?"
    caps = (ready or {}).get("capabilities") or {}
    neo4j_up = bool(caps.get("neo4j_ready", caps.get("reads_ready", reachable)))
    left = Text.assemble(
        Text("menhir  ", style="bold"),
        _status_dot(is_ready, reachable),
        Text(f"   bind {host}:{port}   neo4j ", style="dim"),
        Text("up" if neo4j_up else "down", style="green" if neo4j_up else "red"),
        Text(f"   mode {startup}", style="dim"),
    )
    priv = Text(" PRIVACY ON ", style="bold white on red") if redact else Text(" privacy off ", style="dim")
    header.add_row(left, priv)

    metrics = Table.grid(expand=True)
    metrics.add_column(justify="left")
    if stats:
        sched = stats.get("scheduler") or {}
        sched_state = sched.get("state") or sched.get("status") or ("running" if sched else "?")
        enr = stats.get("enrichment") or {}
        enr_rate = enr.get("rate") or enr.get("per_hour") or enr.get("episodes_per_hour")
        ops = stats.get("operations") or []
        total_calls = sum(int(o.get("total_calls", 0)) for o in ops)
        metrics.add_row(
            Text.assemble(
                Text("queue ", style="dim"), Text(str(stats.get("queue_depth", 0)), style="bold cyan"),
                Text("   enrichment ", style="dim"), Text("on" if stats.get("enrichment_enabled") else "off",
                    style="green" if stats.get("enrichment_enabled") else "yellow"),
                Text("   scheduler ", style="dim"), Text(str(sched_state), style="bold"),
                Text("   ops ", style="dim"), Text(f"{total_calls} calls/{stats.get('since_hours', '?')}h", style="bold"),
                *( (Text("   rate ", style="dim"), Text(f"{enr_rate}/h", style="bold")) if enr_rate is not None else () ),
            )
        )
    else:
        metrics.add_row(Text("server unreachable — waiting…", style="yellow"))

    tail = Table.grid(expand=True)
    # One log record = one row. Long records (e.g. neo4j notification dumps) are
    # truncated with an ellipsis instead of wrapping across the whole panel.
    tail.add_column(justify="left", overflow="ellipsis", no_wrap=True)
    if log_lines:
        for ln in log_lines:
            shown = redact_log_line(ln, reveal=not redact)
            style = "red" if " ERROR " in ln or "Traceback" in ln else ("yellow" if " WARNING " in ln else "dim")
            tail.add_row(Text(shown, style=style, no_wrap=True, overflow="ellipsis"))
    else:
        tail.add_row(Text("no log lines yet", style="dim"))

    footer = Text.assemble(
        Text("  p", style="bold"), Text(" privacy   ", style="dim"),
        Text("q", style="bold"), Text(" quit (server keeps running)", style="dim"),
    )

    return Group(
        Panel(header, border_style="cyan", padding=(0, 1)),
        Panel(metrics, title="metrics", border_style="blue", padding=(0, 1)),
        Panel(tail, title="recent (server.log)", border_style="grey37", padding=(0, 1)),
        footer,
    )


async def _poll_json(
    client: httpx.AsyncClient, url: str, *, api_key: str | None = None
) -> dict | None:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        resp = await client.get(url, timeout=2.0, headers=headers)
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    return None


#: Scheme override for a dashboard pointed at a TLS-terminating backend.
_SCHEME_ENV = "MENHIR_CONSOLE_SCHEME"


def _dashboard_base_url(host: str, port: int, *, api_key: str | None) -> str:
    """Build the dashboard base URL, refusing to put a bearer key on a plaintext LAN wire (CF-42).

    The scheme was a hard-coded ``http://`` literal with no TLS option and no warning, while
    ``_poll_json`` attaches ``Authorization: Bearer <key>`` to every request. On loopback that is
    harmless. The service is deliberately LAN-exposable (``MENHIR_API_HOST=0.0.0.0``) and
    ``--host`` is an ordinary user-facing flag, so pointing the dashboard at a non-loopback
    address put a bearer key -- possibly the operator one, per CF-41 -- in the clear.

    Loopback keeps ``http`` so the default workflow is unchanged. A remote host with a key
    defaults to ``https``; set ``MENHIR_CONSOLE_SCHEME=http`` to override deliberately, which
    warns rather than failing silently. A remote host with NO key stays on ``http``: there is no
    credential to protect and refusing would break an unauthenticated dashboard that works today.
    """
    override = (os.getenv(_SCHEME_ENV) or "").strip().lower()
    if override in {"http", "https"}:
        if override == "http" and api_key and not is_loopback_host(host):
            logger.warning(
                "%s=http with an API key against non-loopback host %s: the bearer key is sent "
                "in the clear.",
                _SCHEME_ENV,
                host,
            )
        return f"{override}://{host}:{port}"

    if api_key and not is_loopback_host(host):
        logger.warning(
            "Dashboard is sending an API key to non-loopback host %s; using https. "
            "Set %s=http to override if the backend really is plaintext.",
            host,
            _SCHEME_ENV,
        )
        return f"https://{host}:{port}"

    return f"http://{host}:{port}"


async def run_console(
    *,
    host: str,
    port: int,
    log_file: Path,
    interval: float,
    redact: bool,
    api_key: str | None = None,
    log_lines: int = 14,
) -> None:
    """Run the live dashboard loop until the user quits."""
    base = _dashboard_base_url(host, port, api_key=api_key)
    async with httpx.AsyncClient() as client:
        with Live(auto_refresh=False, screen=False, refresh_per_second=_REFRESH_HZ) as live:
            while True:
                key = _read_key_nonblocking()
                if key:
                    lowered = key.lower()
                    if lowered == "q" or key in ("\x03", "\x1b"):
                        break
                    if lowered == "p":
                        redact = not redact

                ready = await _poll_json(client, f"{base}/api/ready", api_key=api_key)
                stats = await _poll_json(client, f"{base}/api/stats", api_key=api_key)
                reachable = ready is not None or stats is not None
                tail = _tail_lines(log_file, log_lines)

                live.update(
                    _render(
                        host=host,
                        port=port,
                        ready=ready,
                        stats=stats,
                        reachable=reachable,
                        log_lines=tail,
                        redact=redact,
                    ),
                    refresh=True,
                )
                await asyncio.sleep(interval)
