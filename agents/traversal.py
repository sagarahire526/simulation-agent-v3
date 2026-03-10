"""
Traversal Agent — Autonomous ReAct agent that explores the Neo4j
Knowledge Graph using tools to gather data needed to answer the
user's query.
"""
from __future__ import annotations

import json
import time
import logging
import warnings
from typing import Any

from langgraph.prebuilt import create_react_agent

from models.state import SimulationState, ToolCallRecord
from services.llm_provider import LLMProvider
from tools.langchain_tools import get_all_tools
from prompts.traversal_prompt import TRAVERSAL_SYSTEM
from services.semantic_service import SemanticService

logger = logging.getLogger(__name__)

# Suppress noisy Neo4j deprecation warnings
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

DEFAULT_MAX_STEPS = 15

# ─── ANSI colors for terminal output ───
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _print_divider(char: str = "─", width: int = 70):
    print(f"{_DIM}{char * width}{_RESET}")


def _print_tool_call(step_num: int, tool_name: str, tool_input: dict):
    """Print a tool call in a readable format."""
    _print_divider()
    print(f"{_BOLD}{_CYAN}  🔧 Step {step_num}: {tool_name}{_RESET}")

    # Format input — print full code for sandbox tools, truncate others
    for key, val in tool_input.items():
        val_str = str(val)
        if key == "code" and tool_name in ("run_sql_python", "run_python"):
            # Print full SQL/Python code — never truncate
            print(f"     {_DIM}{key}:{_RESET}")
            for line in val_str.splitlines():
                print(f"       {_DIM}{line}{_RESET}")
        else:
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            print(f"     {_DIM}{key}:{_RESET} {val_str}")


def _print_tool_result(status: str, output: str):
    """Print a tool result in a readable format."""
    if status == "error":
        icon, color = "✗", _RED
    else:
        icon, color = "✓", _GREEN

    # Try to pretty-print JSON output
    display = output
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            # Show key summary instead of raw JSON dump
            if "records" in parsed:
                count = parsed.get("count", len(parsed["records"]))
                display = f"{count} records returned"
                if parsed["records"] and count <= 5:
                    display += "\n" + json.dumps(parsed["records"], indent=2, default=str)
                elif parsed["records"]:
                    display += f" (showing first 3)\n" + json.dumps(
                        parsed["records"][:3], indent=2, default=str
                    )
            elif "relevant_nodes" in parsed:
                nodes = parsed["relevant_nodes"]
                metrics = parsed.get("relevant_metrics", [])
                display = f"{len(nodes)} nodes, {len(metrics)} metrics found"
                for n in nodes[:5]:
                    display += f"\n     • {n.get('node_id', '?')} — {(n.get('definition') or '')[:80]}"
            elif "error" in parsed:
                display = f"Error: {parsed['error']}"
                if parsed.get("traceback"):
                    display += f"\nTraceback:\n{parsed['traceback']}"
                status = "error"
            elif "paths" in parsed:
                paths = parsed["paths"]
                display = f"{len(paths)} paths found"
                for p in paths[:5]:
                    display += f"\n     • ({p.get('from')})─[:{p.get('relationship')}]→({p.get('to')})"
            elif "status" in parsed and parsed["status"] == "success":
                result_val = parsed.get("result", parsed.get("output", ""))
                display = f"Success: {json.dumps(result_val, default=str)[:300]}"
            else:
                display = json.dumps(parsed, indent=2, default=str)
                if len(display) > 1500:
                    display = display[:1500] + "\n     ...(truncated)"
        else:
            display = str(parsed)
            if len(display) > 1500:
                display = display[:1500] + "...(truncated)"
    except (json.JSONDecodeError, TypeError):
        if len(display) > 1500:
            display = display[:1500] + "...(truncated)"

    color_out = _RED if status == "error" else _GREEN
    print(f"     {color_out}{icon} Result:{_RESET} {display}")


def _print_agent_thinking(content: str):
    """Print the agent's reasoning text."""
    if not content.strip():
        return
    # Truncate very long reasoning
    text = content.strip()
    if len(text) > 400:
        text = text[:400] + "..."
    print(f"  {_YELLOW}💭 Agent:{_RESET} {text}")


