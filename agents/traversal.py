"""
Traversal Agent — Autonomous ReAct agent that explores the Neo4j
Knowledge Graph using tools to gather data needed to answer the
user's query.
"""
from __future__ import annotations

import json
import os
import time
import logging
from datetime import date, datetime
import warnings
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
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


# ─── Debug logger for traversal token analysis ───
_DEBUG_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
_DEBUG_LOG_FILE = os.path.join(_DEBUG_LOG_DIR, "traversal_debug.log")
_CONTEXT_DUMP_FILE = os.path.join(_DEBUG_LOG_DIR, "context_exceeded_dump.log")


class _LLMInputCapture(BaseCallbackHandler):
    """Callback that captures every LLM input so we can dump it on context overflow."""

    def __init__(self):
        self.last_prompts: list = []
        self.call_count: int = 0

    def on_llm_start(self, serialized: dict, prompts: list[str], **kwargs) -> None:
        """Called for non-chat LLMs (unlikely but covered)."""
        self.call_count += 1
        self.last_prompts = prompts

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs) -> None:
        """Called before each chat LLM call — captures the full message list."""
        self.call_count += 1
        self.last_messages = messages


def _dump_context_exceeded(
    query: str,
    error: Exception,
    elapsed: float,
    capture: _LLMInputCapture,
    system_prompt: str,
    tools: list,
) -> None:
    """Dump the full LLM input that caused context_length_exceeded to a separate log file."""
    os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)

    lines = [
        f"\n{'!' * 100}",
        f"  CONTEXT LENGTH EXCEEDED — FULL PAYLOAD DUMP",
        f"  Timestamp : {datetime.now().isoformat()}",
        f"  Sub-query : {query}",
        f"  Elapsed   : {elapsed:.1f}s",
        f"  LLM calls before failure: {capture.call_count}",
        f"  Error     : {str(error)[:300]}",
        f"{'!' * 100}",
        f"",
        f"{'=' * 100}",
        f"  SECTION 1: SYSTEM PROMPT ({len(system_prompt):,} chars ≈ {len(system_prompt) // 4:,} tokens)",
        f"{'=' * 100}",
        system_prompt,
        f"",
        f"{'=' * 100}",
        f"  SECTION 2: TOOL SCHEMAS ({len(tools)} tools)",
        f"{'=' * 100}",
    ]

    tool_total_chars = 0
    for t in tools:
        schema_str = json.dumps(t.args_schema.model_json_schema(), indent=2, default=str)
        tool_chars = len(t.description) + len(schema_str)
        tool_total_chars += tool_chars
        lines.append(f"\n  --- {t.name} ({tool_chars:,} chars ≈ {tool_chars // 4:,} tokens) ---")
        lines.append(f"  Description: {t.description[:200]}")
        lines.append(f"  Schema: {schema_str}")
    lines.append(f"\n  TOOL SCHEMAS TOTAL: {tool_total_chars:,} chars ≈ {tool_total_chars // 4:,} tokens")

    # Dump the last messages that were sent to the LLM (the ones that caused the overflow)
    last_msgs = getattr(capture, "last_messages", None)
    if last_msgs:
        lines.extend([
            f"",
            f"{'=' * 100}",
            f"  SECTION 3: LAST LLM INPUT MESSAGES ({len(last_msgs)} message groups)",
            f"{'=' * 100}",
        ])

        total_msg_chars = 0
        # last_messages is list[list[BaseMessage]] — outer list is batch, usually len=1
        for batch_idx, batch in enumerate(last_msgs):
            lines.append(f"\n  --- Batch {batch_idx} ({len(batch)} messages) ---")
            for i, msg in enumerate(batch):
                msg_type = msg.type if hasattr(msg, "type") else type(msg).__name__
                content = getattr(msg, "content", "") or ""
                content_chars = len(content)

                # Tool calls embedded in AI messages
                tc_chars = 0
                tc_info = ""
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    tc_chars = sum(len(json.dumps(tc.get("args", {}), default=str)) for tc in msg.tool_calls)
                    tc_names = [tc["name"] for tc in msg.tool_calls]
                    tc_info = f" | tool_calls: {', '.join(tc_names)} ({tc_chars:,} chars)"

                total_chars = content_chars + tc_chars
                total_msg_chars += total_chars

                lines.append(
                    f"\n  [{i}] {msg_type} — {total_chars:,} chars ≈ {total_chars // 4:,} tokens{tc_info}"
                )

                # Dump full content for inspection
                if content_chars > 2000:
                    lines.append(f"  CONTENT (first 1000 chars):\n{content[:1000]}")
                    lines.append(f"  ... ({content_chars - 2000:,} chars omitted) ...")
                    lines.append(f"  CONTENT (last 1000 chars):\n{content[-1000:]}")
                else:
                    lines.append(f"  CONTENT:\n{content}")

        lines.extend([
            f"",
            f"  {'─' * 80}",
            f"  MESSAGES TOTAL: {total_msg_chars:,} chars ≈ {total_msg_chars // 4:,} tokens",
            f"  GRAND TOTAL (system + tools + messages): "
            f"{len(system_prompt) + tool_total_chars + total_msg_chars:,} chars "
            f"≈ {(len(system_prompt) + tool_total_chars + total_msg_chars) // 4:,} tokens",
            f"  {'─' * 80}",
        ])
    else:
        lines.append(f"\n  (No captured messages — failure may have occurred before first LLM call)")

    lines.append(f"\n{'!' * 100}\n")

    with open(_CONTEXT_DUMP_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    logger.warning(
        "Context exceeded dump written to %s (%d chars)",
        _CONTEXT_DUMP_FILE, sum(len(l) for l in lines),
    )


# Rough token estimate: 1 token ≈ 4 chars for English text
def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _log_traversal_debug(
    query: str,
    system_prompt: str,
    kg_schema_chars: int,
    semantic_context_chars: int,
    tools: list,
    stage: str,
) -> None:
    """Log detailed char/token breakdown before the agent is invoked."""
    os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)

    tool_schemas_str = json.dumps(
        [{"name": t.name, "description": t.description[:100]} for t in tools],
        default=str,
    )
    tool_schemas_chars = sum(
        len(json.dumps(t.args_schema.model_json_schema(), default=str)) + len(t.description)
        for t in tools
    )

    human_msg_chars = len(query)
    total_input_chars = len(system_prompt) + human_msg_chars + tool_schemas_chars

    lines = [
        f"\n{'=' * 80}",
        f"  TRAVERSAL DEBUG — {stage}",
        f"  Timestamp: {datetime.now().isoformat()}",
        f"  Sub-query: {query[:120]}",
        f"{'=' * 80}",
        f"",
        f"  Component Breakdown (chars → est. tokens):",
        f"  {'─' * 60}",
        f"  System prompt (total)    : {len(system_prompt):>8,} chars  ≈ {_estimate_tokens(system_prompt):>7,} tokens",
        f"    ├─ KG schema           : {kg_schema_chars:>8,} chars  ≈ {kg_schema_chars // 4:>7,} tokens",
        f"    ├─ Semantic context     : {semantic_context_chars:>8,} chars  ≈ {semantic_context_chars // 4:>7,} tokens",
        f"    └─ Prompt template      : {len(system_prompt) - kg_schema_chars - semantic_context_chars:>8,} chars  ≈ {(len(system_prompt) - kg_schema_chars - semantic_context_chars) // 4:>7,} tokens",
        f"  Human message            : {human_msg_chars:>8,} chars  ≈ {_estimate_tokens(query):>7,} tokens",
        f"  Tool schemas ({len(tools)} tools)  : {tool_schemas_chars:>8,} chars  ≈ {tool_schemas_chars // 4:>7,} tokens",
        f"  {'─' * 60}",
        f"  TOTAL INITIAL INPUT      : {total_input_chars:>8,} chars  ≈ {total_input_chars // 4:>7,} tokens",
        f"  {'─' * 60}",
        f"  Headroom to 128K tokens  : {128000 - total_input_chars // 4:>7,} tokens remaining",
        f"",
    ]

    with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _log_message_breakdown(query: str, messages: list, elapsed: float) -> None:
    """Log per-message token breakdown after agent completes."""
    os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)

    lines = [
        f"\n{'─' * 80}",
        f"  POST-EXECUTION MESSAGE BREAKDOWN",
        f"  Sub-query: {query[:120]}",
        f"  Elapsed: {elapsed:.1f}s | Total messages: {len(messages)}",
        f"{'─' * 80}",
        f"",
        f"  {'#':<4} {'Type':<12} {'Role':<10} {'Chars':>8}  {'≈Tokens':>8}  Details",
        f"  {'─' * 70}",
    ]

    cumulative_chars = 0
    for i, msg in enumerate(messages):
        msg_type = msg.type if hasattr(msg, "type") else type(msg).__name__
        content = getattr(msg, "content", "") or ""
        content_chars = len(content)

        # For tool calls, also count the tool call args
        tool_info = ""
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tc_chars = sum(len(json.dumps(tc.get("args", {}), default=str)) for tc in msg.tool_calls)
            content_chars += tc_chars
            tool_names = [tc["name"] for tc in msg.tool_calls]
            tool_info = f"calls: {', '.join(tool_names)}"

        # For tool messages, show tool name
        if msg_type == "tool":
            tool_name = getattr(msg, "name", "?")
            tool_info = f"tool: {tool_name} | output: {content[:80]}..."

        cumulative_chars += content_chars

        lines.append(
            f"  {i:<4} {msg_type:<12} {'':>10} {content_chars:>8,}  ≈{content_chars // 4:>7,}  {tool_info}"
        )

    lines.extend([
        f"  {'─' * 70}",
        f"  CUMULATIVE MESSAGE CHARS : {cumulative_chars:>8,}  ≈{cumulative_chars // 4:>7,} tokens",
        f"  (This excludes system prompt + tool schemas which are added on each LLM call)",
        f"",
    ])

    with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _log_failure_debug(query: str, error: Exception, elapsed: float) -> None:
    """Log traversal failure with error details for debugging."""
    os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)

    error_str = str(error)

    # Try to extract token count from the error message
    token_info = ""
    if "tokens" in error_str.lower():
        import re
        match = re.search(r"(\d[\d,]+)\s*tokens?\s*\(", error_str)
        if match:
            token_info = f"  Tokens reported by API: {match.group(1)}"

    lines = [
        f"\n{'!' * 80}",
        f"  TRAVERSAL FAILED — CONTEXT LENGTH EXCEEDED",
        f"  Timestamp: {datetime.now().isoformat()}",
        f"  Sub-query: {query[:120]}",
        f"  Elapsed: {elapsed:.1f}s",
        f"{'!' * 80}",
        f"  Error: {error_str[:500]}",
        token_info,
        f"{'!' * 80}",
        f"",
    ]

    with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _print_divider(char: str = "─", width: int = 70):
    print(f"{_DIM}{char * width}{_RESET}", flush=True)


