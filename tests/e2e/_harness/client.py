"""A stock MCP stdio client, with every request and response recorded.

"Stock" is the point. Phase C says these tests "must launch Menhir the way a real local
MCP client does and exercise stdio, not call repositories/services directly", so this
wraps the official ``mcp`` SDK client rather than Menhir's own helpers. If Menhir's
handler registration, schema emission or framing is broken in a way its internal tests
paper over, this is the layer that notices.

The bridge is spawned exactly as ``README.md:298`` documents it::

    python -m menhir.mcp.server

from the installed venv, with the harness environment, and pointed at the already
running backend through ``MENHIR_BACKEND_URL``.
"""

from __future__ import annotations

import asyncio as _asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.e2e._harness.config import E2EConfig, child_environment
from tests.e2e._harness.evidence import LaneEvidence

__all__ = ["RecordingClient", "stdio_session", "wait_for_project_indexed"]


class RecordingClient:
    """Thin wrapper over ``ClientSession`` that appends every exchange to the transcript."""

    def __init__(self, session: ClientSession, evidence: LaneEvidence) -> None:
        self._session = session
        self._evidence = evidence

    @property
    def session(self) -> ClientSession:
        """Escape hatch for protocol calls this wrapper does not mirror."""
        return self._session

    def _append(self, direction: str, payload: dict[str, Any]) -> None:
        with self._evidence.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"direction": direction, **payload}, default=str) + "\n")

    async def initialize(self) -> Any:
        self._append("request", {"method": "initialize"})
        result = await self._session.initialize()
        self._append("response", {"method": "initialize", "result": _summarize(result)})
        return result

    async def list_tools(self) -> Any:
        self._append("request", {"method": "tools/list"})
        result = await self._session.list_tools()
        self._append(
            "response",
            {
                "method": "tools/list",
                "tool_names": [tool.name for tool in result.tools],
                "count": len(result.tools),
            },
        )
        return result

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_fault: bool = False,
    ) -> Any:
        payload = {"method": "tools/call", "tool": name, "arguments": arguments or {}}
        self._append("request", payload)
        result = await self._session.call_tool(name, arguments or {})
        self._append(
            "response",
            {
                "method": "tools/call",
                "tool": name,
                "is_error": bool(getattr(result, "isError", False)),
                "content": _summarize(result),
            },
        )
        if not allow_fault:
            assert_no_backend_fault(result, tool=name)
        return result


#: Text that means the BACKEND faulted, as distinct from a tool refusing on purpose.
#: A refusal ("Refused: ...", "Cannot answer ...") is a designed answer several lanes
#: assert on; these are not.
_BACKEND_FAULT_MARKERS = (
    "Error: HTTPStatusError",
    "Internal Server Error",
    "Traceback (most recent call last)",
    "got an unexpected keyword argument",
)


def assert_no_backend_fault(result: Any, *, tool: str) -> None:
    """Fail the lane when a tool response carries a backend fault rather than an answer.

    THIS EXISTS BECAUSE A FAULT ONCE READ AS A PASS. E2E-2 asserted ``"500" in context``
    against a fixture whose refund threshold was 500 dollars. ``build_context`` was
    raising ``TypeError`` server-side, and the tool returned::

        Error: HTTPStatusError: Server error '500 Internal Server Error' for url ...

    The substring matched the HTTP status code, the criterion recorded PASS, and a
    crashed endpoint was reported as a working one. A short numeric sentinel is not a
    safe assertion target, but the durable fix is not "choose better sentinels" -- it is
    that a fault must never reach an assertion at all.

    Checked centrally in ``call_tool`` so no lane can forget it. A lane that genuinely
    expects a fault passes ``allow_fault=True`` and says so at the call site.
    """

    content = getattr(result, "content", None) or []
    text = "\n".join(getattr(item, "text", "") or "" for item in content)
    for marker in _BACKEND_FAULT_MARKERS:
        if marker in text:
            raise AssertionError(
                f"backend fault from tool {tool!r} -- the response is an error, not an "
                f"answer, and no assertion on it would be meaningful:\n{text[:800]}"
            )