def _extract_and_print(messages: list) -> tuple[list[ToolCallRecord], str]:
    """
    Walk the agent message history, print each step live-style,
    and return (tool_call_records, findings).
    """
    records: list[ToolCallRecord] = []
    step_num = 0
    findings = "No findings extracted."

    print(f"\n{_BOLD}{'═' * 70}")
    print(f"  🔍 TRAVERSAL AGENT — Exploring Knowledge Graph")
    print(f"{'═' * 70}{_RESET}\n")

    for msg in messages:
        # Agent reasoning or final answer
        if msg.type == "ai":
            # Print reasoning text (if any, before tool calls)
            text = getattr(msg, "content", "") or ""
            if text.strip() and not getattr(msg, "tool_calls", None):
                _print_agent_thinking(text)
                findings = text  # Last AI message without tool calls = findings

            # Tool calls
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    step_num += 1
                    _print_tool_call(step_num, tc["name"], tc["args"])
                    records.append(ToolCallRecord(
                        tool_name=tc["name"],
                        tool_input=tc["args"],
                        tool_output="",
                        status="success",
                        execution_time_ms=0,
                    ))

        # Tool results
        elif msg.type == "tool":
            output = msg.content or ""
            # Match to the last record with empty output
            for rec in reversed(records):
                if rec["tool_output"] == "":
                    rec["tool_output"] = output
                    if "error" in output.lower()[:200]:
                        rec["status"] = "error"
                    _print_tool_result(rec["status"], output)
                    break

    _print_divider("═")
    print(f"  {_BOLD}✅ Traversal complete: {step_num} tool calls{_RESET}")
    _print_divider("═")
    print()

    return records, findings