def _print_tool_call(step_num: int, tool_name: str, tool_input: dict):
    """Print a tool call in a readable format."""
    _print_divider()
    print(f"{_BOLD}{_CYAN}  🔧 Step {step_num}: {tool_name}{_RESET}", flush=True)

    # Format input — print full code for sandbox tools, truncate others
    for key, val in tool_input.items():
        val_str = str(val)
        if key == "code" and tool_name in ("run_sql_python", "run_python"):
            # Print full SQL/Python code — never truncate
            print(f"     {_DIM}{key}:{_RESET}", flush=True)
            for line in val_str.splitlines():
                print(f"       {_DIM}{line}{_RESET}", flush=True)
        else:
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            print(f"     {_DIM}{key}:{_RESET} {val_str}", flush=True)


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
    print(f"     {color_out}{icon} Result:{_RESET} {display}", flush=True)


def _print_agent_thinking(content: str):
    """Print the agent's reasoning text."""
    if not content.strip():
        return
    # Truncate very long reasoning
    text = content.strip()
    if len(text) > 400:
        text = text[:400] + "..."
    print(f"  {_YELLOW}💭 Agent:{_RESET} {text}", flush=True)


def _extract_and_print(messages: list) -> tuple[list[ToolCallRecord], str]:
    """
    Walk the agent message history, print each step live-style,
    and return (tool_call_records, findings).
    """
    records: list[ToolCallRecord] = []
    step_num = 0
    findings = "No findings extracted."

    print(f"\n{_BOLD}{'═' * 70}", flush=True)
    print(f"  🔍 TRAVERSAL AGENT — Exploring Knowledge Graph", flush=True)
    print(f"{'═' * 70}{_RESET}\n", flush=True)

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
    print(f"  {_BOLD}✅ Traversal complete: {step_num} tool calls{_RESET}", flush=True)
    _print_divider("═")
    print(flush=True)

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
        print(f"\n{_DIM}  ♻  Reusing planner semantic context (skipping API call){_RESET}", flush=True)
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
                    f"{kpi_hits} KPI · {qb_hits} Q&A · {sim_hits} scenario result(s){_RESET}",
                    flush=True,
                )
            else:
                print(f"\n{_DIM}  ℹ  No semantic context retrieved (API may be unreachable).{_RESET}", flush=True)
        except Exception as e:
            logger.warning("Semantic search failed (non-fatal): %s", e)

    # Escape literal { } in dynamic content to avoid str.format() KeyError
    safe_kg_schema = kg_schema.replace("{", "{{").replace("}", "}}")
    safe_semantic  = semantic_context.replace("{", "{{").replace("}", "}}")

    system_prompt = TRAVERSAL_SYSTEM.format(
        today_date=date.today(),
        kg_schema=safe_kg_schema,
        semantic_context=safe_semantic,
    )

    max_steps = state.get("max_traversal_steps", DEFAULT_MAX_STEPS)

    # Create the ReAct agent with all available tools
    tools = get_all_tools()

    print(f"  {_DIM}Traversal: {len(tools)} tools | max {max_steps} steps{_RESET}", flush=True)

    # ── Debug: log token breakdown before agent starts ──────────────────
    _log_traversal_debug(
        query=state["user_query"],
        system_prompt=system_prompt,
        kg_schema_chars=len(kg_schema),
        semantic_context_chars=len(semantic_context),
        tools=tools,
        stage="BEFORE_AGENT_INVOKE (sync)",
    )

    # Callback to capture last LLM input for context-exceeded debugging
    llm_capture = _LLMInputCapture()
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
    )

    # Invoke the agent
    start_time = time.perf_counter()
    try:
        result = agent.invoke(
            {"messages": [("human", state["user_query"])]},
            config={
                "recursion_limit": max_steps * 3 + 10,
                "callbacks": [llm_capture],
            },
        )

        elapsed = time.perf_counter() - start_time
        agent_messages = result.get("messages", [])

        # ── Debug: log per-message token breakdown after agent completes ──
        _log_message_breakdown(query=state["user_query"], messages=agent_messages, elapsed=elapsed)

        # Extract + print all tool calls and reasoning
        tool_call_records, findings = _extract_and_print(agent_messages)
        steps_taken = len(tool_call_records)

        print(f"  {_DIM}Total time: {elapsed:.1f}s{_RESET}\n", flush=True)

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
        _log_failure_debug(query=state["user_query"], error=e, elapsed=elapsed)

        # If context length exceeded, dump the full payload to a separate file
        error_str = str(e).lower()
        if "context_length" in error_str or "128000" in error_str or "max limit" in error_str:
            _dump_context_exceeded(
                query=state["user_query"],
                error=e,
                elapsed=elapsed,
                capture=llm_capture,
                system_prompt=system_prompt,
                tools=tools,
            )

        print(f"\n  {_RED}✗ Traversal failed after {elapsed:.1f}s: {e}{_RESET}\n", flush=True)
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
        today_date=date.today(),
        kg_schema=safe_kg_schema,
        semantic_context=safe_semantic,
    )

    max_steps = state.get("max_traversal_steps", DEFAULT_MAX_STEPS)
    tools = get_all_tools()

    query = state["user_query"]

    # ── Debug: log token breakdown before agent starts ──────────────────
    _log_traversal_debug(
        query=query,
        system_prompt=system_prompt,
        kg_schema_chars=len(kg_schema),
        semantic_context_chars=len(semantic_context),
        tools=tools,
        stage="BEFORE_AGENT_INVOKE",
    )

    # Callback to capture last LLM input for context-exceeded debugging
    llm_capture = _LLMInputCapture()
    agent = create_react_agent(model=llm, tools=tools, prompt=system_prompt)

    start_time = time.perf_counter()
    try:
        result = await agent.ainvoke(
            {"messages": [("human", query)]},
            config={
                "recursion_limit": max_steps * 3 + 10,
                "callbacks": [llm_capture],
            },
        )
        elapsed = time.perf_counter() - start_time
        agent_messages = result.get("messages", [])

        # ── Debug: log per-message token breakdown after agent completes ──
        _log_message_breakdown(query=query, messages=agent_messages, elapsed=elapsed)

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
        # ── Debug: log failure with error details ──
        _log_failure_debug(query=query, error=e, elapsed=elapsed)

        # If context length exceeded, dump the full payload to a separate file
        error_str = str(e).lower()
        if "context_length" in error_str or "128000" in error_str or "max limit" in error_str:
            _dump_context_exceeded(
                query=query,
                error=e,
                elapsed=elapsed,
                capture=llm_capture,
                system_prompt=system_prompt,
                tools=tools,
            )

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
