"""
Planner Agent — Multi-step parallel execution node.

Workflow:
  1. Fetch semantic context (KPIs, question bank, simulation scenarios).
  2. Use an LLM to decompose the user query into N focused sub-queries (plan steps).
  3. Execute each sub-query against the Traversal Agent in parallel via a
     ThreadPoolExecutor.
  4. Accumulate all traversal results and pass them to the Response Agent.
"""
from __future__ import annotations

import json
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.settings import config
from models.state import SimulationState
from agents.traversal import traversal_node
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


def _run_traversal_step(
    step_query: str,
    base_state: SimulationState,
    max_steps: int = _PLANNER_STEP_MAX_STEPS,
) -> dict:
    """
    Execute the traversal agent for a single planning step.

    Creates a copy of base_state with `user_query` and a capped
    `max_traversal_steps` so focused sub-queries don't over-iterate.
    `planner_semantic_context` in base_state lets the traversal agent
    skip its own redundant semantic API calls.
    """
    warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")
    step_state: SimulationState = {
        **base_state,
        "user_query": step_query,
        "max_traversal_steps": max_steps,
    }
    try:
        return traversal_node(step_state)
    except Exception as e:
        logger.error("Traversal step failed for query '%s': %s", step_query[:80], e)
        return {
            "traversal_findings": f"Step failed: {e}",
            "traversal_tool_calls": [],
            "traversal_steps_taken": 0,
            "errors": [f"Traversal step error: {e}"],
        }


def planner_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Planner Agent.

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

    # ── Step 2: LLM creates the plan ──────────────────────────────────────────
    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=0.0,
        max_tokens=2048,
    )

    # Escape any literal { } in dynamic content before calling str.format()
    # (Neo4j schema output and semantic context can contain Cypher/JSON brace patterns)
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
        # Strip the "Sub-query N: " prefix for a cleaner business-intent display
        display = step
        if ": " in step:
            display = step.split(": ", 1)[1]
        print(f"  {_CYAN}  Step {i}:{_RESET} {display}")
    print()

    # ── Step 3: Execute each step with traversal agent in parallel ────────────
    print(f"  {_BOLD}Executing {len(steps)} traversal(s) in parallel…{_RESET}\n")

    step_results: list[dict] = [{}] * len(steps)  # preserve order

    with ThreadPoolExecutor(max_workers=len(steps)) as executor:
        future_to_index = {
            executor.submit(_run_traversal_step, step, state): idx
            for idx, step in enumerate(steps)
        }

        done, not_done = wait(future_to_index.keys(), timeout=_STEP_TIMEOUT_SEC)

        for future in not_done:
            idx = future_to_index[future]
            logger.warning("Step %d timed out after %ds — using partial result", idx + 1, _STEP_TIMEOUT_SEC)
            step_results[idx] = {
                "traversal_findings": f"Step timed out after {_STEP_TIMEOUT_SEC}s",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
                "errors": [f"Step {idx + 1} timed out"],
            }

        for future in done:
            idx = future_to_index[future]
            try:
                step_results[idx] = future.result()
            except Exception as e:
                logger.error("Unexpected error in step %d: %s", idx + 1, e)
                step_results[idx] = {
                    "traversal_findings": f"Unexpected error: {e}",
                    "traversal_tool_calls": [],
                    "traversal_steps_taken": 0,
                }

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
        "planner_semantic_context": semantic_context,  # reused by sub-traversals; avoids redundant API calls
        "current_phase": "response",
        "messages": [{
            "agent": "planner",
            "content": (
                f"Planning complete: {len(steps)} steps executed in parallel, "
                f"{total_tool_calls} total traversal tool calls."
            ),
        }],
    }
