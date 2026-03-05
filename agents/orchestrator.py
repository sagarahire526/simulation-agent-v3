"""
Orchestrator Agent — Routing node.

Receives the finalised query from the Query Refiner and decides which
downstream pipeline to activate:

  • "greeting"    → Sets final_response directly; graph terminates immediately.
  • "traversal"   → Simple data lookup; routes to schema discovery → traversal → response.
  • "simulation"  → Complex scenario; routes to schema discovery → planner → response.

The orchestrator does NOT retrieve any data itself — it only classifies and routes.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.settings import config
from models.state import SimulationState
from prompts.orchestrator_prompt import ORCHESTRATOR_SYSTEM

logger = logging.getLogger(__name__)

_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_VALID_ROUTES = {"greeting", "traversal", "simulation"}


def _parse_orchestrator_response(content: str) -> dict:
    """
    Parse the LLM's JSON routing decision.
    Falls back to "simulation" on any parse error (safest default).
    """
    try:
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip())
    except (json.JSONDecodeError, IndexError):
        logger.warning("Orchestrator LLM returned non-JSON; defaulting to simulation route.")
        return {
            "routing_decision": "simulation",
            "reasoning": "Could not parse routing decision; defaulting to simulation.",
            "direct_response": None,
        }


def orchestrator_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Orchestrator Agent.

    Reads:  refined_query
    Writes: routing_decision, routing_context (greeting only),
            final_response (greeting only), current_phase, messages
    """
    refined_query = state.get("refined_query") or state["user_query"]

    print(f"\n{_BOLD}{'═' * 70}")
    print(f"  🎯 ORCHESTRATOR — Routing query to the right pipeline")
    print(f"{'═' * 70}{_RESET}\n")
    print(f"  {_DIM}Query: {refined_query}{_RESET}\n")

    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=0.0,
        max_tokens=512,
    )

    response = llm.invoke([
        SystemMessage(content=ORCHESTRATOR_SYSTEM),
        HumanMessage(content=refined_query),
    ])

    parsed = _parse_orchestrator_response(response.content)
    routing_decision: str = parsed.get("routing_decision", "simulation")
    reasoning: str = parsed.get("reasoning", "")
    direct_response: str | None = parsed.get("direct_response")

    # Ensure we only use known routes
    if routing_decision not in _VALID_ROUTES:
        routing_decision = "simulation"

    route_color = {
        "greeting": _YELLOW,
        "traversal": _CYAN,
        "simulation": _GREEN,
    }.get(routing_decision, _GREEN)

    print(f"  {route_color}→ Route: {routing_decision.upper()}{_RESET}")
    print(f"  {_DIM}Reason: {reasoning}{_RESET}\n")

    result: dict[str, Any] = {
        "routing_decision": routing_decision,
        "routing_context": direct_response or "",
        "current_phase": "orchestration",
        "messages": [{
            "agent": "orchestrator",
            "content": f"Routed to '{routing_decision}'. Reason: {reasoning}",
        }],
    }

    # For greeting: set final_response so the graph can end immediately
    if routing_decision == "greeting" and direct_response:
        result["final_response"] = direct_response
        result["current_phase"] = "complete"

    return result
