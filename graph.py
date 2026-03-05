"""
Simulation Agent — Main LangGraph definition.

Graph Flow:
    START
      └─► query_refiner  [HITL — may interrupt for clarification]
            └─► orchestrator  [classifies and routes]
                  ├─► END                               (greeting — response set directly)
                  └─► discover_schema
                        ├─► traversal → response → END  (simple data lookup)
                        └─► planner   → response → END  (complex simulation)
"""
from __future__ import annotations

import logging
from datetime import datetime

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from models.state import SimulationState
from agents.query_refiner import query_refiner_node
from agents.orchestrator import orchestrator_node
from agents.schema_discovery import discover_schema_node
from agents.traversal import traversal_node
from agents.planner import planner_node
from agents.response import response_node

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Routing functions (conditional edges)
# ─────────────────────────────────────────────

def _route_after_orchestrator(state: SimulationState) -> str:
    """
    After the orchestrator sets routing_decision, decide which node to go to next.
      "greeting"   → "__end__" (final_response already set by orchestrator)
      "traversal"  → discover_schema (then traversal → response)
      "simulation" → discover_schema (then planner → response)
    """
    decision = state.get("routing_decision", "simulation")
    if decision == "greeting":
        return "__end__"
    return "discover_schema"


def _route_after_discovery(state: SimulationState) -> str:
    """
    After schema discovery, branch based on the orchestrator's earlier decision.
      "simulation" → planner
      anything else → traversal (default)
    """
    decision = state.get("routing_decision", "traversal")
    if decision == "simulation":
        return "planner"
    return "traversal"


# ─────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────

def build_simulation_graph() -> StateGraph:
    """
    Build and compile the LangGraph for the simulation agent system.

    The graph is compiled with a MemorySaver checkpointer to support the
    Human-in-the-Loop interrupt in the query_refiner node.

    Returns a compiled graph that can be invoked with a SimulationState
    and a thread config: {"configurable": {"thread_id": "<uuid>"}}.
    """
    graph = StateGraph(SimulationState)

    # ── Nodes ──────────────────────────────────────────────────────────────
    graph.add_node("query_refiner",   query_refiner_node)
    graph.add_node("orchestrator",    orchestrator_node)
    graph.add_node("discover_schema", discover_schema_node)
    graph.add_node("traversal",       traversal_node)
    graph.add_node("planner",         planner_node)
    graph.add_node("response",        response_node)

    # ── Edges ──────────────────────────────────────────────────────────────
    graph.add_edge(START, "query_refiner")
    graph.add_edge("query_refiner", "orchestrator")

    # Orchestrator fans out conditionally
    graph.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {"__end__": END, "discover_schema": "discover_schema"},
    )

    # After schema discovery, branch to planner or direct traversal
    graph.add_conditional_edges(
        "discover_schema",
        _route_after_discovery,
        {"planner": "planner", "traversal": "traversal"},
    )

    graph.add_edge("traversal", "response")
    graph.add_edge("planner",   "response")
    graph.add_edge("response",  END)

    # ── Compile with MemorySaver for HITL interrupt support ────────────────
    compiled = graph.compile(checkpointer=MemorySaver())
    logger.info("Simulation graph compiled successfully (with HITL checkpointer)")

    return compiled


# Module-level singleton so every call to run_simulation reuses the same graph
# (and therefore the same MemorySaver — thread states are keyed by thread_id)
_graph = build_simulation_graph()


def _make_initial_state(query: str, max_steps: int) -> SimulationState:
    return {
        "user_query": query,
        "refined_query": "",
        "current_phase": "query_refinement",
        "routing_decision": "",
        "routing_context": "",
        "planning_rationale": "",
        "planner_steps": [],
        "planner_step_results": [],
        "kg_schema": "",
        "traversal_findings": "",
        "traversal_tool_calls": [],
        "traversal_steps_taken": 0,
        "max_traversal_steps": max_steps,
        "scenario_simulation_guidance": "",
        "final_response": "",
        "calculations": "",
        "data_summary": {},
        "errors": [],
        "created_at": datetime.now().isoformat(),
        "messages": [],
    }


def run_simulation(
    query: str,
    max_steps: int = 15,
    thread_id: str = "default",
) -> dict:
    """
    Start (or resume) a simulation query end-to-end.

    Args:
        query:      The user's simulation question.
        max_steps:  Maximum number of tool calls for each traversal agent run.
        thread_id:  Conversation thread identifier for HITL state tracking.

    Returns:
        The final state dict — or an intermediate state with
        ``"__interrupt__": [...]`` when the query_refiner pauses for clarification.
    """
    thread_config = {"configurable": {"thread_id": thread_id}}

    initial_state = _make_initial_state(query, max_steps)

    logger.info("Starting simulation [thread=%s]: %s", thread_id, query)
    final_state = _graph.invoke(initial_state, config=thread_config)
    logger.info("Simulation complete [thread=%s]", thread_id)

    return final_state


def resume_simulation(
    user_clarification: str,
    thread_id: str,
) -> dict:
    """
    Resume a graph that was interrupted by the query_refiner node.

    Args:
        user_clarification: The user's answer to the clarifying questions.
        thread_id:          Must match the thread_id used in run_simulation.

    Returns:
        The final state dict after resumption.
    """
    from langgraph.types import Command

    thread_config = {"configurable": {"thread_id": thread_id}}

    logger.info(
        "Resuming simulation [thread=%s] with clarification: %s",
        thread_id, user_clarification[:80],
    )
    final_state = _graph.invoke(
        Command(resume=user_clarification),
        config=thread_config,
    )
    logger.info("Simulation resumed and complete [thread=%s]", thread_id)

    return final_state


def get_pending_interrupt(thread_id: str) -> dict | None:
    """
    Check whether a given thread is currently paused at a HITL interrupt.

    Returns the interrupt payload dict if paused, or None if not paused.
    """
    thread_config = {"configurable": {"thread_id": thread_id}}
    state = _graph.get_state(thread_config)

    for task in state.tasks:
        if task.interrupts:
            # Return the first interrupt's value (the clarification prompt dict)
            return task.interrupts[0].value

    return None
