"""
Simulation Service — business logic layer for the LangGraph agent pipeline.

Exposes three operations:
  • run_query(query, thread_id)         — start a new simulation
  • resume_query(clarification, thread_id) — resume after HITL clarification
  • get_interrupt_status(thread_id)     — check if a thread is paused

All three delegate to graph.py and shape the raw state into a clean
response dict that the API endpoint layer can return directly.
"""
from __future__ import annotations

import logging

from graph import run_simulation, resume_simulation, get_pending_interrupt

logger = logging.getLogger(__name__)


def _shape_response(state: dict) -> dict:
    """
    Convert a raw SimulationState dict into the API response shape.
    Detects LangGraph interrupt signals and surfaces them as clarification_needed.
    """
    # LangGraph wraps interrupt values in '__interrupt__' when the graph is paused
    interrupts = state.get("__interrupt__", [])
    if interrupts:
        raw = interrupts[0]
        interrupt_payload = raw.value if hasattr(raw, "value") else raw
        return {
            "status": "clarification_needed",
            "clarification": interrupt_payload,
            "final_response": "",
            "data_summary": {},
            "calculations": "",
            "errors": [],
            "messages": state.get("messages", []),
            "traversal_steps": 0,
            "routing_decision": "",
            "planning_rationale": "",
            "planner_steps": [],
        }

    return {
        "status": "complete",
        "clarification": None,
        "final_response": state.get("final_response", ""),
        "data_summary": state.get("data_summary", {}),
        "calculations": state.get("calculations", ""),
        "errors": state.get("errors", []),
        "messages": state.get("messages", []),
        "traversal_steps": state.get("traversal_steps_taken", 0),
        "routing_decision": state.get("routing_decision", ""),
        "planning_rationale": state.get("planning_rationale", ""),
        "planner_steps": state.get("planner_steps", []),
    }


def run_query(query: str, thread_id: str = "default") -> dict:
    """
    Start a new simulation query.

    Returns a shaped response dict. If the query refiner needs clarification,
    status will be "clarification_needed" and a clarification payload is included.
    """
    if not query.strip():
        raise ValueError("Query cannot be empty")

    logger.info("Starting query [thread=%s]: %.80s", thread_id, query)
    state = run_simulation(query, thread_id=thread_id)
    return _shape_response(state)


def resume_query(clarification: str, thread_id: str) -> dict:
    """
    Resume a paused simulation with the user's clarification answer.

    Returns the shaped final response once the graph completes.
    """
    if not clarification.strip():
        raise ValueError("Clarification cannot be empty")
    if not thread_id.strip():
        raise ValueError("thread_id is required to resume a simulation")

    logger.info("Resuming query [thread=%s]", thread_id)
    state = resume_simulation(clarification, thread_id)
    return _shape_response(state)


def get_interrupt_status(thread_id: str) -> dict | None:
    """
    Check whether a given thread is currently paused at a HITL interrupt.
    Returns the clarification payload if paused, or None otherwise.
    """
    return get_pending_interrupt(thread_id)
