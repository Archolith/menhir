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

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.e2e._harness.config import E2EConfig, child_environment
from tests.e2e._harness.evidence import LaneEvidence

__all__ = ["RecordingClient", "stdio_session"]


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

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
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
        return result


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
