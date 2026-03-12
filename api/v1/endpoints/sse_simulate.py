"""
SSE Simulate endpoints.

  GET  /api/v1/simulate/stream         — start a streaming simulation (text/event-stream)
  POST /api/v1/simulate/stream/resume  — resume a paused (HITL) stream

SSE event sequence (no HITL):
  stream_started → query_refiner_complete → orchestrator_complete → schema_complete
  → [planner_complete | traversal_complete] → response_complete → complete

SSE event sequence (with HITL):
  stream_started → query_refiner_complete → hitl_start
  [user calls POST /simulate/stream/resume]
  → hitl_complete → orchestrator_complete → schema_complete → ... → complete

Error at any point emits:  error
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import services.db_service as db_svc
from graph import stream_simulation
from services.sse_manager import sse_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/simulate", tags=["SSE Stream"])


# ── Request schema ────────────────────────────────────────────────────────────

class StreamResumeRequest(BaseModel):
    thread_id: str
    clarification: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "thread_id": "session-abc-123",
                "clarification": "Chicago market, 300 sites, end of next week.",
            }
        }
    }


# ── Thread runner (executes in asyncio.get_event_loop().run_in_executor) ──────

def _run_stream_thread(
    query: str,
    query_id: str,
    thread_id: str,
    user_id: str,
) -> None:
    """
    All blocking work runs here — inside a thread executor so the event loop
    stays free to serve SSE keep-alive reads and the resume endpoint.

    DB writes mirror the non-streaming simulation_service.py flow:
      - upsert_thread + create_query at start
      - update_query_paused + create_hitl_clarification when HITL fires
      - update_query_complete on success  /  update_query_error on exception
    """
    t0 = time.perf_counter()

    db_svc.upsert_thread(thread_id, user_id)
    db_svc.auto_name_thread(thread_id, query)
    db_svc.create_query(query_id, thread_id, user_id, query)

    def _on_hitl(payload: dict) -> None:
        """Called by stream_simulation() just before it waits for HITL input."""
        db_svc.update_query_paused(query_id)
        db_svc.create_hitl_clarification(
            query_id=query_id,
            thread_id=thread_id,
            questions_asked=payload.get("questions", []),
            assumptions_offered=payload.get("assumptions_if_skipped", []),
        )

    try:
        final_state = stream_simulation(
            query=query,
            query_id=query_id,
            thread_id=thread_id,
            mgr=sse_manager,
            on_hitl=_on_hitl,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)

        db_svc.update_query_complete(
            query_id=query_id,
            refined_query=final_state.get("refined_query", ""),
            routing_decision=final_state.get("routing_decision", ""),
            planner_steps=final_state.get("planner_steps", []),
            final_response=final_state.get("final_response", ""),
            duration_ms=duration_ms,
        )
        sse_manager.put_sync(query_id, "complete", {
            "final_response":    final_state.get("final_response", ""),
            "routing_decision":  final_state.get("routing_decision", ""),
            "planner_steps":     final_state.get("planner_steps", []),
            "planning_rationale": final_state.get("planning_rationale", ""),
            "traversal_steps":   final_state.get("traversal_steps_taken", 0),
            "errors":            final_state.get("errors", []),
            "data_summary":      final_state.get("data_summary", {}),
            "calculations":      final_state.get("calculations", ""),
        })

    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.exception("stream_simulation failed [query=%s thread=%s]", query_id, thread_id)
        db_svc.update_query_error(query_id, duration_ms)
        sse_manager.put_sync(query_id, "error", {"message": str(exc)})

    finally:
        # Sentinel that tells _event_generator to stop iterating
        sse_manager.put_sync(query_id, "__done__", {})


# ── Async SSE generator ───────────────────────────────────────────────────────

async def _event_generator(
    queue: asyncio.Queue,
    query_id: str,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    """
    Reads events from the asyncio.Queue and yields them as SSE-formatted strings.
    Stops when the sentinel '__done__' event is received or on 'error'.
    Cleans up sse_manager state when done regardless of how it exits.
    """
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            event_name = item["event"]
            if event_name == "__done__":
                break
            yield f"event: {event_name}\ndata: {json.dumps(item['data'])}\n\n"
            if event_name == "error":
                break
    finally:
        sse_manager.cleanup(query_id, thread_id)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/stream")
async def stream_simulate(
    query:     str = Query(..., description="The simulation query"),
    user_id:   str = Query(..., description="User identifier"),
    thread_id: str = Query(None, description="Conversation thread ID; auto-generated if omitted"),
):
    """
    Start a streaming simulation. Returns `text/event-stream`.

    The first event is always `stream_started` which carries the `thread_id`
    (important when the caller did not supply one and the server generated it).
    The client must store `thread_id` from this event to call the resume endpoint
    if a `hitl_start` event arrives.
    """
    if not query.strip():
        raise HTTPException(status_code=422, detail="query cannot be empty")
    if not user_id.strip():
        raise HTTPException(status_code=422, detail="user_id cannot be empty")

    if not thread_id:
        thread_id = str(uuid.uuid4())
    query_id = str(uuid.uuid4())

    loop  = asyncio.get_running_loop()
    queue = sse_manager.register(query_id, loop)

    # Launch blocking graph work in a thread executor
    loop.run_in_executor(
        None,
        _run_stream_thread,
        query, query_id, thread_id, user_id,
    )

    async def _stream_with_preamble() -> AsyncGenerator[str, None]:
        # First event: communicate the thread_id back to the client
        yield (
            f"event: stream_started\n"
            f"data: {json.dumps({'query_id': query_id, 'thread_id': thread_id})}\n\n"
        )
        async for chunk in _event_generator(queue, query_id, thread_id):
            yield chunk

    return StreamingResponse(
        _stream_with_preamble(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",   # disable nginx proxy buffering
        },
    )


@router.post("/stream/resume", status_code=200)
def resume_stream(req: StreamResumeRequest):
    """
    Resume a paused SSE stream after the user provides HITL clarification.

    Writes the user's answer to the DB and signals the blocked stream thread
    to continue. The SSE connection must still be open (Option B).
    Returns 404 if no active stream is waiting for this thread_id.
    """
    if not req.thread_id.strip():
        raise HTTPException(status_code=422, detail="thread_id cannot be empty")

    clarification = req.clarification.strip() or "Accept stated assumptions"
    was_skipped   = (clarification == "Accept stated assumptions")

    # Persist HITL answer to DB
    query_id = db_svc.get_paused_query_id(req.thread_id)
    if query_id:
        db_svc.update_hitl_answered(query_id, clarification, was_skipped)
    db_svc.touch_thread(req.thread_id)

    # Unblock the waiting stream thread
    signaled = sse_manager.signal_resume(req.thread_id, clarification)
    if not signaled:
        raise HTTPException(
            status_code=404,
            detail=f"No active stream found for thread '{req.thread_id}'. "
                   "The stream may have already timed out or completed.",
        )

    return {"status": "resumed", "thread_id": req.thread_id}
