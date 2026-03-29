"""
Feedback endpoints — collect user feedback on Simulation Agent responses.
"""
import uuid

from fastapi import APIRouter, Query

from api.v1.schemas import (
    FeedbackRequest,
    FeedbackOut,
    FeedbackSubmitResponse,
    FeedbackStats,
)
from services import db_service as db_svc

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post(
    "/submit",
    response_model=FeedbackSubmitResponse,
    status_code=201,
    summary="Submit feedback for a chat turn",
)
def submit_feedback(fb: FeedbackRequest):
    """Store feedback (rating / thumbs / comment) linked to a query turn via query_id."""
    feedback_id = str(uuid.uuid4())
    db_svc.create_feedback(
        feedback_id=feedback_id,
        thread_id=fb.thread_id,
        query_id=fb.query_id,
        user_id=fb.user_id,
        username=fb.username,
        rating=fb.rating,
        is_positive=fb.is_positive,
        comment=fb.comment,
    )
    return FeedbackSubmitResponse(feedback_id=feedback_id)


@router.get(
    "/query/{query_id}",
    response_model=list[FeedbackOut],
    summary="Get all feedback for a chat turn",
)
def get_feedback_for_query(query_id: str):
    """Retrieve all feedback entries linked to a specific chat turn."""
    rows = db_svc.get_feedback_for_query(query_id)
    return [FeedbackOut(**r) for r in rows]


@router.get(
    "/thread/{thread_id}",
    response_model=list[FeedbackOut],
    summary="Get all feedback for a thread",
)
def get_feedback_for_thread(thread_id: str):
    """Retrieve all feedback entries for a given session/thread."""
    rows = db_svc.get_feedback_for_thread(thread_id)
    return [FeedbackOut(**r) for r in rows]


@router.get(
    "/stats",
    response_model=FeedbackStats,
    summary="Get feedback statistics",
)
def feedback_stats(
    thread_id: str | None = Query(None, description="Filter stats by thread"),
):
    """Return aggregate feedback stats (count, avg rating, thumbs up/down)."""
    return db_svc.get_feedback_stats(thread_id)
