"""
Response Agent — Interprets traversal findings, performs calculations
via Python sandbox, and generates a PM-readable response.

Uses gpt-5-mini (reasoning model, medium effort) for structured, format-strict output.

Handles two upstream paths:
  • Direct traversal path: reads traversal_findings + traversal_tool_calls
  • Planner path: reads planner_steps + planner_step_results (N parallel traversals)
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage

from models.state import SimulationState
from services.llm_provider import LLMProvider
from tools.python_sandbox import execute_python
from prompts.response_prompt import RESPONSE_SYSTEM
from prompts.chart_prompt import CHART_SYSTEM
from prompts.algorithm_prompt import ALGORITHM_SYSTEM
from services.date_context import today_date_context
from agents.scenario_render import lean_scenario_result, render_scenario_findings


logger = logging.getLogger(__name__)


def _scenario_or_findings(result: dict) -> str:
    """Findings text for one planner step. For a deterministic scenario step the planner
    passes the FULL structured result through (`scenario_full_result`); render a LEAN view
    here — at the response boundary — so heavy per-site detail never enters the LLM prompt
    (the full detail is attached to the final payload separately). Non-scenario steps use
    the traversal agent's own findings string."""
    full = result.get("scenario_full_result")
    if full is not None:
        return render_scenario_findings(
            result.get("scenario_label", "scenario"),
            result.get("scenario_resolved", {}),
            lean_scenario_result(full),
        )
    return result.get("traversal_findings", "No findings.")


def _format_traversal_data(state: SimulationState) -> tuple[str, list]:
    """
    Format traversal findings for the response LLM.

    Sends findings and a compact tool call summary — NOT the full raw tool
    outputs, which bloat the context and slow down LLM processing.

    Returns (formatted_context_string, effective_tool_calls_list).
    """
    planner_steps = state.get("planner_steps", [])
    planner_results = state.get("planner_step_results", [])

    # ── Planner path: accumulate results from N parallel traversals ───────────
    if planner_steps and planner_results:
        lines = [f"## Planner Execution — {len(planner_steps)} Parallel Steps\n"]
        all_tool_calls: list = []

        for idx, (step, result) in enumerate(zip(planner_steps, planner_results), 1):
            findings = _scenario_or_findings(result)
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

        # Compact tool summary: just tool names + key numeric outputs (skip raw payloads)
        if all_tool_calls:
            lines.append(f"\n## Tool Call Summary ({len(all_tool_calls)} calls)\n")
            for i, tc in enumerate(all_tool_calls, 1):
                status_icon = "OK" if tc["status"] == "success" else "ERR"
                lines.append(f"- {tc['tool_name']} [{status_icon}]: {_compact_output(tc['tool_output'])}")

        return "\n".join(lines), all_tool_calls

    # ── Direct traversal path ─────────────────────────────────────────────────
    lines = ["## Traversal Agent Findings\n"]

    findings = state.get("traversal_findings", "")
    lines.append(findings if findings else "No findings were recorded by the traversal agent.")

    tool_calls = state.get("traversal_tool_calls", [])
    if tool_calls:
        lines.append(f"\n## Tool Call Summary ({len(tool_calls)} calls)\n")
        for i, tc in enumerate(tool_calls, 1):
            status_icon = "OK" if tc["status"] == "success" else "ERR"
            lines.append(f"- {tc['tool_name']} [{status_icon}]: {_compact_output(tc['tool_output'])}")

    return "\n".join(lines), tool_calls


