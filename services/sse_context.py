"""
SSE Context — thread-safe contextvars for passing the SSE manager into graph nodes.

graph.py sets these before streaming; nodes like planner.py read them to emit
granular progress events without polluting SimulationState.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.sse_manager import SSEManager

_sse_mgr: ContextVar["SSEManager | None"] = ContextVar("_sse_mgr", default=None)
_sse_query_id: ContextVar[str | None] = ContextVar("_sse_query_id", default=None)


def set_sse_context(mgr: "SSEManager", query_id: str) -> None:
    _sse_mgr.set(mgr)
    _sse_query_id.set(query_id)


def emit_sse(event_name: str, data: dict) -> None:
    """Emit an SSE event if a manager is available (no-op in non-streaming mode)."""
    mgr = _sse_mgr.get()
    qid = _sse_query_id.get()
    if mgr is not None and qid is not None:
        mgr.put_sync(qid, event_name, data)
