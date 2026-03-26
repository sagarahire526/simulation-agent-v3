"""
Deterministic KPI Executor — stitches collected kpi_python_function code
blocks into a single PythonSandbox call without LLM involvement.

The traversal agent calls get_kpi to collect metadata, then passes
the function code + filters here for batch execution. This avoids:
  - Backslash / line-continuation errors (no LLM writing code)
  - Data truncation (one combined call, one result)
  - Skipped run_sql_python calls (deterministic, always executes)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.tools import tool

from tools.python_sandbox import PythonSandbox

logger = logging.getLogger(__name__)


def _extract_function_name(code: str) -> str | None:
    """Extract the function name from a 'def func_name(...)' string."""
    match = re.search(r"def\s+(\w+)\s*\(", code)
    return match.group(1) if match else None


def _build_combined_code(kpi_entries: list[dict]) -> tuple[str, list[str]]:
    """
    Stitch multiple kpi_python_function blocks into one executable script.

    Args:
        kpi_entries: list of {"function_code": str, "filters": dict}

    Returns:
        (combined_code_string, list_of_function_names)
    """
    func_definitions: list[str] = []
    call_lines: list[str] = []
    func_names: list[str] = []

    for i, entry in enumerate(kpi_entries):
        func_code = entry["function_code"].strip()
        filters = entry.get("filters") or {}

        func_name = _extract_function_name(func_code)
        if not func_name:
            # Fallback: wrap raw SQL-like code in a named function
            func_name = f"_kpi_func_{i}"
            func_code = f"def {func_name}(execute_query, filters=None):\n    " + func_code.replace("\n", "\n    ") + "\n"

        func_definitions.append(func_code)
        call_lines.append(f"{func_name}_result = {func_name}(execute_query, {filters!r})")
        func_names.append(func_name)

    # Build final result dict
    result_entries = ", ".join(f'"{name}": {name}_result' for name in func_names)
    call_lines.append(f"result = {{{result_entries}}}")

    combined = "\n\n".join(func_definitions) + "\n\n" + "\n".join(call_lines)
    return combined, func_names


@tool
def execute_kpis(kpi_functions: str) -> str:
    """Execute collected KPI python functions in one deterministic batch.

    USE WHEN: You have called get_kpi on one or more KPIs and collected their
    kpi_python_function code. Instead of calling run_sql_python manually,
    pass the function codes and filters here for automatic batch execution.

    INPUT FORMAT (JSON string):
    [
        {
            "function_code": "def get_site_count(execute_query, filters=None): ...",
            "filters": {"m_market": "CHICAGO"}
        },
        {
            "function_code": "def get_gc_capacity(execute_query, filters=None): ...",
            "filters": {"m_market": "CHICAGO"}
        }
    ]

    - function_code: The FULL kpi_python_function string from get_kpi output.
      Copy it exactly as returned.
    - filters: Dict of filter key-value pairs. Use keys from kpi_filters or
      kpi_contract.parameters. Common keys: m_market, rgn_region,
      pj_general_contractor.

    RETURNS: JSON with results keyed by function name. Each value is a list
    of dicts (rows). Example:
    {
        "status": "success",
        "result": {
            "get_site_count": [{"m_market": "CHICAGO", "count": 142}, ...],
            "get_gc_capacity": [{"gc_company": "Acme", "capacity": 5}, ...]
        }
    }

    On error: returns {"status": "error", "error": "..."} — read the error
    and fix your input (likely a malformed function_code or wrong filter key).
    """
    # Parse input
    try:
        kpi_entries = json.loads(kpi_functions)
    except (json.JSONDecodeError, TypeError) as exc:
        return json.dumps({
            "status": "error",
            "error": f"Invalid JSON input: {exc}. Expected a JSON array of objects.",
        })

    if not isinstance(kpi_entries, list) or not kpi_entries:
        return json.dumps({
            "status": "error",
            "error": "Input must be a non-empty JSON array of {function_code, filters} objects.",
        })

    # Validate entries
    for i, entry in enumerate(kpi_entries):
        if not isinstance(entry, dict) or "function_code" not in entry:
            return json.dumps({
                "status": "error",
                "error": f"Entry {i} missing required 'function_code' field.",
            })

    # Build combined code
    try:
        combined_code, func_names = _build_combined_code(kpi_entries)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": f"Failed to build combined code: {exc}",
        })

    logger.info(
        "KPI Executor: running %d functions (%s) in one batch",
        len(func_names), ", ".join(func_names),
    )

    # Execute via PythonSandbox
    sandbox = PythonSandbox()
    exec_result = sandbox.execute(combined_code, timeout_seconds=60)

    # Truncate large outputs for context safety (same 30KB limit as run_sql_python)
    output = json.dumps(exec_result, default=str)
    if len(output) > 30000:
        # Trim individual result arrays while preserving structure
        if exec_result.get("status") == "success" and isinstance(exec_result.get("result"), dict):
            for key, rows in exec_result["result"].items():
                if isinstance(rows, list) and len(rows) > 50:
                    total = len(rows)
                    exec_result["result"][key] = rows[:50]
                    exec_result.setdefault("_truncated", {})[key] = {
                        "total_rows": total,
                        "rows_shown": 50,
                    }
            output = json.dumps(exec_result, default=str)

    return output
