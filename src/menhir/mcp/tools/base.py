"""Backward-compatible exports for MCP tool contract bases."""

from __future__ import annotations

from menhir.mcp.contracts import BaseJsonTool, BaseTextTool, BaseTool

__all__ = ["BaseTool", "BaseTextTool", "BaseJsonTool"]
