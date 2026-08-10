"""Conflict tool group — conflict listing, resolution, LLM review, scanning."""

from .list_conflicts import ListConflictsTool
from .requeue_for_review import RequeueForReviewTool
from .resolve_conflict import ResolveConflictTool
from .run_llm_review import RunLLMReviewTool
from .scan_conflicts import ScanConflictsTool

CONFLICT_TOOLS = [ListConflictsTool, ResolveConflictTool, RequeueForReviewTool, RunLLMReviewTool, ScanConflictsTool]
