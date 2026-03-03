"""
Simulation Agent — Main LangGraph definition.

Graph Flow:
    START → discover_schema → traversal (autonomous ReAct) → response → END
"""
from __future__ import annotations

import logging
from datetime import datetime

from langgraph.graph import StateGraph, START, END

from models.state import SimulationState
from agents.schema_discovery import discover_schema_node
from agents.traversal import traversal_node
from agents.response import response_node

logger = logging.getLogger(__name__)


def build_simulation_graph() -> StateGraph:
    """
    Build and compile the LangGraph for the simulation agent system.

    Returns a compiled graph that can be invoked with a SimulationState.
    """

    # ── Define the graph ──
    graph = StateGraph(SimulationState)

    # ── Add nodes ──
    graph.add_node("discover_schema", discover_schema_node)
    graph.add_node("traversal", traversal_node)
    graph.add_node("response", response_node)

    # ── Add edges ──
    graph.add_edge(START, "discover_schema")
    graph.add_edge("discover_schema", "traversal")
    graph.add_edge("traversal", "response")
    graph.add_edge("response", END)

    # ── Compile ──
    compiled = graph.compile()
    logger.info("Simulation graph compiled successfully")

    return compiled


def run_simulation(query: str, max_steps: int = 15) -> dict:
    """
    Convenience function: run a simulation query end-to-end.

    Args:
        query: The user's simulation question
        max_steps: Maximum number of tool calls for the traversal agent

    Returns:
        The final state dict with all results.
    """
    graph = build_simulation_graph()

    initial_state: SimulationState = {
        "user_query": query,
        "current_phase": "discovery",
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

    logger.info(f"Starting simulation for: {query}")
    final_state = graph.invoke(initial_state)
    logger.info("Simulation complete")

    return final_state