def traversal_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Autonomous Traversal Agent.

    Reads: user_query, kg_schema, max_traversal_steps
    Writes: traversal_findings, traversal_tool_calls, traversal_steps_taken,
            current_phase, messages, errors
    """
    # Suppress pandas SQLAlchemy warnings
    warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

    llm = LLMProvider.get_llm("default")

    # Build system prompt with KG schema injected
    kg_schema = state.get("kg_schema", "Schema not available")

    # ── Semantic search: KPI + Question Bank + Simulation context ─────────
    # When called from the planner, semantic context is already pre-fetched and
    # stored in state to avoid N redundant API calls across parallel sub-steps.
    semantic_context = ""
    simulation_guidance = state.get("scenario_simulation_guidance", "")

    if state.get("planner_semantic_context"):
        semantic_context = state["planner_semantic_context"]
        print(f"\n{_DIM}  ♻  Reusing planner semantic context (skipping API call){_RESET}")
    else:
        try:
            semantic = SemanticService()
            context_data = semantic.get_all_context(state["user_query"])

            kpi_hits = len(context_data.get("kpi", []))
            qb_hits  = len(context_data.get("question_bank", []))
            sim_hits = len(context_data.get("simulation", []))
            total    = kpi_hits + qb_hits + sim_hits

            if total:
                semantic_context    = semantic.format_traversal_context(context_data)
                simulation_guidance = semantic.format_simulation_guidance(context_data)
                print(
                    f"\n{_GREEN}  🎯 Semantic context: "
                    f"{kpi_hits} KPI · {qb_hits} Q&A · {sim_hits} scenario result(s){_RESET}"
                )
            else:
                print(f"\n{_DIM}  ℹ  No semantic context retrieved (API may be unreachable).{_RESET}")
        except Exception as e:
            logger.warning("Semantic search failed (non-fatal): %s", e)

    # Escape literal { } in dynamic content to avoid str.format() KeyError
    safe_kg_schema = kg_schema.replace("{", "{{").replace("}", "}}")
    safe_semantic  = semantic_context.replace("{", "{{").replace("}", "}}")

    system_prompt = TRAVERSAL_SYSTEM.format(
        kg_schema=safe_kg_schema,
        semantic_context=safe_semantic,
    )

    max_steps = state.get("max_traversal_steps", DEFAULT_MAX_STEPS)

    # Create the ReAct agent with all available tools
    tools = get_all_tools()
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
    )

    print(f"\n{_DIM}  Query: {state['user_query']}{_RESET}")
    print(f"{_DIM}  Max steps: {max_steps}{_RESET}")

    # Invoke the agent
    start_time = time.perf_counter()
    try:
        result = agent.invoke(
            {"messages": [("human", state["user_query"])]},
            config={"recursion_limit": max_steps * 3 + 10},
        )

        elapsed = time.perf_counter() - start_time
        agent_messages = result.get("messages", [])

        # Extract + print all tool calls and reasoning
        tool_call_records, findings = _extract_and_print(agent_messages)
        steps_taken = len(tool_call_records)

        print(f"  {_DIM}Total time: {elapsed:.1f}s{_RESET}\n")

        logger.info(
            "Traversal agent completed: %d tool calls in %.1fs",
            steps_taken, elapsed,
        )

        return {
            "traversal_findings": findings,
            "traversal_tool_calls": tool_call_records,
            "traversal_steps_taken": steps_taken,
            "scenario_simulation_guidance": simulation_guidance,
            "current_phase": "response",
            "messages": [{
                "agent": "traversal",
                "content": (
                    f"Autonomous exploration complete: {steps_taken} tool calls, "
                    f"{elapsed:.1f}s elapsed"
                ),
            }],
        }

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        print(f"\n  {_RED}✗ Traversal failed after {elapsed:.1f}s: {e}{_RESET}\n")
        logger.error("Traversal agent failed: %s", e)
        return {
            "traversal_findings": f"Traversal failed: {e}",
            "traversal_tool_calls": [],
            "traversal_steps_taken": 0,
            "scenario_simulation_guidance": simulation_guidance,
            "current_phase": "response",
            "errors": [f"Traversal agent error: {e}"],
            "messages": [{
                "agent": "traversal",
                "content": f"Traversal failed after {elapsed:.1f}s: {e}",
            }],
        }


async def atraversal_node(state: SimulationState) -> dict[str, Any]:
    """
    Async version of traversal_node for concurrent execution from the planner.

    Uses agent.ainvoke() so multiple sub-traversals can truly overlap via
    asyncio.gather() in the planner, rather than serializing through threads.
    """
    warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

    llm = LLMProvider.get_llm("default")

    kg_schema = state.get("kg_schema", "Schema not available")
    # Planner always pre-fetches and injects semantic context — reuse it.
    semantic_context = state.get("planner_semantic_context", "")
    simulation_guidance = state.get("scenario_simulation_guidance", "")

    safe_kg_schema = kg_schema.replace("{", "{{").replace("}", "}}")
    safe_semantic  = semantic_context.replace("{", "{{").replace("}", "}}")

    system_prompt = TRAVERSAL_SYSTEM.format(
        kg_schema=safe_kg_schema,
        semantic_context=safe_semantic,
    )

    max_steps = state.get("max_traversal_steps", DEFAULT_MAX_STEPS)
    tools = get_all_tools()
    agent = create_react_agent(model=llm, tools=tools, prompt=system_prompt)

    query = state["user_query"]

    start_time = time.perf_counter()
    try:
        result = await agent.ainvoke(
            {"messages": [("human", query)]},
            config={"recursion_limit": max_steps * 3 + 10},
        )
        elapsed = time.perf_counter() - start_time
        agent_messages = result.get("messages", [])
        tool_call_records, findings = _extract_and_print(agent_messages)
        steps_taken = len(tool_call_records)

        logger.info(
            "Async traversal complete: %d tool calls in %.1fs | '%s'",
            steps_taken, elapsed, query[:60],
        )

        return {
            "traversal_findings": findings,
            "traversal_tool_calls": tool_call_records,
            "traversal_steps_taken": steps_taken,
            "scenario_simulation_guidance": simulation_guidance,
            "current_phase": "response",
            "messages": [{
                "agent": "traversal",
                "content": (
                    f"Exploration complete: {steps_taken} tool calls, {elapsed:.1f}s"
                ),
            }],
        }

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        logger.error("Async traversal failed after %.1fs: %s", elapsed, e)
        return {
            "traversal_findings": f"Traversal failed: {e}",
            "traversal_tool_calls": [],
            "traversal_steps_taken": 0,
            "scenario_simulation_guidance": simulation_guidance,
            "current_phase": "response",
            "errors": [f"Traversal agent error: {e}"],
            "messages": [{
                "agent": "traversal",
                "content": f"Traversal failed after {elapsed:.1f}s: {e}",
            }],
        }
