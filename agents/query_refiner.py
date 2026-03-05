"""
Query Refiner Agent — Human-in-the-Loop node.

Analyses the user's raw query for completeness (required params present?).
If the query is under-specified, the node suspends the graph via LangGraph's
`interrupt()` mechanism and waits for the user to supply clarification.
Once the query is well-defined, it forwards the finalised query to the
Orchestrator Agent.

Human-in-the-Loop flow:
  1. LLM evaluates the query.
  2. If complete → set refined_query and advance.
  3. If incomplete → `interrupt()` with clarification questions + assumptions.
  4. Caller resumes the graph with `Command(resume=<user_clarification_text>)`.
  5. Node merges the user's clarification with the original query → refined_query.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.types import interrupt

from config.settings import config
from models.state import SimulationState
from prompts.query_refiner_prompt import QUERY_REFINER_SYSTEM

logger = logging.getLogger(__name__)

_CYAN  = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_RESET = "\033[0m"


def _parse_refiner_response(content: str) -> dict:
    """
    Parse the LLM's JSON output from the query refiner.
    Returns a safe default dict on any parse failure.
    """
    try:
        # Strip markdown fences if the LLM added them despite instructions
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip())
    except (json.JSONDecodeError, IndexError):
        logger.warning("Query refiner LLM returned non-JSON; treating query as complete.")
        return {
            "is_complete": True,
            "clarification_questions": [],
            "assumptions": [],
            "refined_query": "",
        }


def query_refiner_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Query Refiner Agent (Human-in-the-Loop).

    Reads:  user_query
    Writes: refined_query, current_phase, messages
    May interrupt the graph to ask clarifying questions.
    """
    user_query = state["user_query"]

    print(f"\n{_BOLD}{'═' * 70}")
    print(f"  🔍 QUERY REFINER — Evaluating query completeness")
    print(f"{'═' * 70}{_RESET}\n")
    print(f"  {_DIM}Query: {user_query}{_RESET}\n")

    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=0.0,
        max_tokens=1024,
    )

    response = llm.invoke([
        SystemMessage(content=QUERY_REFINER_SYSTEM),
        HumanMessage(content=user_query),
    ])

    parsed = _parse_refiner_response(response.content)
    is_complete: bool = parsed.get("is_complete", True)
    clarification_questions: list[str] = parsed.get("clarification_questions", [])
    assumptions: list[str] = parsed.get("assumptions", [])
    refined_query: str = parsed.get("refined_query", user_query) or user_query

    if assumptions:
        print(f"  {_DIM}Assumptions: {' | '.join(assumptions)}{_RESET}")

    if is_complete:
        print(f"  {_GREEN}✓ Query is complete — proceeding to orchestrator.{_RESET}\n")
        return {
            "refined_query": refined_query,
            "current_phase": "orchestration",
            "messages": [{
                "agent": "query_refiner",
                "content": f"Query accepted as complete. Refined: {refined_query}",
            }],
        }

    # ── Query is incomplete → ask the user for clarification ──────────────────
    print(f"  {_YELLOW}⚠ Query needs clarification:{_RESET}")
    for q in clarification_questions:
        print(f"     • {q}")
    print()

    # Build a user-facing clarification prompt
    clarification_prompt = {
        "type": "clarification_needed",
        "original_query": user_query,
        "questions": clarification_questions,
        "assumptions_if_skipped": assumptions,
        "message": (
            "Your query needs a bit more detail to run a precise simulation. "
            "Please answer the questions below (or press Enter to accept assumptions):"
        ),
    }

    # Suspend graph — caller must resume with Command(resume=<user_text>)
    user_clarification: str = interrupt(clarification_prompt)

    # ── Graph resumed with user's clarification ────────────────────────────────
    if user_clarification and user_clarification.strip():
        refined_query = (
            f"{user_query} — Additional context: {user_clarification.strip()}"
        )
        print(f"  {_GREEN}✓ Clarification received. Refined query:{_RESET}")
        print(f"     {refined_query}\n")
    else:
        # User accepted assumptions; use the LLM's refined version as-is
        refined_query = refined_query or user_query
        print(f"  {_DIM}No clarification provided — proceeding with assumptions.{_RESET}\n")

    return {
        "refined_query": refined_query,
        "current_phase": "orchestration",
        "messages": [{
            "agent": "query_refiner",
            "content": f"Query refined after clarification. Final: {refined_query}",
        }],
    }
