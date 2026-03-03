"""
Shared state models for the LangGraph simulation agent system.
All agents read/write to this shared state as it flows through the graph.
"""
from __future__ import annotations

import operator
from typing import Any, Literal, Optional, TypedDict, Annotated


# ─────────────────────────────────────────────
# Traversal Agent output types
# ─────────────────────────────────────────────

class ToolCallRecord(TypedDict):
    """Record of a single tool invocation by the traversal agent."""
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: Any
    status: Literal["success", "error"]
    execution_time_ms: float


# ─────────────────────────────────────────────
# Main Graph State  (shared across all nodes)
# ─────────────────────────────────────────────

class SimulationState(TypedDict):
    """
    The shared state that flows through the LangGraph.
    Uses Annotated + operator.add for list fields so that
    each node *appends* rather than overwrites.
    """
    # ── Input ──
    user_query: str

    # ── Phase tracking ──
    current_phase: Literal[
        "discovery", "traversal", "response", "complete", "error"
    ]

    # ── Knowledge Graph Schema (discovered once) ──
    kg_schema: str  # Node labels, relationships, properties

    # ── Traversal Agent ──
    traversal_findings: str  # Agent's natural-language summary of what it found
    traversal_tool_calls: Annotated[list[ToolCallRecord], operator.add]
    traversal_steps_taken: int  # Number of tool invocations
    max_traversal_steps: int  # Safety ceiling (default 15)

    # ── Semantic Scenario Guidance (traversal → response) ──
    # Calculation Phase Steps + Simulator Phase Steps + Methodology from the
    # best-matched scenario; written by traversal_node, consumed by response_node.
    scenario_simulation_guidance: str

    # ── Response Agent ──
    final_response: str
    calculations: str  # Show-your-work for transparency
    data_summary: dict[str, Any]  # Structured data for downstream

    # ── Error handling ──
    errors: Annotated[list[str], operator.add]

    # ── Metadata ──
    created_at: str
    messages: Annotated[list[dict], operator.add]  # Conversation trace
