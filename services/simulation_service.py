"""
Simulation Service — business logic layer for the LangGraph agent pipeline.

Wraps graph.run_simulation and shapes the raw state dict into a clean
response that the API endpoint layer can return directly.
"""
from __future__ import annotations

import logging

from graph import run_simulation

logger = logging.getLogger(__name__)


def run_query(query: str) -> dict:
    """
    Execute a natural-language query through the full LangGraph pipeline.

    Raises ValueError on empty input so the endpoint can return HTTP 400.
    Returns a dict that matches SimulateResponse schema.
    """
    if not query.strip():
        raise ValueError("Query cannot be empty")

    logger.info("Running simulation query: %s", query)
    state = run_simulation(query)

    return {
        "final_response": state.get("final_response", ""),
        "data_summary":   state.get("data_summary", {}),
        "calculations":   state.get("calculations", ""),
        "errors":         state.get("errors", []),
        "messages":       state.get("messages", []),
        "traversal_steps": state.get("traversal_steps_taken", 0),
    }
