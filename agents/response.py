"""
Response Agent — Interprets traversal findings, performs calculations
via Python sandbox, and generates a PM-readable response.

Handles two upstream paths:
  • Direct traversal path: reads traversal_findings + traversal_tool_calls
  • Planner path: reads planner_steps + planner_step_results (N parallel traversals)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.settings import config
from models.state import SimulationState
from tools.python_sandbox import execute_python
from prompts.response_prompt import RESPONSE_SYSTEM

logger = logging.getLogger(__name__)


def _format_traversal_data(state: SimulationState) -> tuple[str, list]:
    """
    Format traversal findings and tool call log for the response LLM.

    Handles both:
    - Direct traversal (single run): traversal_findings + traversal_tool_calls
    - Planner path (parallel runs): planner_steps + planner_step_results

    Returns (formatted_context_string, effective_tool_calls_list).
    """
    planner_steps = state.get("planner_steps", [])
    planner_results = state.get("planner_step_results", [])

    # ── Planner path: accumulate results from N parallel traversals ───────────
    if planner_steps and planner_results:
        lines = [f"## Planner Execution — {len(planner_steps)} Parallel Steps\n"]
        all_tool_calls: list = []

        for idx, (step, result) in enumerate(zip(planner_steps, planner_results), 1):
            findings = result.get("traversal_findings", "No findings.")
            tool_calls = result.get("traversal_tool_calls", [])
            steps_taken = result.get("traversal_steps_taken", 0)
            step_errors = result.get("errors", [])

            lines.append(f"### Step {idx}: {step}")
            lines.append(f"*Tool calls: {steps_taken}*\n")
            lines.append(findings)

            if step_errors:
                lines.append("\n*Errors in this step:*")
                for err in step_errors:
                    lines.append(f"- {err}")
            lines.append("")
            all_tool_calls.extend(tool_calls)

        if all_tool_calls:
            lines.append(f"\n## Consolidated Tool Call Log ({len(all_tool_calls)} calls)\n")
            for i, tc in enumerate(all_tool_calls, 1):
                status_icon = "OK" if tc["status"] == "success" else "ERROR"
                lines.append(f"### Call {i}: {tc['tool_name']} [{status_icon}]")
                input_str = json.dumps(tc["tool_input"], default=str)
                if len(input_str) > 300:
                    input_str = input_str[:300] + "..."
                lines.append(f"**Input**: {input_str}")
                output_str = str(tc["tool_output"])
                if len(output_str) > 1500:
                    output_str = output_str[:1500] + "\n... (truncated)"
                lines.append(f"**Output**: {output_str}")
                lines.append("")

        return "\n".join(lines), all_tool_calls

    # ── Direct traversal path ─────────────────────────────────────────────────
    lines = ["## Traversal Agent Findings\n"]

    findings = state.get("traversal_findings", "")
    lines.append(findings if findings else "No findings were recorded by the traversal agent.")

    tool_calls = state.get("traversal_tool_calls", [])
    if tool_calls:
        lines.append(f"\n## Tool Call Log ({len(tool_calls)} calls)\n")
        for i, tc in enumerate(tool_calls, 1):
            status_icon = "OK" if tc["status"] == "success" else "ERROR"
            lines.append(f"### Call {i}: {tc['tool_name']} [{status_icon}]")
            input_str = json.dumps(tc["tool_input"], default=str)
            if len(input_str) > 300:
                input_str = input_str[:300] + "..."
            lines.append(f"**Input**: {input_str}")
            output_str = str(tc["tool_output"])
            if len(output_str) > 1500:
                output_str = output_str[:1500] + "\n... (truncated)"
            lines.append(f"**Output**: {output_str}")
            lines.append("")

    return "\n".join(lines), tool_calls


def response_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Response Agent.

    Reads: refined_query (or user_query), traversal/planner data, errors
    Writes: final_response, calculations, data_summary, current_phase, messages
    """
    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=0.1,
        max_tokens=config.llm.max_tokens,
    )

    # Prefer the query refiner's cleaned-up version
    user_query = state.get("refined_query") or state["user_query"]

    data_context, effective_tool_calls = _format_traversal_data(state)
    errors = state.get("errors", [])

    user_message_parts = [
        f"## Original User Query\n{user_query}",
        f"\n{data_context}",
    ]

    if errors:
        user_message_parts.append(
            "\n## Errors Encountered\n" +
            "\n".join(f"- {e}" for e in errors)
        )

    simulation_guidance = state.get("scenario_simulation_guidance", "").strip()
    if simulation_guidance:
        user_message_parts.append(f"\n{simulation_guidance}")

    user_message_parts.append(
        "\n## Instructions"
        "\nAnalyze the collected data above and generate a comprehensive, "
        "PM-readable response. Use the Simulation Guidance above (if provided) "
        "as a reference for how to structure your calculations and output only for used approaches to calculate data— "
        "adapt it to what was actually retrieved. Use Python sandbox for any "
        "calculations — write the code and I'll execute it. Include specific "
        "numbers from the data. If data is missing or queries failed, acknowledge "
        "it explicitly."
    )

    user_message = "\n".join(user_message_parts)

    response = llm.invoke([
        SystemMessage(content=RESPONSE_SYSTEM),
        HumanMessage(content=user_message),
    ])

    final_response = response.content

    # Execute any Python calculation blocks embedded in the response
    calculations_output = ""
    if "```python" in final_response:
        code_blocks = final_response.split("```python")
        for block in code_blocks[1:]:
            code = block.split("```")[0].strip()
            if not code:
                continue
            exec_context = {}
            for i, tc in enumerate(effective_tool_calls):
                if tc["status"] == "success" and tc["tool_output"]:
                    try:
                        parsed = json.loads(tc["tool_output"])
                        exec_context[f"call_{i}_{tc['tool_name']}"] = parsed
                    except (json.JSONDecodeError, TypeError):
                        exec_context[f"call_{i}_{tc['tool_name']}"] = tc["tool_output"]

            calc_result = execute_python(code, exec_context)
            if calc_result["status"] == "success":
                calculations_output += (
                    f"Calculation:\n{code}\n"
                    f"Output: {calc_result.get('output', '')}\n"
                    f"Result: {calc_result.get('result')}\n\n"
                )

    # Build data summary from all successful tool calls
    data_summary: dict[str, Any] = {}
    for i, tc in enumerate(effective_tool_calls):
        if tc["status"] == "success" and tc["tool_output"]:
            try:
                data_summary[f"call_{i}_{tc['tool_name']}"] = json.loads(tc["tool_output"])
            except (json.JSONDecodeError, TypeError):
                data_summary[f"call_{i}_{tc['tool_name']}"] = tc["tool_output"]

    logger.info("Response agent generated final output")

    return {
        "final_response": final_response,
        "calculations": calculations_output,
        "data_summary": data_summary,
        "current_phase": "complete",
        "messages": [{"agent": "response", "content": "Generated final response"}],
    }
