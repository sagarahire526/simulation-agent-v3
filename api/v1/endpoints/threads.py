"""
Threads endpoints.

  POST   /api/v1/threads                          — Create a new thread
  GET    /api/v1/threads                          — List all threads for a user
  GET    /api/v1/threads/{thread_id}              — Get a single thread's metadata
  DELETE /api/v1/threads/{thread_id}              — Delete a thread and all its data
  GET    /api/v1/threads/{thread_id}/messages     — Get all queries for a thread
  GET    /api/v1/threads/{thread_id}/clarification — Get pending clarification status
"""
import uuid

from fastapi import APIRouter, HTTPException, Query

import services.db_service as db_svc
from api.v1.schemas import CreateThreadRequest, ThreadSummary, MessageRecord, ClarificationStatus

router = APIRouter(prefix="/threads", tags=["Threads"])


@router.post("", response_model=ThreadSummary, status_code=201)
def create_thread(req: CreateThreadRequest):
    """
    Create a new conversation thread with a human-readable name.

    The frontend should call this before sending the first query,
    using the first few words of the user's question as thread_name.
    The returned thread_id is then passed to /simulate or /simulate/stream.
    """
    thread_id = str(uuid.uuid4())
    db_svc.upsert_thread(thread_id, req.user_id, req.thread_name)
    thread = db_svc.get_thread(thread_id)
    return thread


@router.get("", response_model=list[ThreadSummary])
def list_threads(user_id: str = Query(..., description="Filter threads by user ID")):
    """
    Return all threads belonging to a user, most recently active first.
    Each thread includes a count of total queries made within it.
    """
    return db_svc.get_threads_by_user(user_id)


@router.get("/{thread_id}", response_model=ThreadSummary)
def get_thread(thread_id: str):
    """
    Return metadata for a single thread.
    Returns 404 if the thread does not exist.
    """
    thread = db_svc.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
    return thread


@router.delete("/{thread_id}", status_code=204)
def delete_thread(thread_id: str):
    """
    Permanently delete a thread and all its queries and clarification records.
    Returns 404 if the thread does not exist.
    """
    deleted = db_svc.delete_thread(thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")


@router.get("/{thread_id}/messages", response_model=list[MessageRecord])
def get_messages(thread_id: str):
    """
    Return all queries for a thread in chronological order (oldest first).
    Each record includes the original query, refined query, routing decision,
    planner steps, final response, and timing metadata.
    Returns 404 if the thread does not exist.
    """
    thread = db_svc.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
    return db_svc.get_messages_by_thread(thread_id)


@router.get("/{thread_id}/clarification", response_model=ClarificationStatus)
def get_clarification_status(thread_id: str):
    """
    Check whether a thread is currently paused waiting for user clarification.

    Returns:
      - is_paused=true with full clarification details if paused
      - is_paused=false with null fields if not paused

    Used by the frontend on page refresh to detect and restore a pending
    HITL state without needing to re-run the simulation.
    """
    thread = db_svc.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")

    row = db_svc.get_pending_clarification(thread_id)
    if not row:
        return ClarificationStatus(is_paused=False)

    return ClarificationStatus(
        is_paused=True,
        clarification_id=row["clarification_id"],
        query_id=row["query_id"],
        questions_asked=row["questions_asked"],
        assumptions_offered=row["assumptions_offered"],
        asked_at=row["asked_at"],
    )
