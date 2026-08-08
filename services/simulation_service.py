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

import json
import logging
import time
import uuid

from graph import run_simulation, resume_simulation, get_pending_interrupt
import services.db_service as db_svc
from services.langfuse_observability import set_request_context

logger = logging.getLogger(__name__)


def _slim_tool_output(tc: dict) -> dict:
    """
    Return a copy of a tool call record with tool_output parsed to an object.

    For get_kpi:  keep kpi_kpi_id, kpi_name, and kpi_business_logic.
    For get_node: keep only node_id, name, label, and map_python_function.
    Other tools are parsed through; if parsing fails, the original string is kept.
    """
    raw = tc.get("tool_output")
    if not raw:
        return tc

    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return tc

    tool_name = tc.get("tool_name", "")
    if tool_name == "get_kpi" and isinstance(parsed, dict):
        parsed = {k: parsed[k] for k in ("kpi_kpi_id", "kpi_name", "kpi_business_logic") if k in parsed}
    elif tool_name == "get_node" and isinstance(parsed, dict):
        parsed = {k: parsed[k] for k in ("node_id", "name", "label", "map_python_function") if k in parsed}

    return {**tc, "tool_output": parsed}


def _build_traces(state: dict, duration_ms: float) -> dict:
    """
    Build a structured trace JSON from the final graph state.

    For the simulation path (planner), traces are grouped by planner step.
    For the traversal path, all tool calls appear under a single step.
    """
    routing = state.get("routing_decision", "")
    steps = []

    if routing == "simulation":
        planner_steps = state.get("planner_steps", [])
        planner_results = state.get("planner_step_results", [])
        for i, step_result in enumerate(planner_results):
            step_label = planner_steps[i] if i < len(planner_steps) else f"Step {i + 1}"
            tool_calls = [_slim_tool_output(tc) for tc in step_result.get("traversal_tool_calls", [])]
            steps.append({
                "step": step_label,
                "tool_calls": tool_calls,
            })
    elif routing == "traversal":
        tool_calls = [_slim_tool_output(tc) for tc in state.get("traversal_tool_calls", [])]
        query = state.get("refined_query") or state.get("user_query", "")
        steps.append({
            "step": query,
            "tool_calls": tool_calls,
        })

    total_tool_calls = sum(len(s["tool_calls"]) for s in steps)

    nodes_executed = []
    for node in ["query_refiner", "orchestrator", "discover_schema", "planner", "traversal", "response"]:
        # Infer which nodes ran based on state fields they set
        if node == "query_refiner" and state.get("refined_query"):
            nodes_executed.append(node)
        elif node == "orchestrator" and routing:
            nodes_executed.append(node)
        elif node == "discover_schema" and state.get("kg_schema"):
            nodes_executed.append(node)
        elif node == "planner" and state.get("planner_steps"):
            nodes_executed.append(node)
        elif node == "traversal" and (state.get("traversal_findings") or state.get("planner_step_results")):
            nodes_executed.append(node)
        elif node == "response" and state.get("final_response"):
            nodes_executed.append(node)

    return {
        "nodes_executed": nodes_executed,
        "steps": steps,
        "total_tool_calls": total_tool_calls,
        "total_execution_time_ms": duration_ms,
    }


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
        "current_status": state.get("current_status", []),
        "execution_algorithm": state.get("execution_algorithm", ""),
        "errors": state.get("errors", []),
        "routing_decision": state.get("routing_decision", ""),
        "planner_steps": state.get("planner_steps", []),
        "graph": state.get("graph_data", {}),
        "analysis": state.get("semantic_analysis", {}),
    }


def run_query(
    query: str,
    thread_id: str = "default",
    user_id: str = "anonymous",
    project_type: str = "",
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

    # Bind tracing context for every LLM call this query makes. Set here (not in
    # the endpoint) because the whole run happens on this executor thread —
    # loop.run_in_executor does not copy the caller's contextvars.
    set_request_context(thread_id, user_id, query_id)

    db_svc.upsert_thread(thread_id, user_id)
    db_svc.auto_name_thread(thread_id, query)
    db_svc.create_query(query_id, thread_id, user_id, query)

    logger.info("Starting query [thread=%s query=%s]: %.80s", thread_id, query_id, query)

    try:
        state = run_simulation(query, thread_id=thread_id, project_type=project_type)
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
        traces = _build_traces(state, duration_ms)
        db_svc.update_query_complete(
            query_id=query_id,
            refined_query=state.get("refined_query", ""),
            routing_decision=state.get("routing_decision", ""),
            planner_steps=state.get("planner_steps", []),
            final_response=state.get("final_response", ""),
            current_status=state.get("current_status", []),
            duration_ms=duration_ms,
            graph_data=state.get("graph_data"),
            traces=traces,
            analysis=state.get("semantic_analysis"),
            algorithm=state.get("execution_algorithm", ""),
            scenario_match_found=state.get("scenario_match_found"),
        )
        response["traces"] = traces

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

    # Resume arrives on a fresh request/thread, so rebind the tracing context.
    # ResumeRequest carries no user_id — recover it from the thread so the
    # post-clarification traces stay attributed to the same user.
    try:
        thread_row = db_svc.get_thread(thread_id) or {}
    except Exception:  # noqa: BLE001 — tracing metadata must never break a resume
        thread_row = {}
    set_request_context(thread_id, thread_row.get("user_id"), query_id)

    db_svc.touch_thread(thread_id)

    logger.info("Resuming query [thread=%s]", thread_id)

    t0 = time.perf_counter()
    state = resume_simulation(clarification, thread_id)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)

    response = _shape_response(state)

    if query_id:
        if response["status"] == "complete":
            traces = _build_traces(state, duration_ms)
            db_svc.update_query_complete(
                query_id=query_id,
                refined_query=state.get("refined_query", ""),
                routing_decision=state.get("routing_decision", ""),
                planner_steps=state.get("planner_steps", []),
                final_response=state.get("final_response", ""),
                current_status=state.get("current_status", []),
                duration_ms=duration_ms,
                graph_data=state.get("graph_data"),
                traces=traces,
                analysis=state.get("semantic_analysis"),
                scenario_match_found=state.get("scenario_match_found"),
            )
            response["traces"] = traces
        else:
            db_svc.update_query_error(query_id, duration_ms)

    return response


def get_interrupt_status(thread_id: str) -> dict | None:
    """
    Check whether a given thread is currently paused at a HITL interrupt.
    Returns the clarification payload if paused, or None otherwise.
    """
    return get_pending_interrupt(thread_id)