def _compact_output(raw, max_len: int = 200) -> str:
    """Extract a compact summary from a tool output (dict or JSON string)."""
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
    if isinstance(parsed, dict):
        if "records" in parsed:
            return f"{parsed.get('count', len(parsed['records']))} records"
        if "error" in parsed:
            return f"Error: {str(parsed['error'])[:120]}"
        if "relevant_nodes" in parsed:
            return f"{len(parsed['relevant_nodes'])} nodes, {len(parsed.get('relevant_metrics', []))} metrics"
        if "paths" in parsed:
            return f"{len(parsed['paths'])} paths"
        # Generic: summarise list-valued keys by count (covers scenario results,
        # e.g. "cycle_baseline:2, predictions:31") instead of dumping the raw dict.
        list_bits = [f"{k}:{len(v)}" for k, v in parsed.items() if isinstance(v, list)]
        if list_bits:
            return ", ".join(list_bits)
        if parsed.get("status") == "success":
            return f"OK — {str(parsed.get('result', parsed.get('output', '')))[:150]}"
    text = str(raw)
    return text[:max_len] + "…" if len(text) > max_len else text


# "Current Status" section marker — tolerant to all the variants the response
# model emits in practice:
#   ## Current Status      (intended — heading prefix)
#   Current Status         (plain text — model sometimes drops the `##`)
#   **Current Status**     (bolded)
#   Current Status:        (trailing colon)
# Anchored to start-of-line so mid-sentence mentions don't false-match.
_CURRENT_STATUS_MARKER_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*)?Current Status(?:\*\*)?\s*:?\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _split_current_status(markdown: str) -> tuple[list[str], str]:
    """
    Parse the markdown's "Current Status" table into a flat list of
    "<Metric>: <Value>" strings AND return the markdown with that section
    removed so the data lives in exactly one place (the `current_status`
    sibling field, not duplicated in `final_response`).

    Returns ([], <unchanged markdown>) when the section is absent (TYPE 1 /
    greeting) or the table can't be parsed. The original markdown is preserved
    in those cases — we only strip when we have rows to put somewhere safe.

    Robust to common emission variants — the section title can be a `##`
    heading, plain text, bold, or end with a colon. The table that follows is
    found by walking lines after the marker; parsing stops at the first blank
    line *after* table rows start, or at the next heading / `---` separator
    if no table was found.

    Bold markers (`**`) and surrounding whitespace are stripped from each cell.
    """
    if not markdown:
        return [], markdown
    m = _CURRENT_STATUS_MARKER_RE.search(markdown)
    if not m:
        return [], markdown

    section_start = m.start()
    rows: list[str] = []
    seen_separator = False
    in_table = False
    # Track running char offset so we know exactly where the section ends.
    pos = m.end()
    for line in markdown[m.end():].splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            # Blank line ends the table once we've started parsing one;
            # before the table starts, blanks are tolerated.
            if in_table:
                break
            pos += len(line)
            continue
        if stripped.startswith("|"):
            in_table = True
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Separator row like "|---|---|"
            if cells and all(set(c) <= set("-: ") and "-" in c for c in cells):
                seen_separator = True
                pos += len(line)
                continue
            # Row before the separator is the header — skip it
            if not seen_separator:
                pos += len(line)
                continue
            if len(cells) < 2:
                pos += len(line)
                continue
            metric = re.sub(r"\*+", "", cells[0]).strip()
            value = re.sub(r"\*+", "", cells[1]).strip()
            if metric and value:
                rows.append(f"{metric}: {value}")
            pos += len(line)
            continue
        # Non-table, non-blank line — if we were parsing a table, it just ended.
        if in_table:
            break
        # Before the table starts, treat the next heading or "---" as the
        # boundary of the section. Anything else (e.g. a one-line intro under
        # the marker) is tolerated so the table can still be found.
        if stripped.startswith("---") or re.match(r"^#{1,6}\s+\S", stripped):
            break
        pos += len(line)

    # If we couldn't extract any rows, leave the markdown untouched —
    # we don't want to silently drop content the parser didn't understand.
    if not rows:
        return [], markdown

    section_end = pos
    trimmed = markdown[:section_start] + markdown[section_end:]
    # Collapse the 3+ consecutive newlines that removal can introduce.
    trimmed = re.sub(r"\n{3,}", "\n\n", trimmed).strip()
    return rows, trimmed


