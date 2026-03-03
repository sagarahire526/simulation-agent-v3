"""
Response Agent — Interprets traversal findings, performs calculations
via Python sandbox, and generates a PM-readable response.
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


def _format_traversal_data(state: SimulationState) -> str:
    """Format traversal agent findings and tool call log for the response LLM."""
    lines = ["## Traversal Agent Findings\n"]

    # Main findings summary from the agent's final message
    findings = state.get("traversal_findings", "")
    if findings:
        lines.append(findings)
    else:
        lines.append("No findings were recorded by the traversal agent.")

    # Tool call log (abbreviated for context window efficiency)
    tool_calls = state.get("traversal_tool_calls", [])
    if tool_calls:
        lines.append(f"\n## Tool Call Log ({len(tool_calls)} calls)\n")
        for i, tc in enumerate(tool_calls, 1):
            status_icon = "OK" if tc["status"] == "success" else "ERROR"
            lines.append(f"### Call {i}: {tc['tool_name']} [{status_icon}]")
            # Show input
            input_str = json.dumps(tc["tool_input"], default=str)
            if len(input_str) > 300:
                input_str = input_str[:300] + "..."
            lines.append(f"**Input**: {input_str}")
            # Show output (truncated)
            output_str = str(tc["tool_output"])
            if len(output_str) > 1500:
                output_str = output_str[:1500] + "\n... (truncated)"
            lines.append(f"**Output**: {output_str}")
            lines.append("")

    return "\n".join(lines)


def response_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Response Agent.

    Reads: user_query, traversal_findings, traversal_tool_calls, errors
    Writes: final_response, calculations, data_summary, current_phase, messages
    """
    llm = ChatOpenAI(
        model=config.llm.model,
        temperature=0.1,  # Slight creativity for presentation
        max_tokens=config.llm.max_tokens,
    )

    # Build context
    data_context = _format_traversal_data(state)
    errors = state.get("errors", [])

    user_message_parts = [
        f"## Original User Query\n{state['user_query']}",
        f"\n{data_context}",
    ]

    if errors:
        user_message_parts.append(
            "\n## Errors Encountered\n" +
            "\n".join(f"- {e}" for e in errors)
        )

    # Inject simulation guidance from semantic match (if available)
    simulation_guidance = state.get("scenario_simulation_guidance", "").strip()
    if simulation_guidance:
        user_message_parts.append(
            f"\n{simulation_guidance}"
        )

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

    # Call LLM
    response = llm.invoke([
        SystemMessage(content=RESPONSE_SYSTEM),
        HumanMessage(content=user_message),
    ])

    final_response = response.content

    # Try to extract any Python calculation blocks and execute them
    calculations_output = ""
    if "```python" in final_response:
        code_blocks = final_response.split("```python")
        for block in code_blocks[1:]:
            code = block.split("```")[0].strip()
            if code:
                # Build context from tool call outputs
                exec_context = {}
                for i, tc in enumerate(state.get("traversal_tool_calls", [])):
                    if tc["status"] == "success" and tc["tool_output"]:
                        try:
                            parsed = json.loads(tc["tool_output"])
                            exec_context[f"call_{i}_{tc['tool_name']}"] = parsed
                        except (json.JSONDecodeError, TypeError):
                            exec_context[f"call_{i}_{tc['tool_name']}"] = tc["tool_output"]

                calc_result = execute_python(code, exec_context)
                if calc_result["status"] == "success":
                    output = calc_result.get("output", "")
                    result_val = calc_result.get("result")
                    calculations_output += (
                        f"Calculation:\n{code}\n"
                        f"Output: {output}\n"
                        f"Result: {result_val}\n\n"
                    )

    # Build data summary from successful tool calls
    data_summary: dict[str, Any] = {}
    for i, tc in enumerate(state.get("traversal_tool_calls", [])):
        if tc["status"] == "success" and tc["tool_output"]:
            try:
                parsed = json.loads(tc["tool_output"])
                data_summary[f"call_{i}_{tc['tool_name']}"] = parsed
            except (json.JSONDecodeError, TypeError):
                data_summary[f"call_{i}_{tc['tool_name']}"] = tc["tool_output"]

    logger.info("Response agent generated final output")

    return {
        "final_response": final_response,
        "calculations": calculations_output,
        "data_summary": data_summary,
        "current_phase": "complete",
        "messages": [{
            "agent": "response",
            "content": "Generated final response",
        }],
    }
