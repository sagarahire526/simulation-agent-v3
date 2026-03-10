"""
Planner Agent — Multi-step parallel execution node.

Workflow:
  1. Fetch semantic context (KPIs, question bank, simulation scenarios).
  2. Use an LLM to decompose the user query into N focused sub-queries (plan steps).
  3. Execute each sub-query against the Traversal Agent concurrently via asyncio.gather()
     running in a dedicated thread with its own event loop.
  4. Accumulate all traversal results and pass them to the Response Agent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.settings import config
from models.state import SimulationState
from agents.traversal import atraversal_node
from services.semantic_service import SemanticService
from prompts.planner_prompt import PLANNER_SYSTEM

logger = logging.getLogger(__name__)

_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_MAX_PARALLEL_STEPS = 6    # Hard cap — prompt targets 4-6 focused steps
_PLANNER_STEP_MAX_STEPS = 10  # Sub-steps are focused; rarely need more than 10 tool calls
_STEP_TIMEOUT_SEC = 120   # Kill a runaway sub-traversal after 2 minutes


def _parse_planner_response(content: str) -> tuple[str, list[str]]:
    """
    Parse the LLM's JSON plan output.
    Returns (planning_rationale, steps_list).
    Falls back to a single-step plan on parse failure.
    """
    try:
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        data = json.loads(clean.strip())
        rationale = data.get("planning_rationale", "")
        steps = data.get("steps", [])
        if not steps or not isinstance(steps, list):
            raise ValueError("No steps found in planner response")
        return rationale, [str(s) for s in steps if str(s).strip()]
    except (json.JSONDecodeError, ValueError, IndexError):
        logger.warning("Planner LLM returned non-JSON or empty steps; using single-step fallback.")
        return "Single-step fallback due to parse error.", []


async def _run_traversal_step_async(
    step_query: str,
    base_state: SimulationState,
    step_idx: int,
    max_steps: int = _PLANNER_STEP_MAX_STEPS,
) -> dict:
    """Run one planning step via the async traversal node."""
    warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")
    step_state: SimulationState = {
        **base_state,
        "user_query": step_query,
        "max_traversal_steps": max_steps,
    }
    try:
        return await atraversal_node(step_state)
    except Exception as e:
        logger.error("Traversal step %d failed for query '%s': %s", step_idx + 1, step_query[:80], e)
        return {
            "traversal_findings": f"Step failed: {e}",
            "traversal_tool_calls": [],
            "traversal_steps_taken": 0,
            "errors": [f"Traversal step error: {e}"],
        }


async def _gather_traversals(steps: list[str], state: SimulationState) -> list:
    """
    Run all traversal steps concurrently inside a single asyncio event loop.
    Each step uses agent.ainvoke() so they truly overlap during LLM I/O waits.
    """
    tasks = [
        asyncio.wait_for(
            _run_traversal_step_async(step, state, idx),
            timeout=float(_STEP_TIMEOUT_SEC),
        )
        for idx, step in enumerate(steps)
    ]
    return await asyncio.gather(*tasks, return_exceptions=True)


def planner_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Planner Agent (sync — required by LangGraph's sync stream API).

    Reads:  refined_query, kg_schema, max_traversal_steps
    Writes: planner_steps, planner_step_results,
            scenario_simulation_guidance, current_phase, messages
    """
    refined_query = state.get("refined_query") or state["user_query"]
    kg_schema = state.get("kg_schema", "Schema not available")

    print(f"\n{_BOLD}{'═' * 70}")
    print(f"  📋 PLANNER AGENT — Decomposing query into parallel steps")
    print(f"{'═' * 70}{_RESET}\n")
    print(f"  {_DIM}Query: {refined_query}{_RESET}\n")

    # ── Step 1: Fetch semantic context for planning guidance ──────────────────
    semantic_context = ""
    simulation_guidance = ""
    try:
        semantic = SemanticService()
        context_data = semantic.get_all_context(refined_query)

        total_hits = sum(len(v) for v in context_data.values())
        if total_hits:
            semantic_context = semantic.format_traversal_context(context_data)
            simulation_guidance = semantic.format_simulation_guidance(context_data)
            kpi_hits = len(context_data.get("kpi", []))
            qb_hits  = len(context_data.get("question_bank", []))
            sim_hits = len(context_data.get("simulation", []))
            print(
                f"  {_GREEN}🎯 Semantic context: "
                f"{kpi_hits} KPI · {qb_hits} Q&A · {sim_hits} scenario(s){_RESET}"
            )
        else:
            print(f"  {_DIM}ℹ  No semantic context (API may be unreachable).{_RESET}")
    except Exception as e:
        logger.warning("Semantic search in planner failed (non-fatal): %s", e)

    # ── Step 2: LLM creates the plan (fast model — just query decomposition) ──
    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=0.0,
        max_tokens=2048,
    )

    # Escape any literal { } in dynamic content before calling str.format()
    safe_kg_schema = kg_schema.replace("{", "{{").replace("}", "}}")
    safe_semantic = semantic_context.replace("{", "{{").replace("}", "}}")

    planning_prompt = PLANNER_SYSTEM.format(
        kg_schema=safe_kg_schema,
        semantic_context=safe_semantic,
    )

    llm_response = llm.invoke([
        SystemMessage(content=planning_prompt),
        HumanMessage(content=refined_query),
    ])

    rationale, steps = _parse_planner_response(llm_response.content)

    # Safety: if parsing failed, fall back to single traversal step on the full query
    if not steps:
        steps = [f"Sub-query 1: {refined_query}"]

    # Cap the number of parallel steps
    steps = steps[:_MAX_PARALLEL_STEPS]

    print(f"\n  {_BOLD}Business Analysis Plan ({len(steps)} steps):{_RESET}")
    if rationale:
        print(f"  {_YELLOW}📌 Intent:{_RESET} {rationale}\n")
    for i, step in enumerate(steps, 1):
        display = step
        if ": " in step:
            display = step.split(": ", 1)[1]
        print(f"  {_CYAN}  Step {i}:{_RESET} {display}")
    print()

    # ── Step 3: Execute each step concurrently ────────────────────────────────
    # Strategy: run asyncio.gather() inside a dedicated thread that owns its own
    # event loop. This avoids "cannot call asyncio.run() from a running loop"
    # errors that occur when LangGraph's sync runner has its own internal loop,
    # while still getting true async concurrency across all traversal sub-steps.
    print(f"  {_BOLD}Executing {len(steps)} traversal(s) in parallel…{_RESET}\n")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="planner-async") as executor:
        future = executor.submit(asyncio.run, _gather_traversals(steps, state))
        gathered = future.result(timeout=_STEP_TIMEOUT_SEC + 30)

    step_results: list[dict] = []
    for idx, result in enumerate(gathered):
        if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
            logger.warning("Step %d timed out after %ds", idx + 1, _STEP_TIMEOUT_SEC)
            step_results.append({
                "traversal_findings": f"Step timed out after {_STEP_TIMEOUT_SEC}s",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
                "errors": [f"Step {idx + 1} timed out"],
            })
        elif isinstance(result, Exception):
            logger.error("Unexpected error in step %d: %s", idx + 1, result)
            step_results.append({
                "traversal_findings": f"Unexpected error: {result}",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
            })
        else:
            step_results.append(result)

    total_tool_calls = sum(
        r.get("traversal_steps_taken", 0) for r in step_results
    )
    print(f"\n  {_GREEN}✅ All steps complete — {total_tool_calls} total tool calls{_RESET}\n")

    logger.info(
        "Planner completed: %d steps, %d total tool calls",
        len(steps), total_tool_calls,
    )

    return {
        "planning_rationale": rationale,
        "planner_steps": steps,
        "planner_step_results": step_results,
        "scenario_simulation_guidance": simulation_guidance,
        "planner_semantic_context": semantic_context,
        "current_phase": "response",
        "messages": [{
            "agent": "planner",
            "content": (
                f"Planning complete: {len(steps)} steps executed in parallel, "
                f"{total_tool_calls} total traversal tool calls."
            ),
        }],
    }