def _generate_chart(llm, user_query: str, data_context: str) -> dict[str, Any]:
    """
    Ask the LLM to produce a Highcharts-compatible chart spec from the
    traversal data and the already-generated response.

    Returns a dict with "charts" and "rationale" keys, or an empty-charts
    fallback on any error.
    """
    empty = {"charts": [], "rationale": "Chart generation skipped."}

    try:
        chart_user_msg = (
            f"## User Query\n{user_query}\n\n"
            f"## Collected Data\n{data_context}\n\n"
            "Based on the data above, produce the chart JSON."
        )
        chart_resp = llm.invoke([
            SystemMessage(content=CHART_SYSTEM),
            HumanMessage(content=chart_user_msg),
        ])

        raw = chart_resp.content.strip()

        # Strip markdown fences — GPT-4o often wraps JSON in ```json ... ```
        # or adds prose before/after the block
        if "```" in raw:
            # Extract content between first ``` and last ```
            parts = raw.split("```")
            # parts[1] is the content inside the first fence pair
            if len(parts) >= 3:
                fenced = parts[1]
                # Remove optional language tag (e.g., "json\n")
                if fenced.startswith(("json", "JSON")):
                    fenced = fenced.split("\n", 1)[1] if "\n" in fenced else fenced[4:]
                raw = fenced.strip()

        # Last resort: find the outermost { ... } if there's still junk around it
        if not raw.startswith("{"):
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]

        graph_data = json.loads(raw)

        # Basic validation
        if not isinstance(graph_data, dict) or "charts" not in graph_data:
            logger.warning("Chart LLM returned unexpected structure; skipping.")
            return empty

        logger.info("Chart generated: %d chart(s)", len(graph_data.get("charts", [])))
        return graph_data

    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Chart JSON parsing failed: %s — raw response: %.500s", exc, raw)
        return empty
    except Exception as exc:
        logger.error("Chart generation failed: %s", exc)
        return empty


