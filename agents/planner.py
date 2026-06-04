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
import contextvars
import json
import logging
import warnings
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage

from models.state import SimulationState
from services.llm_provider import LLMProvider
from agents.traversal import atraversal_node
from services.semantic_service import SemanticService
from services import internal_scenarios as scenario_lib
from services.sse_context import emit_sse
from prompts.planner_prompt import PLANNER_SYSTEM
from services.date_context import today_date_context

logger = logging.getLogger(__name__)

_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_MAX_PARALLEL_STEPS = 6    # Hard cap — prompt targets 4-6 focused steps
_PLANNER_STEP_MAX_STEPS = 10  # Budget: get_kpi + run_sql_python + 3 retries + get_node fallback + run_sql_python + spare
_STEP_TIMEOUT_SEC = 300   # Kill a runaway sub-traversal after 5 minutes

# Shared executor for running asyncio event loops from sync planner nodes.
# Bounded to 4 threads so 100 concurrent requests don't spawn 100 OS threads.
_planner_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="planner-async")



def _fetch_gcl_context(query: str) -> dict:
    """
    Run the GCL semantic search and produce the planner-facing strings.
    Always returns a dict with stable keys; on error every field is empty.
    """
    try:
        semantic = SemanticService()
        data = semantic.get_all_context(query)
        return {
            "formatted": semantic.format_traversal_context(data) if any(data.values()) else "",
            "guidance":  semantic.format_simulation_guidance(data) if any(data.values()) else "",
            "analysis":  SemanticService.extract_headings(data) if any(data.values()) else {},
            "hits":      (
                len(data.get("kpi", [])),
                len(data.get("question_bank", [])),
                len(data.get("simulation", [])),
            ),
        }
    except Exception as e:
        logger.warning("GCL semantic search failed (non-fatal): %s", e)
        return {"formatted": "", "guidance": "", "analysis": {}, "hits": (0, 0, 0)}


