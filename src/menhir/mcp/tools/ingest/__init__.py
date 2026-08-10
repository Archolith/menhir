"""Ingest tool group — memory creation, flagging, deletion, project scanning."""

from .add_candidate import AddCandidateTool
from .add_memory import AddMemoryTool
from .add_memory_and_track import AddMemoryAndTrackTool
from .close_memory import CloseMemoryTool
from .delete_memory import DeleteMemoryTool
from .flag_memory import FlagMemoryTool
from .ingest_document import IngestDocumentTool
from .ingest_project import IngestProjectTool
from .promote_memory import PromoteMemoryTool
from .unflag_memory import UnflagMemoryTool

INGEST_TOOLS = [AddMemoryTool, AddMemoryAndTrackTool, FlagMemoryTool, UnflagMemoryTool, PromoteMemoryTool, DeleteMemoryTool, IngestProjectTool, IngestDocumentTool, CloseMemoryTool, AddCandidateTool]