def _generate_algorithm(llm, user_query: str, data_context: str) -> str:
    """
    Ask a fast-tier LLM to turn the agent's tool trace into a numbered,
    plain-English algorithm narrative. Called in parallel with the main
    response LLM so it costs no extra wall-clock time.

    Returns an empty string on any failure — the main response is never blocked
    by problems here.
    """
    try:
        resp = llm.invoke([
            SystemMessage(content=ALGORITHM_SYSTEM),
            HumanMessage(content=(
                f"## User Query\n{user_query}\n\n"
                f"## Tool Trace\n{data_context}\n\n"
                "Write the numbered algorithm now."
            )),
        ])
        return (resp.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Algorithm generation failed: %s", exc)
        return ""


def response_node(state: SimulationState) -> dict[str, Any]:
    """
    LangGraph node: Response Agent.

    Reads: refined_query (or user_query), traversal/planner data, errors
    Writes: final_response, calculations, data_summary, current_phase, messages
    """
    # gpt-5-mini is a reasoning model: max_tokens covers BOTH internal reasoning
    # and visible output. With reasoning_effort="medium" the model can easily
    # spend 3–5k tokens reasoning before emitting any output — so the default
    # 4096 cap leaves nothing for the response itself and returns empty content.
    # 16000 is comfortable for a long structured markdown answer plus reasoning.
    llm = LLMProvider.get_llm("gpt-5-mini", reasoning_effort="medium", max_tokens=16000)

    # Prefer the query refiner's cleaned-up version
    user_query = state.get("user_query") or state["refined_query"]

    data_context, effective_tool_calls = _format_traversal_data(state)
    errors = state.get("errors", [])

    user_message_parts = [
        f"## Original User Query\n{user_query}",
    ]

    # Include planner's decomposition strategy so the response agent
    # understands WHY data was collected the way it was
    planner_steps = state.get("planner_steps", [])
    planning_rationale = state.get("planning_rationale", "")
    if planner_steps:
        plan_lines = ["## Planner Strategy"]
        if planning_rationale:
            plan_lines.append(f"**Rationale**: {planning_rationale}\n")
        plan_lines.append("**Decomposed into these sub-queries:**")
        for i, step in enumerate(planner_steps, 1):
            display = step.split(": ", 1)[1] if ": " in step else step
            plan_lines.append(f"  {i}. {display}")
        user_message_parts.append("\n".join(plan_lines))

    user_message_parts.append(f"\n{data_context}")

    if errors:
        user_message_parts.append(
            "\n## Errors Encountered\n" +
            "\n".join(f"- {e}" for e in errors)
        )

    simulation_guidance = state.get("scenario_simulation_guidance", "").strip()
    if simulation_guidance:
        user_message_parts.append(f"\n{simulation_guidance}")

    # Tell the response agent which route was taken so it picks the right format
    routing = state.get("routing_decision", "traversal")
    if routing == "traversal" and not planner_steps:
        query_type_hint = (
            "This is a simple data fetch query (direct traversal, no planner). "
            "Use TYPE 1 format: one-line answer + data table. Nothing else."
        )
    else:
        query_type_hint = (
            "This is a simulation query. Identify whether it is scheduling, what-if, "
            "or general analysis and use the matching TYPE format from your instructions."
        )

    user_message_parts.append(
        f"\n## Query Type Hint\n{query_type_hint}"
        "\n\n## Instructions"
        "\nAnalyze the collected data above and generate the response in the exact format "
        "specified by your system prompt for this query type."
        "\n\nIMPORTANT:"
        "\n- Use the Simulation Guidance (if provided) as a methodology reference — "
        "adapt it to the data that was actually retrieved."
        "\n- Show ALL fetched data in consolidated tables with human-readable column names."
        "\n- NEVER use database column names — always use full readable labels."
        "\n- DEDUPLICATE: Multiple sub-queries may return overlapping data. "
        "Present each data point ONCE. Merge related tables."
        "\n- Every insight must pass the 'so what' test — no filler, no generic statements."
        "\n- If data is missing or queries failed, acknowledge it in one line and move on."
    )

    user_message = "\n".join(user_message_parts)
    print(f"DATE HANDLER RETURNS WITH THE RESPONSES AS FOLLOWS: {today_date_context()}")
    system_prompt = RESPONSE_SYSTEM.format(today_date=today_date_context())

    # Fire the algorithm-narrative LLM in a background thread so it runs in
    # parallel with the main response call. Total latency stays at
    # max(main, algorithm) instead of sum. Failures here never block or
    # degrade the main response — _generate_algorithm returns "" on error.
    algorithm_result: dict[str, str] = {"value": ""}

    def _algorithm_worker() -> None:
        fast_llm = LLMProvider.get_llm("gpt-5.4-mini", reasoning_effort="low", max_tokens=1024)
        algorithm_result["value"] = _generate_algorithm(fast_llm, user_query, data_context)

    algorithm_thread = threading.Thread(target=_algorithm_worker, daemon=True)
    algorithm_thread.start()

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ])

    final_response = response.content

    # Wait for the algorithm narrative — it usually finishes well before the
    # heavy response LLM. Cap at 60s in case the fast tier stalls; on timeout
    # we simply ship with an empty algorithm string.
    algorithm_thread.join(timeout=60)
    execution_algorithm = algorithm_result["value"]

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

    # ── Chart generation (fast tier — structured JSON, no deep reasoning) ──
    # Same reasoning-budget caveat as the main response LLM — bump max_tokens
    # so reasoning + JSON output both fit comfortably.
    chart_llm = LLMProvider.get_llm("gpt-5-mini", temperature=0.0, max_tokens=8000)
    graph_data = _generate_chart(chart_llm, user_query, data_context)

    logger.info("Response agent generated final output")

    # Split the Current Status table out of the markdown so the same data
    # doesn't appear twice (once in current_status, once in final_response).
    current_status, final_response = _split_current_status(final_response)

    return {
        "final_response": final_response,
        "current_status": current_status,
        "execution_algorithm": execution_algorithm,
        "calculations": calculations_output,
        "data_summary": data_summary,
        "graph_data": graph_data,
        "current_phase": "complete",
        "messages": [{"agent": "response", "content": "Generated final response"}],
    }
