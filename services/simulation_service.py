"""
Simulation Service — business logic layer for the LangGraph agent pipeline.

Exposes three operations:
  • run_query(query, thread_id, user_id)    — start a new simulation
  • resume_query(clarification, thread_id)  — resume after HITL clarification
  • get_interrupt_status(thread_id)         — check if a thread is paused

All three delegate to graph.py, shape the raw state into a clean response
dict, and persist the interaction to pwc_simulation_agent_schema via db_service.
"""
from __future__ import annotations

import logging
import time
import uuid

from graph import run_simulation, resume_simulation, get_pending_interrupt
import services.db_service as db_svc

logger = logging.getLogger(__name__)


def _shape_response(state: dict) -> dict:
    """
    Convert a raw SimulationState dict into the API response shape.
    Detects LangGraph interrupt signals and surfaces them as clarification_needed.
    """
    interrupts = state.get("__interrupt__", [])
    if interrupts:
        raw = interrupts[0]
        interrupt_payload = raw.value if hasattr(raw, "value") else raw
        return {
            "status": "clarification_needed",
            "final_response": "",
            "errors": [],
            "routing_decision": "",
            "planner_steps": [],
            "clarification": interrupt_payload,
        }

    return {
        "status": "complete",
        "final_response": state.get("final_response", ""),
        "errors": state.get("errors", []),
        "routing_decision": state.get("routing_decision", ""),
        "planner_steps": state.get("planner_steps", []),
        "graph": state.get("graph_data", {}),
    }


def run_query(
    query: str,
    thread_id: str = "default",
    user_id: str = "anonymous",
) -> dict:
    """
    Start a new simulation query.

    Persists to DB:
      - upsert thread (thread_id, user_id)
      - create query row
      - on clarification pause: update query to paused + create hitl_clarification row
      - on completion: update query with all result fields

    Returns a shaped response dict. If the query refiner needs clarification,
    status will be "clarification_needed" and a clarification payload is included.
    """
    if not query.strip():
        raise ValueError("Query cannot be empty")

    query_id = str(uuid.uuid4())
    t0 = time.perf_counter()

    db_svc.upsert_thread(thread_id, user_id)
    db_svc.auto_name_thread(thread_id, query)
    db_svc.create_query(query_id, thread_id, user_id, query)

    logger.info("Starting query [thread=%s query=%s]: %.80s", thread_id, query_id, query)

    try:
        state = run_simulation(query, thread_id=thread_id)
    except Exception:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        db_svc.update_query_error(query_id, duration_ms)
        raise

    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    response = _shape_response(state)

    if response["status"] == "clarification_needed":
        clarification = response.get("clarification", {})
        db_svc.update_query_paused(query_id)
        db_svc.create_hitl_clarification(
            query_id=query_id,
            thread_id=thread_id,
            questions_asked=clarification.get("questions", []),
            assumptions_offered=clarification.get("assumptions_if_skipped", []),
        )
    else:
        db_svc.update_query_complete(
            query_id=query_id,
            refined_query=state.get("refined_query", ""),
            routing_decision=state.get("routing_decision", ""),
            planner_steps=state.get("planner_steps", []),
            final_response=state.get("final_response", ""),
            duration_ms=duration_ms,
            graph_data=state.get("graph_data"),
        )

    return response


def resume_query(clarification: str, thread_id: str) -> dict:
    """
    Resume a paused simulation with the user's clarification answer.

    Persists to DB:
      - update hitl_clarification with user's answer
      - touch thread last_active_at
      - on completion: update query with all result fields
      - on error: update query status to error

    Returns the shaped final response once the graph completes.
    """
    if not clarification.strip():
        raise ValueError("Clarification cannot be empty")
    if not thread_id.strip():
        raise ValueError("thread_id is required to resume a simulation")

    was_skipped = clarification.strip() == "Accept stated assumptions"

    # Look up the paused query for this thread before resuming
    query_id = db_svc.get_paused_query_id(thread_id)
    if query_id:
        db_svc.update_hitl_answered(query_id, clarification, was_skipped)

    db_svc.touch_thread(thread_id)

    logger.info("Resuming query [thread=%s]", thread_id)

    t0 = time.perf_counter()
    state = resume_simulation(clarification, thread_id)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)

    response = _shape_response(state)

    if query_id:
        if response["status"] == "complete":
            db_svc.update_query_complete(
                query_id=query_id,
                refined_query=state.get("refined_query", ""),
                routing_decision=state.get("routing_decision", ""),
                planner_steps=state.get("planner_steps", []),
                final_response=state.get("final_response", ""),
                duration_ms=duration_ms,
                graph_data=state.get("graph_data"),
            )
        else:
            db_svc.update_query_error(query_id, duration_ms)

    return response


def get_interrupt_status(thread_id: str) -> dict | None:
    """
    Check whether a given thread is currently paused at a HITL interrupt.
    Returns the clarification payload if paused, or None otherwise.
    """
    return get_pending_interrupt(thread_id)