def _summarize(result: Any) -> Any:
    """Render an SDK result as JSON-safe data without truncating it.

    Truncation is deliberately avoided: the transcript is release evidence, and a
    shortened response is not evidence of what the server actually returned.
    """

    if hasattr(result, "model_dump"):
        try:
            return result.model_dump(mode="json")
        except Exception:  # pragma: no cover - SDK shape drift
            pass
    return repr(result)


@asynccontextmanager
async def stdio_session(
    config: E2EConfig,
    installed_python: Path,
    evidence: LaneEvidence,
    *,
    feature_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[RecordingClient]:
    """Spawn the stdio bridge and yield an initialized, recording client.

    The bridge's cwd is the harness state directory for the same reason the backend's
    is: ``resolve_env_file`` falls back to ``./.env``, and a bridge started inside the
    checkout would read the developer's env file.

    ``feature_env`` must be the SAME combination the backend was started with. The
    ``running_stack`` fixture passes both from one source so they cannot drift; a lane
    that spawns a bridge by hand is responsible for matching them.
    """

    env = child_environment(
        config,
        MENHIR_BACKEND_URL=config.backend_url,
        **(feature_env or {}),
        **(extra_env or {}),
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)

    parameters = StdioServerParameters(
        command=str(installed_python),
        args=["-m", "menhir.mcp.server"],
        env=env,
        cwd=str(config.state_dir),
    )
    evidence.record_stack(
        stdio_command=[str(installed_python), "-m", "menhir.mcp.server"],
        stdio_cwd=str(config.state_dir),
        backend_url=config.backend_url,
    )

    with evidence.stdio_log_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                client = RecordingClient(session, evidence)
                await client.initialize()
                yield client


async def wait_for_project_indexed(
    client: "RecordingClient",
    project: str,
    *,
    symbol_path: str | None = None,
    timeout: float = 180.0,
) -> str:
    """Block until ``project`` is answerable by ``query_structure``, or raise.

    ``ingest_project`` does not guarantee the graph write has landed when it returns.
    Its own formatter says so::

        "Graph write running in background - check server log for completion."

    and that string still begins "Scanned <project>:", so a lane checking the receipt
    cannot tell a completed scan from a queued one. Querying immediately then answers
    "Project '<name>' is not ingested in the structural graph" -- which is a race, not a
    defect, and it presented as an intermittent local failure and a hard CI failure on
    the slower runner.

    Polling the read surface is the honest wait: the lane proceeds exactly when the data
    an agent would query is actually there.
    """

    import time as _time

    async def _probe(query_type: str, path: str = "") -> str:
        args = {"query_type": query_type, "project": project}
        if path:
            args["path"] = path
        result = await client.call_tool("query_structure", args)
        content = getattr(result, "content", None) or []
        return "\n".join(getattr(item, "text", "") or "" for item in content)

    #: The formatter's own empty-result wordings. A still-writing project and a genuinely
    #: empty one read identically, which is why this wait is bounded and raises rather
    #: than returning quietly on timeout.
    pending = ("is not ingested", "No files found", "No symbols found")

    deadline = _time.monotonic() + timeout
    last = ""
    while _time.monotonic() < deadline:
        last = await _probe("files")
        ready = not any(marker in last for marker in pending)
        if ready and symbol_path:
            # Files land before symbols. CI got past the files probe and then failed on
            # "No symbols found for src/shop/storage.py" -- the background write is
            # incremental, so the first stage appearing proves only that it started.
            symbols = await _probe("symbols", symbol_path)
            if any(marker in symbols for marker in pending):
                last, ready = symbols, False
        if ready:
            return last
        await _asyncio.sleep(1.0)
    raise AssertionError(
        f"project {project!r} never became fully answerable within {timeout}s after "
        f"ingest_project returned (symbol_path={symbol_path!r}). "
        f"Last response:\n{last[:600]}"
    )
