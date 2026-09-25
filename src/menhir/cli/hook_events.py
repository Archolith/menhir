"""Shared stdin/env helpers for the menhir hook event implementations."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _parse_stdin() -> tuple[str, str]:
    """Parse session_id and prompt from stdin JSON. Returns (session_id, prompt)."""
    session_id = "unknown"
    prompt = ""
    if not sys.stdin.isatty():
        try:
            hook_input = json.load(sys.stdin)
            session_id = hook_input.get("session_id", session_id)
            prompt = hook_input.get("prompt", "")
        except Exception as exc:
            print(f"menhir hook: stdin parse failed ({type(exc).__name__})", file=sys.stderr)
    return session_id, prompt


def _load_env() -> None:
    """Load .env from ENV_FILE env var or package root."""
    import os
    from dotenv import load_dotenv

    env_file = os.environ.get("ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        pkg_dir = Path(__file__).resolve().parent.parent.parent.parent
        load_dotenv(pkg_dir / ".env")

