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
import time
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


def _print_phase_timings(timings: dict[str, float], total_ms: float) -> None:
    """Print a per-phase timing summary to the terminal after execution."""
    print("\n" + "─" * 52)
    print("  Phase Timing Summary")
    print("─" * 52)
    for node, ms in timings.items():
        secs = ms / 1000
        print(f"  {node:<22} {secs:>7.2f} s")
    print("─" * 52)
    print(f"  {'TOTAL':<22} {total_ms / 1000:>7.2f} s")
    print("─" * 52 + "\n")


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
        "planner_semantic_context": "",
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
    max_steps: int = 20,
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

    timings: dict[str, float] = {}
    t_start = time.perf_counter()
    t_prev = t_start

    for chunk in _graph.stream(initial_state, config=thread_config, stream_mode="updates"):
        t_now = time.perf_counter()
        for node_name in chunk:
            timings[node_name] = round((t_now - t_prev) * 1000, 1)
        t_prev = t_now

    total_ms = round((time.perf_counter() - t_start) * 1000, 1)
    _print_phase_timings(timings, total_ms)

    final_state = dict(_graph.get_state(thread_config).values)
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

    timings: dict[str, float] = {}
    t_start = time.perf_counter()
    t_prev = t_start

    for chunk in _graph.stream(Command(resume=user_clarification), config=thread_config, stream_mode="updates"):
        t_now = time.perf_counter()
        for node_name in chunk:
            timings[node_name] = round((t_now - t_prev) * 1000, 1)
        t_prev = t_now

    total_ms = round((time.perf_counter() - t_start) * 1000, 1)
    _print_phase_timings(timings, total_ms)

    final_state = dict(_graph.get_state(thread_config).values)
    logger.info("Simulation resumed and complete [thread=%s]", thread_id)

    return final_state


# ─────────────────────────────────────────────
# SSE streaming helpers
# ─────────────────────────────────────────────

_NODE_TO_EVENT: dict[str, str] = {
    "query_refiner":   "query_refiner_complete",
    "orchestrator":    "orchestrator_complete",
    "discover_schema": "schema_complete",
    "planner":         "planner_complete",
    "traversal":       "traversal_complete",
    "response":        "response_complete",
}


def _emit_node_event(query_id: str, node_name: str, state_delta: dict, mgr) -> None:
    """Map a LangGraph node update to an SSE event and push it via sse_manager."""
    event_name = _NODE_TO_EVENT.get(node_name)
    if not event_name:
        return

    data: dict = {}
    if node_name == "query_refiner":
        data = {"refined_query": state_delta.get("refined_query", "")}
    elif node_name == "orchestrator":
        data = {"routing_decision": state_delta.get("routing_decision", "")}
    elif node_name == "planner":
        data = {"planner_steps": state_delta.get("planner_steps", [])}
    elif node_name == "traversal":
        data = {"traversal_steps": state_delta.get("traversal_steps_taken", 0)}
    elif node_name == "response":
        data = {"final_response": state_delta.get("final_response", "")}

    mgr.put_sync(query_id, event_name, data)


def stream_simulation(
    query: str,
    query_id: str,
    thread_id: str,
    mgr,                 # SSEManager instance — passed in to avoid circular import
    max_steps: int = 20,
    on_hitl=None,        # optional callable(payload) invoked just before .wait()
) -> dict:
    """
    Stream the simulation graph end-to-end, pushing SSE events via mgr.put_sync().

    HITL handling (Option B — same stream stays open):
      1. Phase 1 streams until query_refiner calls interrupt().
      2. on_hitl(payload) is called (for DB writes from caller).
      3. A threading.Event is created and waited on — this blocks the executor
         thread but never touches the asyncio event loop.
      4. The resume endpoint calls mgr.signal_resume() which sets the event.
      5. Phase 2 streams the resumed graph to completion.

    Returns the final LangGraph state values dict.
    """
    thread_config = {"configurable": {"thread_id": thread_id}}
    initial_state = _make_initial_state(query, max_steps)

    logger.info(
        "Streaming simulation [thread=%s query=%s]: %.80s",
        thread_id, query_id, query,
    )

    # ── Phase 1: initial run ──────────────────────────────────────────────────
    timings: dict[str, float] = {}
    t_start = time.perf_counter()
    t_prev = t_start

    for chunk in _graph.stream(initial_state, config=thread_config, stream_mode="updates"):
        t_now = time.perf_counter()
        for node_name, state_delta in chunk.items():
            timings[node_name] = round((t_now - t_prev) * 1000, 1)
            _emit_node_event(query_id, node_name, state_delta, mgr)
        t_prev = t_now

    # ── Check for HITL interrupt ──────────────────────────────────────────────
    graph_state = _graph.get_state(thread_config)
    interrupt_payload = None
    for task in graph_state.tasks:
        if task.interrupts:
            interrupt_payload = task.interrupts[0].value
            break

    if interrupt_payload:
        if on_hitl:
            on_hitl(interrupt_payload)   # caller does DB writes here
        mgr.put_sync(query_id, "hitl_start", interrupt_payload)

        hitl_event = mgr.create_hitl_event(thread_id)
        logger.info("HITL pause — blocking stream thread [thread=%s]", thread_id)
        hitl_event.wait()                # blocks executor thread; event loop is free
        logger.info("HITL unblocked [thread=%s]", thread_id)

        answer = mgr.get_resume_answer(thread_id)
        mgr.put_sync(query_id, "hitl_complete", {"answer": answer})

        # ── Phase 2: resume ───────────────────────────────────────────────────
        from langgraph.types import Command
        t_prev = time.perf_counter()
        for chunk in _graph.stream(
            Command(resume=answer),
            config=thread_config,
            stream_mode="updates",
        ):
            t_now = time.perf_counter()
            for node_name, state_delta in chunk.items():
                timings[node_name] = round((t_now - t_prev) * 1000, 1)
                _emit_node_event(query_id, node_name, state_delta, mgr)
            t_prev = t_now

    total_ms = round((time.perf_counter() - t_start) * 1000, 1)
    _print_phase_timings(timings, total_ms)

    final_state = dict(_graph.get_state(thread_config).values)
    logger.info("Streaming complete [thread=%s query=%s]", thread_id, query_id)
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
