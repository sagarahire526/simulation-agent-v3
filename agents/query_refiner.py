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

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.types import interrupt

from models.state import SimulationState
from services.llm_provider import LLMProvider
from services.entity_lookup_service import get_all_entity_lookups
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

    print(f"\n{_BOLD}{'═' * 70}", flush=True)
    print(f"  🔍 QUERY REFINER — Evaluating query completeness", flush=True)
    print(f"{'═' * 70}{_RESET}\n", flush=True)
    print(f"  {_DIM}Query: {user_query}{_RESET}\n", flush=True)

    llm = LLMProvider.get_llm("fast", max_tokens=1024)

    # Fetch formal entity names from DB for name normalization
    lookups = get_all_entity_lookups()
    print(f"LOOKUPS ARE AS FOLLOWS: {lookups}")
    system_prompt = QUERY_REFINER_SYSTEM.replace(
        "{{gc_names}}", ", ".join(lookups["gc_names"]) if lookups["gc_names"] else "(not available)"
    ).replace(
        "{{market_names}}", ", ".join(lookups["markets"]) if lookups["markets"] else "(not available)"
    ).replace(
        "{{region_names}}", ", ".join(lookups["regions"]) if lookups["regions"] else "(not available)"
    )

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_query),
    ])

    parsed = _parse_refiner_response(response.content)
    is_complete: bool = parsed.get("is_complete", True)
    clarification_questions: list[str] = parsed.get("clarification_questions", [])
    assumptions: list[str] = parsed.get("assumptions", [])
    refined_query: str = parsed.get("refined_query", user_query) or user_query

    if assumptions:
        print(f"  {_DIM}Assumptions: {' | '.join(assumptions)}{_RESET}", flush=True)

    if is_complete:
        print(f"  {_GREEN}✓ Query is complete — proceeding to orchestrator.{_RESET}\n", flush=True)
        return {
            "refined_query": refined_query,
            "current_phase": "orchestration",
            "messages": [{
                "agent": "query_refiner",
                "content": f"Query accepted as complete. Refined: {refined_query}",
            }],
        }

    # ── Query is incomplete → ask the user for clarification ──────────────────
    print(f"  {_YELLOW}⚠ Query needs clarification:{_RESET}", flush=True)
    for q in clarification_questions:
        print(f"     • {q}", flush=True)
    print(flush=True)

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
    accepted_assumptions = (
        user_clarification is not None
        and user_clarification.strip().lower() == "accept stated assumptions"
    )

    if accepted_assumptions:
        # User clicked "Accept stated assumptions". Hierarchy is region → market → area,
        # so broaden the unspecified level to ALL valid values rather than letting the
        # LLM collapse it to a single default (e.g. CENTRAL only). The LLM still merges
        # the original query with the entity lists, but with explicit broadening rules.
        broaden_prompt = (
            f"Original query: {user_query}\n"
            f"Clarification questions that were asked: {clarification_questions}\n"
            f"Stated assumptions offered to user: {assumptions}\n"
            "User's answer: 'Accept stated assumptions' — this means BROADEN the "
            "unspecified scope to ALL valid values at the level that was asked. Do NOT "
            "pick a single default value.\n\n"
            "Rules for the refined_query:\n"
            "1. Preserve ALL quantitative facts from the original query verbatim "
            "   (rates, counts, targets, time windows, percentages).\n"
            "2. For every clarification question about geography/scope, expand to ALL "
            "   valid values at that level. Hierarchy is region → market → area:\n"
            "   - Asked 'which region?' with no region given → cover ALL regions "
            "     (WEST, SOUTH, CENTRAL).\n"
            "   - User said 'SOUTH region' and was asked 'which market within SOUTH?' "
            "     → cover ALL markets that belong to SOUTH region.\n"
            "   - User said 'CHICAGO market' and was asked 'which area within CHICAGO?' "
            "     → cover ALL areas under CHICAGO market.\n"
            "   - Asked 'which GC?' → cover ALL GCs in the established scope.\n"
            "3. Resolve entity names to their formal DB values.\n"
            "4. Do NOT add 'will be retrieved from the database' assumptions for values "
            "   the user already provided.\n"
            "5. Set is_complete=true."
        )
        resume_response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=broaden_prompt),
        ])
        resume_parsed = _parse_refiner_response(resume_response.content)
        refined_query = resume_parsed.get("refined_query", "") or (
            refined_query or user_query
        )
        print(f"  {_GREEN}✓ Assumptions accepted (scope broadened). Refined query:{_RESET}", flush=True)
        print(f"     {refined_query}\n", flush=True)
    elif user_clarification and user_clarification.strip():
        # Re-run the LLM with the original query + clarification + entity lists
        # so it can properly resolve entity names in the refined query.
        merge_prompt = (
            f"Original query: {user_query}\n"
            f"Clarification questions asked: {clarification_questions}\n"
            f"User's answer: {user_clarification.strip()}\n\n"
            "Produce the final refined_query. CRITICAL — follow the Preservation Rule "
            "from your system prompt: preserve ALL quantitative facts from the original "
            "query VERBATIM (ra~tes, counts, targets, time windows, percentages, named "
            "values the user gave as ground truth). Only add the clarification answer "
            "(geography) and resolve entity names(market, gc's, regions and areas) to their formal DB values."
            "Do NOT summarize, condense, or strip the user's stated numbers. Do NOT list "
            "'will be retrieved from the database' assumptions for any value the user "
            "already provided. Set is_complete=true."
        )
        resume_response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=merge_prompt),
        ])
        resume_parsed = _parse_refiner_response(resume_response.content)
        refined_query = resume_parsed.get("refined_query", "") or (
            f"{user_query} — Additional context: {user_clarification.strip()}"
        )
        print(f"  {_GREEN}✓ Clarification received. Refined query:{_RESET}", flush=True)
        print(f"     {refined_query}\n", flush=True)
    else:
        # User pressed Enter / sent empty answer; use the LLM's refined version as-is
        refined_query = refined_query or user_query
        print(f"  {_DIM}No clarification provided — proceeding with assumptions.{_RESET}\n", flush=True)

    return {
        "refined_query": refined_query,
        "current_phase": "orchestration",
        "messages": [{
            "agent": "query_refiner",
            "content": f"Query refined after clarification. Final: {refined_query}",
        }],
    }
