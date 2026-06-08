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
    refined_query: str           # Finalized query after query_refiner validation
    project_type: str            # "NTM", "AHLOB Modernization", or "NTM,AHLOB Modernization" — maps to smp_name filter

    # ── Phase tracking ──
    current_phase: Literal[
        "query_refinement", "orchestration", "discovery",
        "planning", "traversal", "response", "complete", "error"
    ]

    # ── Orchestrator routing ──
    routing_decision: str        # "greeting" | "simulation" | "traversal"
    routing_context: str         # For greeting: direct response text set by orchestrator

    # ── Planner Agent ──
    planning_rationale: str                                     # Business-intent rationale for the plan
    planner_steps: list[str]                                    # Ordered steps created by planner
    planner_step_results: Annotated[list[dict], operator.add]  # Results from parallel traversals

    # ── Knowledge Graph Schema (discovered once) ──
    kg_schema: str  # Node labels, relationships, properties

    # ── Pre-fetched semantic context (set by planner; reused by sub-traversals) ──
    planner_semantic_context: str
    semantic_analysis: dict[str, list]  # Heading-only summary of semantic search hits

    # ── Traversal Agent ──
    traversal_findings: str  # Agent's natural-language summary of what it found
    traversal_tool_calls: Annotated[list[ToolCallRecord], operator.add]
    traversal_steps_taken: int  # Number of tool invocations
    max_traversal_steps: int  # Safety ceiling (default 15)

    # ── Semantic Scenario Guidance (traversal/planner → response) ──
    scenario_simulation_guidance: str
    # True if GCL sim or internal library had a match ≥ 0.8; False if neither;
    # None until the planner runs. Persisted via checkpointer so the UI can
    # render a no-match notice when the user re-opens the question.
    scenario_match_found: Optional[bool]

    # ── Response Agent ──
    final_response: str
    current_status: list[str]  # Sibling field: flattened rows from the markdown's "## Current Status" table (e.g. "Total Sites: 300"). [] for TYPE 1 / greeting.
    execution_algorithm: str  # Numbered step-by-step narrative of how the system answered the query
    calculations: str  # Show-your-work for transparency
    data_summary: dict[str, Any]  # Structured data for downstream
    graph_data: dict[str, Any]  # Highcharts-compatible chart JSON for visualization

    # ── Error handling ──
    errors: Annotated[list[str], operator.add]

    # ── Metadata ──
    created_at: str
    messages: Annotated[list[dict], operator.add]  # Conversation trace