def _fetch_internal_scenarios(query: str) -> list[dict]:
    """
    Run the Internal Scenario Library lookup. Returns [] on any error so the
    planner can proceed with GCL-only context.
    """
    try:
        return scenario_lib.search(query)
    except Exception as e:
        logger.warning("Internal scenario search failed (non-fatal): %s", e)
        return []


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
        "refined_query": step_query,
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
    Emits SSE planner_step_complete events as each step finishes.
    """
    # Wrap each step in a task that tags the result with its index
    async def _tagged_step(idx: int, step: str) -> tuple[int, dict | Exception]:
        try:
            result = await asyncio.wait_for(
                _run_traversal_step_async(step, state, idx),
                timeout=float(_STEP_TIMEOUT_SEC),
            )
            return idx, result
        except Exception as exc:
            return idx, exc

    pending = [_tagged_step(i, s) for i, s in enumerate(steps)]
    results: list[dict | Exception] = [None] * len(steps)  # type: ignore[list-item]

    for coro in asyncio.as_completed(pending):
        step_idx, result = await coro
        results[step_idx] = result

        # Emit SSE progress event
        step_query = steps[step_idx]
        display_query = step_query.split(": ", 1)[1] if ": " in step_query else step_query
        is_error = isinstance(result, Exception)
        emit_sse("planner_step_complete", {
            "step_index": step_idx,
            "step_total": len(steps),
            "step_query": display_query,
            "status": "error" if is_error else "complete",
            "error": str(result) if is_error else None,
        })

    return results


def planner_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Planner Agent (sync — required by LangGraph's sync stream API).

    Reads:  refined_query, max_traversal_steps
    Writes: planner_steps, planner_step_results,
            scenario_simulation_guidance, current_phase, messages
    """
    refined_query = state.get("refined_query") or state["user_query"]

    print(f"\n{_BOLD}{'═' * 70}", flush=True)
    print(f"  📋 PLANNER AGENT — Decomposing query into parallel steps", flush=True)
    print(f"{'═' * 70}{_RESET}\n", flush=True)
    print(f"  {_DIM}Query: {refined_query}{_RESET}\n", flush=True)

    # ── Step 1: Fetch semantic context for planning guidance ──────────────────
    semantic_context = ""
    simulation_guidance = ""
    semantic_analysis: dict[str, list[str]] = {}
    internal_matches: list[dict] = []

    # Run GCL semantic search and the local Internal Scenario Library lookup
    # in parallel — both feed the planner via the same semantic_context block,
    # and the planner picks the source with the higher similarity score.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="planner-ctx") as ctx_pool:
        gcl_future = ctx_pool.submit(_fetch_gcl_context, refined_query)
        internal_future = ctx_pool.submit(_fetch_internal_scenarios, refined_query)

        try:
            gcl = gcl_future.result()
            semantic_context     = gcl["formatted"]
            simulation_guidance  = gcl["guidance"]
            semantic_analysis    = gcl["analysis"]
            kpi_hits, qb_hits, sim_hits = gcl["hits"]
            if any([kpi_hits, qb_hits, sim_hits]):
                print(
                    f"  {_GREEN}🎯 Semantic context: "
                    f"{kpi_hits} KPI · {qb_hits} Q&A · {sim_hits} scenario(s){_RESET}",
                    flush=True,
                )
            else:
                print(f"  {_DIM}ℹ  No semantic context (API may be unreachable).{_RESET}", flush=True)
        except Exception as e:
            logger.warning("Semantic search in planner failed (non-fatal): %s", e)

        try:
            internal_matches = internal_future.result()
            if internal_matches:
                top = internal_matches[0]
                print(
                    f"  {_GREEN}📚 Internal Library: "
                    f"matched '{top.get('tag')}' "
                    f"(similarity {top['similarity_score'] * 100:.1f}%){_RESET}",
                    flush=True,
                )
            else:
                print(
                    f"  {_DIM}ℹ  Internal Library: no match "
                    f"(threshold {scenario_lib.MIN_SIMILARITY:.2f}).{_RESET}",
                    flush=True,
                )
        except Exception as e:
            logger.warning("Internal scenario lookup failed (non-fatal): %s", e)

    # Splice the internal-library block onto the GCL block so the planner sees
    # both signals with their similarity scores under the same Mode A logic.
    if internal_matches:
        internal_block = scenario_lib.format_for_planner(internal_matches)
        if internal_block:
            semantic_context = (
                (semantic_context + "\n\n" if semantic_context else "")
                + internal_block
            )

    # ── Step 2: LLM creates the plan (planner tier — strong reasoning for
    #            fact-vs-gap judgement and step decomposition) ──
    llm = LLMProvider.get_llm("planner")

    # Escape any literal { } in dynamic content before calling str.format()
    safe_semantic = semantic_context.replace("{", "{{").replace("}", "}}")

    planning_prompt = PLANNER_SYSTEM.format(
        today_date=today_date_context(),
        semantic_context=safe_semantic,
    )

    llm_response = llm.bind(response_format={"type": "json_object"}).invoke([
        SystemMessage(content=planning_prompt),
        HumanMessage(content=refined_query),
    ])

    rationale, steps = _parse_planner_response(llm_response.content)

    # Safety: if parsing failed, fall back to single traversal step on the full query
    if not steps:
        steps = [f"Sub-query 1: {refined_query}"]

    # Cap the number of parallel steps
    steps = steps[:_MAX_PARALLEL_STEPS]

    print(f"\n  {_BOLD}Business Analysis Plan ({len(steps)} steps):{_RESET}", flush=True)
    if rationale:
        print(f"  {_YELLOW}📌 Intent:{_RESET} {rationale}\n", flush=True)
    display_steps = []
    for i, step in enumerate(steps, 1):
        display = step
        if ": " in step:
            display = step.split(": ", 1)[1]
        print(f"  {_CYAN}  Step {i}:{_RESET} {display}", flush=True)
        display_steps.append(display)
    print(flush=True)

    # ── SSE: plan is ready, sub-queries about to start ────────────────────────
    emit_sse("planner_plan_ready", {
        "step_total": len(steps),
        "steps": display_steps,
        "rationale": rationale,
    })

    # ── Step 3: Execute each step concurrently ────────────────────────────────
    # Strategy: run asyncio.gather() inside a dedicated thread that owns its own
    # event loop. This avoids "cannot call asyncio.run() from a running loop"
    # errors that occur when LangGraph's sync runner has its own internal loop,
    # while still getting true async concurrency across all traversal sub-steps.
    print(f"  {_BOLD}Executing {len(steps)} traversal(s) in parallel…{_RESET}\n", flush=True)

    # Inject semantic context into state so sub-traversals can reuse it
    # (the planner's return value sets this field too late for the sub-traversals)
    traversal_state: SimulationState = {
        **state,
        "planner_semantic_context": semantic_context,
        "scenario_simulation_guidance": simulation_guidance,
    }

    ctx = contextvars.copy_context()
    future = _planner_executor.submit(ctx.run, asyncio.run, _gather_traversals(steps, traversal_state))
    gathered = future.result(timeout=_STEP_TIMEOUT_SEC + 60)

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
    print(f"\n  {_GREEN}✅ All steps complete — {total_tool_calls} total tool calls{_RESET}\n", flush=True)

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
        "semantic_analysis": semantic_analysis,
        "current_phase": "response",
        "messages": [{
            "agent": "planner",
            "content": (
                f"Planning complete: {len(steps)} steps executed in parallel, "
                f"{total_tool_calls} total traversal tool calls."
            ),
        }],
    }
